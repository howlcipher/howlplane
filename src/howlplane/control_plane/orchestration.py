"""Session-scoped agent orchestration. Git is the evidence authority on recovery."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, TextIO

from howlplane.control_plane import agent_readiness, workspace_trust
from howlplane.control_plane.agent_execution import AgentBackendRegistry
from howlplane.control_plane.atomic_io import safe_load_json
from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager
from howlplane.control_plane.task_spec import TaskSpec

SCHEMA = "howlplane.orchestration/v1"
# v1: no capability evidence. v2: every agent carries `capabilities` and a
# role-keyed `capacity` record; load migrates v1 by replaying recorded failures.
# v3: an execution-budget stop is attempt evidence (`timed_out_assignments`),
# never role capacity; load converts v2's budget EXHAUSTED entries.
SCHEMA_VERSION = 3
AGENTS = ("claude_code", "codex", "cursor", "agy", "devin_cli")
ROLES = ("planning", "implementation", "review", "acceptance")
AGENT_STATES = {"AVAILABLE", "DEGRADED", "UNAVAILABLE", "RESERVED"}
UNAVAILABLE_FAILURES = {"MISSING_EXECUTABLE", "AUTHENTICATION_REQUIRED", "PROVIDER_UNAVAILABLE"}
MODEL_LIMIT_FAILURES = {"QUOTA_EXHAUSTED", "SESSION_LIMIT", "RATE_LIMITED"}
# Hard failures bar the same agent from the same role for the rest of the
# session, whatever model it would try next. Capacity failures are EXHAUSTED;
# the others are deterministic role failures that another model rarely fixes.
CAPACITY_FAILURES = {"SESSION_LIMIT"}
# HowlPlane's own per-assignment deadline expired. That says nothing about the
# provider's allowance: only this exact assignment is barred from running again
# unchanged, and the agent stays eligible for other models, roles, and tasks.
ATTEMPT_TIMEOUT_FAILURES = {"EXECUTION_BUDGET_EXCEEDED"}
# The CLI refused this session's directory as untrusted. That bars the agent
# from this workspace only: no role capacity, no session-wide capability loss,
# and no readiness penalty for other directories.
WORKSPACE_TRUST_FAILURES = {"WORKSPACE_TRUST_REQUIRED"}
DEFAULT_EXECUTION_BUDGET_SECONDS = 300
MAX_EXECUTION_BUDGET_SECONDS = 1800
# Every session before v3 dispatched with a hardcoded 300s deadline.
LEGACY_EXECUTION_BUDGET_SECONDS = 300
ROLE_FAILURES = {
    "EXECUTION_PERMISSION_REQUIRED", "ENGINEERING_FAILURE", "NO_REPOSITORY_CHANGE", "MALFORMED_OUTPUT",
    "CAPABILITY_FAILURE", "POLICY_FAILURE", "VERIFICATION_FAILURE", "PROVIDER_STALLED",
    "READ_ONLY_ROLE_MUTATED_REPOSITORY", "AUDIT_FINDINGS_OR_UNCONFIRMED", "ACCEPTANCE_REJECTED_OR_UNCONFIRMED",
}
REQUIRED_KEYS = ("id", "created_at", "goal", "orchestrator", "strategy", "failover", "policy", "stage", "status",
                 "agents", "attempts", "lease", "repository_evidence")
BINARIES = {"claude_code": "claude", "codex": "codex", "cursor": "agent", "agy": "agy", "devin_cli": "devin"}
TERMINAL = {"COMPLETE", "COMPLETE WITH WARNINGS", "BLOCKED", "HANDOFF REQUIRED"}
# A value stops at a quote or backslash so redacting serialized JSON cannot
# consume the string delimiter and corrupt the manifest.
SECRET = re.compile(r"(?i)(bearer\s+|(?:token|password|secret|api[_-]?key)[=: ]+)([^\s,;\x22\\]+)|\b(?:sk-|ghp_|gho_|github_pat_)[\w-]{8,}")


def redact(value: str) -> str:
    return SECRET.sub(lambda match: match.group(1) + "<redacted>" if match.group(1) else "<redacted>", value)


AGENT_NAMES = {"claude_code": "Claude", "codex": "Codex", "cursor": "Cursor", "agy": "AGY", "devin_cli": "Devin"}
PHASE_NAMES = {"planning": "PLAN", "implementation": "IMPLEMENT", "review": "AUDIT", "acceptance": "INTEGRATE", "verification": "VERIFY"}
PRIVATE_REASONING = re.compile(r"(?i)(?:private\s+)?(?:chain[- ]of[- ]thought|internal reasoning).*?(?:[.;]|$)")


class SessionProgress:
    def __init__(
        self,
        document: dict[str, Any],
        stream: TextIO | None = None,
        heartbeat_interval: float = 30.0,
        enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.document = document
        self.stream = stream or sys.stderr
        self.heartbeat_interval = max(1.0, float(heartbeat_interval))
        self.enabled = enabled
        self.clock = clock
        self.started_at = clock()
        self.last_visible_at = self.started_at
        self.last_change_at = self.started_at
        self.phase_name = PHASE_NAMES.get(document.get("stage", "planning"), "SESSION CREATED")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stall_reported = False

    @staticmethod
    def _safe(value: Any, limit: int = 160) -> str:
        text = PRIVATE_REASONING.sub("", redact(str(value))).replace("\n", " ").strip()
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def _write(self, label: str, message: str, timestamp: bool = True) -> None:
        if not self.enabled:
            return
        prefix = f"[{datetime.now().strftime('%H:%M:%S')}] " if timestamp else ""
        self.stream.write(f"{prefix}{label:<11} {self._safe(message)}\n")
        self.stream.flush()
        self.last_visible_at = self.clock()

    def session_started(self) -> None:
        if not self.enabled:
            return
        reserved = [AGENT_NAMES.get(agent, agent) for agent, state in self.document["agents"].items() if state["state"] == "RESERVED"]
        requested = self.document.get("requested_orchestrator", self.document["orchestrator"])
        selected = self.document.get("selected_orchestrator")
        if selected is None and requested != "AUTO":
            selected = requested
        lines = [
            "HOWL ORCHESTRATION",
            f"Session: {self.document['id'][:8]}...",
            f"Goal: {self._safe(self.document['goal'], 90)}",
            f"Requested orchestrator: {AGENT_NAMES.get(requested, requested)}",
            f"Selected orchestrator: {AGENT_NAMES.get(selected, selected) if selected else 'pending'}",
            f"Strategy: {self.document['strategy']}",
            f"Failover: {self.document['failover']}",
            f"Execution: {self.document['policy']}",
        ]
        if reserved:
            lines.append(f"Reserved agents: {', '.join(reserved)}")
        lines.extend(["", "Starting orchestration..."])
        self.stream.write("\n".join(lines) + "\n")
        self.stream.flush()
        self.last_visible_at = self.clock()

    def phase(self, phase: str, message: str) -> None:
        self.phase_name = PHASE_NAMES.get(phase, phase.upper())
        self.last_change_at = self.clock()
        self._stall_reported = False
        self._write(self.phase_name, message)

    def assignment(self, task_id: str, agent: str, task: str) -> None:
        self.last_change_at = self.clock()
        self._write("ASSIGN", f"{AGENT_NAMES.get(agent, agent)} assigned task {task_id}: {task}")

    def worker_complete(self, task_id: str, agent: str, task: str) -> None:
        self.last_change_at = self.clock()
        self._write("COMPLETE", f"{AGENT_NAMES.get(agent, agent)} completed {task_id}: {task}")

    def capability_downgrade(self, agent: str) -> None:
        self.last_change_at = self.clock()
        self._write("CAPABILITY", f"{AGENT_NAMES.get(agent, agent)} marked interactive-only for this session")

    def role_excluded(self, agent: str, role: str, entry: dict[str, Any]) -> None:
        self.last_change_at = self.clock()
        name = AGENT_NAMES.get(agent, agent)
        if entry["state"] == "EXHAUSTED":
            self._write("CAPABILITY", f"{name} {role} capacity marked exhausted for this session ({entry['reason']})")
        else:
            self._write("CAPABILITY", f"{name} excluded from {role} for this session after {entry['reason']}")

    def attempt_timed_out(self, agent: str, role: str, budget: int, partial: bool) -> None:
        self.last_change_at = self.clock()
        detail = "; partial changes kept for the next worker" if partial else ""
        self._write("TIMEOUT", f"{AGENT_NAMES.get(agent, agent)} {role} stopped at the {budget}s execution budget "
                               f"(attempt only; capacity unchanged){detail}")

    def selection(self, agent: str, role: str, evidence: str) -> None:
        self._write("SELECT", f"{AGENT_NAMES.get(agent, agent)} for {role}: {evidence}")

    def route_skip(self, agent: str, reason: str = "unattended execution unavailable") -> None:
        self._write("ROUTE", f"{AGENT_NAMES.get(agent, agent)} skipped: {reason}")

    def excluded(self, agent: str, reason: str) -> None:
        self._write("EXCLUDED", f"{AGENT_NAMES.get(agent, agent)}: {reason}")

    def explicit_override_warning(self, agent: str) -> None:
        self._write("WARNING", f"{AGENT_NAMES.get(agent, agent)} previously required interactive permission; explicit selection overrides AUTO exclusion")

    def selected_orchestrator(self, agent: str) -> None:
        self.last_change_at = self.clock()
        self._write("SELECT", f"{AGENT_NAMES.get(agent, agent)} selected as orchestrator")

    def limit(self, agent: str, model: str, reason: str) -> None:
        self.last_change_at = self.clock()
        self._write("LIMIT", f"{agent}/{model} reached {reason.lower().replace('_', ' ')}")

    def checkpoint(self, task_id: str) -> None:
        self._write("CHECKPOINT", f"Preserved task {task_id} state")

    def reroute(self, task_id: str, source: str, target: str, reason: str) -> None:
        self.last_change_at = self.clock()
        self._write("REROUTE", f"{task_id}: {AGENT_NAMES.get(source, source)} → {AGENT_NAMES.get(target, target)} ({reason})")
        reserved = [AGENT_NAMES.get(agent, agent) for agent, state in self.document["agents"].items() if state["state"] == "RESERVED"]
        if reserved:
            self._write("REROUTE", f"{', '.join(reserved)} skipped: RESERVED")

    def validation(self, started: bool, message: str) -> None:
        self.phase_name = "VERIFY"
        self.last_change_at = self.clock()
        self._write("VERIFY", message)

    def blocked(self, status: str, reason: str, next_step: str | None = None) -> None:
        self.last_change_at = self.clock()
        self._write(status, reason)
        if next_step:
            self._write("RESUME", next_step)

    def complete(self, status: str) -> None:
        successes = sum(attempt.get("state") == "SUCCEEDED" for attempt in self.document.get("attempts", []))
        passed = sum(test.get("exit_code") == 0 for test in self.document.get("tests", []))
        self._write("COMPLETE", f"Session finished; {successes} tasks completed; {len(self.document.get('reroutes', []))} rerouted; {passed} validations passed; final status: {status}")

    def heartbeat(self, force: bool = False) -> bool:
        current = self.clock()
        if not force and current - self.last_visible_at < self.heartbeat_interval:
            return False
        active = [attempt for attempt in self.document.get("attempts", []) if attempt.get("state") == "ASSIGNED"]
        if active:
            tasks = ", ".join(attempt.get("task_id", self.document["id"][:8]) for attempt in active)
            message = f"{self.phase_name} — {len(active)} active worker(s); tasks: {tasks}"
        else:
            message = f"Session active; current phase: {self.phase_name}"
        self._write("WORKING", message)
        if current - self.last_change_at >= 300 and not self._stall_reported:
            self._write("WARNING", "No worker state change for 5m; session remains active")
            self._stall_reported = True
        return True

    @contextmanager
    def waiting(self):
        if self.enabled:
            self._stop.clear()
            self._thread = threading.Thread(target=self._heartbeat_loop, name=f"howl-progress-{self.document['id'][:8]}", daemon=True)
            self._thread.start()
        try:
            yield
        finally:
            self._stop.set()
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(timeout=1)
            self._thread = None

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            self.heartbeat()


def state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    if not base or not Path(base).is_absolute():
        base = str(Path.home() / ".local" / "state")
    return Path(base) / "howlplane" / "orchestrate"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=20)
    if result.returncode:
        raise ValueError(f"Git inspection failed: {redact(result.stderr.strip())}")
    return result.stdout.strip()


def evidence(repo: Path) -> dict[str, Any]:
    import hashlib
    root = Path(git(repo, "rev-parse", "--show-toplevel")).resolve()
    def digest(*args: str) -> str:
        return hashlib.sha256(git(root, *args).encode()).hexdigest()
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z")
    untracked_hashes = {}
    for name in filter(None, untracked.split("\0")):
        candidate = root / name
        if candidate.is_file() and not candidate.is_symlink():
            untracked_hashes[name] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return {
        "root": str(root),
        "head": git(root, "rev-parse", "HEAD"),
        "status": git(root, "status", "--porcelain=v1", "--untracked-files=all"),
        "diff_sha256": digest("diff", "--binary", "--no-ext-diff"),
        "cached_diff_sha256": digest("diff", "--cached", "--binary", "--no-ext-diff"),
        "worktrees": git(root, "worktree", "list", "--porcelain"),
        "untracked_sha256": untracked_hashes,
    }


def fingerprint(snapshot: dict[str, Any]) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


@contextmanager
def locked(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with (root / ".lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def path_for(root: Path, session_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", session_id):
        raise ValueError("Invalid session ID")
    return root / f"{session_id}.json"


def save(path: Path, document: dict[str, Any], token: str) -> None:
    current = safe_load_json(path) if path.exists() else None
    if current and current.get("lease", {}).get("token") != token:
        raise ValueError("Coordinator lease changed; stale assignment rejected")
    clean = json.loads(redact(json.dumps(document)))
    secure_write(path, clean)


def secure_write(path: Path, document: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(json.loads(redact(json.dumps(document))), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def active_sessions(root: Path, repo: Path, include_terminal: bool = False) -> list[dict[str, Any]]:
    sessions = []
    for path in root.glob("[0-9a-f]*.json"):
        doc = safe_load_json(path)
        if isinstance(doc, dict) and doc.get("schema") == SCHEMA and doc.get("repository") == str(repo) and (include_terminal or doc.get("status") not in TERMINAL):
            sessions.append(doc)
    return sorted(sessions, key=lambda item: str(item.get("created_at", "")), reverse=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def discover_models(agent: str) -> list[str]:
    """Read only advertised lists. A silent CLI default remains UNKNOWN."""
    try:
        return agent_readiness.routing_models(agent)
    except (OSError, ValueError):
        return []


def inventory(availability: dict[str, str], repo: Path | None = None) -> dict[str, dict[str, Any]]:
    """Session start inventory. Cached readiness evidence informs it; nothing here sends a prompt.

    `local_only` forbids hosted egress, and every agent here is a hosted CLI, so
    none is dispatchable in that mode -- the same verdict the doctor and the
    provider pool give. With a repository, each agent also carries its vendor
    trust state for that workspace, read from local files only.
    """
    readiness = agent_readiness.cached_summaries()
    hosted = agent_readiness.hosted_probes_allowed()
    found = {}
    for agent in AGENTS:
        installed = shutil.which(BINARIES[agent]) is not None
        requested = availability.get(agent, "AUTO").upper()
        state = "RESERVED" if requested == "RESERVED" else "AVAILABLE" if installed and requested != "UNAVAILABLE" else "UNAVAILABLE"
        if state == "AVAILABLE" and not hosted:
            state = "UNAVAILABLE"
        evidence = readiness.get(agent)
        capabilities = unknown_capabilities("CLI metadata does not confirm unattended execution")
        if evidence and evidence["unattended_execution"] is not None:
            capabilities.update({"unattended_execution": evidence["unattended_execution"],
                                 "reason": f"agent readiness: {evidence['unattended_source']}",
                                 "evidence_time": evidence["last_verified_at"], "scope": "readiness cache"})
        if state == "AVAILABLE" and evidence:
            # Only fresh, negative, agent-wide evidence removes an agent; UNKNOWN never does.
            if evidence["authenticated"] is False or evidence["capacity"]["state"] in {"QUOTA_EXHAUSTED", "SESSION_EXHAUSTED", "RATE_LIMITED"}:
                state = "UNAVAILABLE"
            elif capabilities["unattended_execution"] is False:
                state = "DEGRADED"
        found[agent] = {
            "backend": BINARIES[agent],
            "installed": installed,
            "callable": installed,
            "state": state,
            "models": discover_models(agent) if installed and hosted and requested != "RESERVED" else [],
            "capabilities": capabilities,
            "capacity": {},
            "readiness": readiness_digest(evidence),
        }
        if not hosted and installed:
            found[agent]["unavailable_reason"] = agent_readiness.LOCAL_ONLY_DETAIL
        if repo is not None and installed:
            trust = workspace_trust.check(agent, repo)
            found[agent]["workspace_trust"] = {key: trust.get(key) for key in (
                "state", "workspace", "scope", "covering_path", "detail", "source", "checked_at")}
    return found


def readiness_digest(evidence: dict[str, Any] | None) -> dict[str, Any]:
    """The routing-relevant slice of cached readiness, kept on the session record."""
    if not evidence:
        return {"live_smoke": "NOT_RUN", "verified_at": None, "capacity_state": "UNKNOWN", "model_limits": []}
    return {"live_smoke": evidence["live_smoke"]["status"], "verified_at": evidence["live_smoke"]["verified_at"],
            "capacity_state": evidence["capacity"]["state"],
            "model_limits": [f"{item['model']}:{item['state']}" for item in evidence["capacity"]["limits"] if item.get("model")]}


def selection_evidence(doc: dict[str, Any], agent: str) -> str:
    """Observable facts behind an AUTO choice; no subjective ranking."""
    state = agent_record(doc, agent)
    unattended = state["capabilities"]["unattended_execution"]
    readiness = state.get("readiness") or readiness_digest(None)
    smoke = readiness.get("live_smoke", "NOT_RUN")
    age = agent_readiness.age_seconds(readiness.get("verified_at"))
    smoke_text = {"NOT_RUN": "not run", "STALE": "stale"}.get(smoke, smoke.lower())
    if smoke == "PASS" and age is not None:
        smoke_text = f"passed {agent_readiness._duration(age)} ago"
    return (f"unattended {({True: 'verified', False: 'interactive-only (explicit override)', None: 'unverified'})[unattended]}"
            f" · live smoke {smoke_text} · capacity {readiness.get('capacity_state', 'UNKNOWN')}")


class SessionStateInvalid(ValueError):
    """A manifest that cannot be normalized without guessing at its evidence."""


def unknown_capabilities(reason: str = "No unattended execution evidence recorded") -> dict[str, Any]:
    return {"unattended_execution": None, "reason": reason, "evidence_time": None, "scope": "session"}


def normalize_agent(agent: str, record: Any) -> dict[str, Any]:
    """Bring one agent record to the v2 shape. Missing evidence is UNKNOWN, never AVAILABLE."""
    if not isinstance(record, dict) or record.get("state") not in AGENT_STATES:
        raise SessionStateInvalid(f"Agent record for {agent} is malformed")
    record.setdefault("backend", BINARIES.get(agent, agent))
    record.setdefault("installed", False)
    record.setdefault("callable", record["installed"])
    if not isinstance(record.get("models"), list):
        record["models"] = []
    capabilities = record.get("capabilities")
    if not isinstance(capabilities, dict) or capabilities.get("unattended_execution") not in (None, True, False):
        record["capabilities"] = unknown_capabilities()
    else:
        for key, value in unknown_capabilities().items():
            capabilities.setdefault(key, value)
    capacity = record.get("capacity")
    record["capacity"] = {
        role: entry for role, entry in (capacity.items() if isinstance(capacity, dict) else ())
        if isinstance(entry, dict) and entry.get("state") in {"EXHAUSTED", "FAILED"} and entry.get("reason")
    }
    return record


def agent_record(doc: dict[str, Any], agent: str) -> dict[str, Any]:
    """Normalized access to an agent. An agent the manifest never recorded is UNAVAILABLE."""
    agents = doc["agents"]
    if agent not in agents:
        agents[agent] = {"backend": BINARIES.get(agent, agent), "installed": False, "callable": False,
                         "state": "UNAVAILABLE", "models": [],
                         "capabilities": unknown_capabilities("Agent absent from session manifest"), "capacity": {}}
    return normalize_agent(agent, agents[agent])


def normalize_session(doc: Any) -> list[str]:
    """Validate and migrate a loaded manifest in place; return migration notes for progress."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise SessionStateInvalid("Manifest is not an orchestration session")
    missing = [key for key in REQUIRED_KEYS if key not in doc]
    if missing:
        raise SessionStateInvalid(f"Manifest lacks required fields: {', '.join(missing)}")
    if not isinstance(doc["agents"], dict) or not isinstance(doc["attempts"], list) or not isinstance(doc["lease"], dict) or "token" not in doc["lease"]:
        raise SessionStateInvalid("Manifest agents, attempts, or lease are malformed")
    if any(not isinstance(item, dict) or not {"stage", "agent", "model"} <= item.keys() for item in doc["attempts"]):
        raise SessionStateInvalid("Manifest attempt history is malformed")
    version = doc.get("schema_version", 1)
    if not isinstance(version, int) or not 1 <= version <= SCHEMA_VERSION:
        raise SessionStateInvalid(f"Unsupported session schema version {version!r}")
    for key, default in (("constraints", []), ("role_models", {}), ("fallbacks", {}), ("model_states", {}),
                         ("known_models", {}), ("tests", []), ("reroutes", []), ("capability_notices", []),
                         ("timed_out_assignments", []), ("execution_budget", {})):
        if not isinstance(doc.get(key), type(default)):
            doc[key] = default
    try:
        doc["execution_budget"] = {**default_execution_budget(), **validate_execution_budget(doc["execution_budget"])}
    except ValueError as error:
        raise SessionStateInvalid(str(error)) from error
    doc["lease"].setdefault("pid", 0)
    doc["lease"].setdefault("renewed_at", 0)
    for agent in list(doc["agents"]):
        normalize_agent(agent, doc["agents"][agent])
    for agent in AGENTS:
        agent_record(doc, agent)
    if version == SCHEMA_VERSION:
        return []
    # v1 recorded failures only in the attempt log. Replaying that log restores
    # the session's hard-failure evidence; successes are not replayed as
    # positive capability claims, they only clear an earlier same-role failure.
    for attempt in doc["attempts"]:
        agent, role = attempt["agent"], attempt["stage"]
        if agent not in AGENTS:
            continue
        # v2 already holds the replayed outcome of every other attempt.
        if attempt.get("state") == "SUCCEEDED" and version == 1:
            agent_record(doc, agent)["capacity"].pop(role, None)
        elif attempt.get("failure") in ATTEMPT_TIMEOUT_FAILURES:
            record_timeout(doc, role, agent, attempt["model"], LEGACY_EXECUTION_BUDGET_SECONDS,
                           attempt.get("timeout_source"), attempt.get("finished_at"))
        elif attempt.get("failure") and version == 1:
            record_failure(doc, agent, role, attempt["model"], attempt["failure"], attempt.get("finished_at"))
    # v2 recorded a budget stop as role capacity EXHAUSTED. It was attempt
    # evidence all along: keep it as such and undo the degradation it caused.
    for agent in AGENTS:
        state = agent_record(doc, agent)
        for role, entry in list(state["capacity"].items()):
            if entry["reason"] in ATTEMPT_TIMEOUT_FAILURES:
                del state["capacity"][role]
                record_timeout(doc, role, agent, entry.get("model", "UNKNOWN"), LEGACY_EXECUTION_BUDGET_SECONDS,
                               None, entry.get("evidence_time"))
        if (state["state"] == "DEGRADED" and not state["capacity"]
                and state["capabilities"]["unattended_execution"] is not False):
            state["state"] = "AVAILABLE"
    doc["schema_version"] = SCHEMA_VERSION
    doc.setdefault("migrations", []).append({"from": version, "to": SCHEMA_VERSION, "at": now()})
    return [f"Session manifest normalized from schema v{version} → v{SCHEMA_VERSION}"]


def record_capability_failure(doc: dict[str, Any], agent: str, reason: str, at: str | None = None) -> None:
    state = agent_record(doc, agent)
    if state["state"] == "AVAILABLE":
        state["state"] = "DEGRADED"
    state["capabilities"].update({"unattended_execution": False, "reason": reason, "evidence_time": at or now(), "scope": "session"})


def record_capability_success(doc: dict[str, Any], agent: str) -> None:
    state = agent_record(doc, agent)
    state["state"] = "AVAILABLE"
    state["capabilities"].update({"unattended_execution": True, "reason": "Successful unattended session invocation",
                                  "evidence_time": now(), "scope": "session"})


def record_failure(doc: dict[str, Any], agent: str, role: str, model: str, failure: str, at: str | None = None) -> dict[str, Any] | None:
    """Apply one failed attempt to session evidence. Returns the role exclusion it created, if any."""
    state = agent_record(doc, agent)
    if failure in ATTEMPT_TIMEOUT_FAILURES:
        # Attempt evidence only; `record_timeout` bars the identical retry.
        return None
    if failure in WORKSPACE_TRUST_FAILURES:
        state["workspace_trust"] = {"state": workspace_trust.TRUST_REQUIRED, "workspace": doc.get("repository"),
                                    "role": role, "source": "orchestrate session", "detected_at": at or now()}
        return None
    if failure == "EXECUTION_PERMISSION_REQUIRED":
        record_capability_failure(doc, agent, failure, at)
    if failure in UNAVAILABLE_FAILURES:
        state["state"] = "UNAVAILABLE"
    if failure in MODEL_LIMIT_FAILURES:
        doc["model_states"][f"{agent}:{model}"] = "EXHAUSTED"
        if model == "UNKNOWN":
            state["state"] = "UNAVAILABLE"
    if failure not in CAPACITY_FAILURES and failure not in ROLE_FAILURES:
        return None
    if state["state"] == "AVAILABLE":
        state["state"] = "DEGRADED"
    entry = {"state": "EXHAUSTED" if failure in CAPACITY_FAILURES else "FAILED", "reason": failure,
             "model": model, "evidence_time": at or now(), "scope": "session"}
    state["capacity"][role] = entry
    return entry


def capacity_reason(entry: dict[str, Any]) -> str:
    return {"SESSION_LIMIT": "session limit reached"}.get(entry["reason"], f"failed earlier this session ({entry['reason']})")


def default_execution_budget() -> dict[str, int]:
    return {role: DEFAULT_EXECUTION_BUDGET_SECONDS for role in ROLES}


def validate_execution_budget(budget: dict[str, Any]) -> dict[str, int]:
    """A finite per-role deadline. Anything outside 1..MAX is refused, never clamped."""
    checked = {}
    for role, seconds in budget.items():
        if role not in ROLES:
            raise ValueError(f"Unknown execution budget role {role!r}")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= MAX_EXECUTION_BUDGET_SECONDS:
            raise ValueError(f"Execution budget for {role} must be 1..{MAX_EXECUTION_BUDGET_SECONDS} seconds")
        checked[role] = seconds
    return checked


def parse_execution_budget(values: list[str] | None) -> dict[str, int]:
    """Parse repeatable `role=seconds` (or a bare `seconds` for every role)."""
    parsed: dict[str, int] = {}
    for value in values or []:
        role, _, seconds = value.rpartition("=")
        if not seconds.strip().isdigit():
            raise ValueError("Expected --execution-budget role=seconds or seconds")
        for target in ([role.strip()] if role else ROLES):
            parsed[target] = int(seconds)
    return validate_execution_budget(parsed)


def execution_budget(doc: dict[str, Any], role: str) -> int:
    return doc.get("execution_budget", {}).get(role, DEFAULT_EXECUTION_BUDGET_SECONDS)


def timeout_signature(doc: dict[str, Any], role: str, agent: str, model: str, budget: int | None = None) -> str:
    """Identity of an assignment. Changing its scope, worker, model, or deadline yields a new one."""
    import hashlib
    identity = {"goal": doc["goal"], "constraints": doc["constraints"], "role": role, "agent": agent, "model": model,
                "budget": execution_budget(doc, role) if budget is None else budget}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]


def record_timeout(doc: dict[str, Any], role: str, agent: str, model: str, budget: int,
                   source: str | None = None, at: str | None = None) -> None:
    signature = timeout_signature(doc, role, agent, model, budget)
    ledger = doc.setdefault("timed_out_assignments", [])
    if any(item.get("signature") == signature for item in ledger):
        return
    ledger.append({"signature": signature, "stage": role, "agent": agent, "model": model,
                   "execution_budget_seconds": budget, "timeout_source": source, "at": at or now()})


def timed_out(doc: dict[str, Any], role: str, agent: str, model: str) -> bool:
    """True when this exact assignment already hit its deadline and would run again unchanged."""
    signature = timeout_signature(doc, role, agent, model)
    return any(item.get("signature") == signature for item in doc.get("timed_out_assignments", []))


def workspace_trust_block(doc: dict[str, Any], agent: str) -> str | None:
    """Why this session's workspace refuses the agent, or None. Scoped to the workspace, never the agent."""
    record = agent_record(doc, agent)
    trust = record.get("workspace_trust") or {}
    if trust.get("state") != workspace_trust.TRUST_REQUIRED:
        return None
    if trust.get("source") == "vendor trust store" and trust.get("workspace"):
        # A pre-dispatch verdict from local files: an operator may have prepared
        # the workspace since (for example, before `resume`). A refusal the CLI
        # itself returned this session stays binding: no retry in this folder.
        current = workspace_trust.check(agent, trust["workspace"])
        if current["state"] == workspace_trust.READY:
            record["workspace_trust"] = {**trust, "state": current["state"], "covering_path": current["covering_path"],
                                         "checked_at": current["checked_at"]}
            return None
    return f"workspace trust required for {trust.get('workspace') or doc.get('repository')} (run howlplane factory prepare)"


def capability_skip_reason(doc: dict[str, Any], agent: str, role: str) -> str | None:
    state = agent_record(doc, agent)
    if state["state"] == "RESERVED":
        return "reserved"
    trust_block = workspace_trust_block(doc, agent)
    if trust_block:
        # An explicit orchestrator choice cannot answer a vendor trust prompt.
        return trust_block
    # A role hard failure outranks an explicit orchestrator choice: selecting an
    # agent is not consent to burn another call on capacity it just exhausted.
    if role in state["capacity"]:
        return capacity_reason(state["capacity"][role])
    if state["capabilities"]["unattended_execution"] is not False:
        return None
    if doc.get("requested_orchestrator") == agent:
        return "explicit override"
    return "unattended execution unavailable"


def exclusions(doc: dict[str, Any], role: str) -> list[dict[str, str]]:
    """Why each agent is not a candidate for this role, for the terminal explanation."""
    eligible = {agent for agent, _ in candidates(doc, role)}
    found = []
    for agent in AGENTS:
        state = agent_record(doc, agent)
        if agent in eligible and not (role == "review" and agent == doc.get("implementer")):
            continue
        reason = capability_skip_reason(doc, agent, role)
        if reason in (None, "explicit override"):
            if role == "review" and agent == doc.get("implementer"):
                reason = "implemented this change; audit must be independent"
            elif state["state"] == "UNAVAILABLE":
                reason = "unavailable"
            elif role == "acceptance":
                reason = "not the session orchestrator"
            elif any(item["stage"] == role and item["agent"] == agent and timed_out(doc, role, agent, item["model"])
                     for item in doc.get("timed_out_assignments", [])):
                reason = (f"timed out at the {execution_budget(doc, role)}s execution budget; a retry needs a changed "
                          "budget, model, or scope (capacity unaffected)")
            else:
                reason = "no usable model remains"
        found.append({"agent": agent, "reason": reason})
    return found


def candidates(doc: dict[str, Any], role: str) -> list[tuple[str, str]]:
    agents = {agent: agent_record(doc, agent) for agent in AGENTS}
    lead = doc["orchestrator"]
    order = list(AGENTS)
    if lead != "AUTO":
        order.remove(lead)
        order.insert(0, lead)
    if role == "review" and doc.get("implementer") in order:
        order.remove(doc["implementer"])
    if doc["strategy"] == "ECONOMY":
        order.sort(key=lambda value: (value not in ("agy", "cursor"), value != lead))
    elif doc["strategy"] == "QUALITY":
        order.sort(key=lambda value: (value not in ("claude_code", "codex"), value != lead))
    if role in {"planning", "acceptance"} and lead != "AUTO" and lead in order:
        order.remove(lead)
        order.insert(0, lead)
    if role == "acceptance":
        order = order[:1]
    selected = []
    for agent in order:
        state = agents[agent]["state"]
        skip_reason = capability_skip_reason(doc, agent, role)
        explicit_override = skip_reason == "explicit override"
        if state == "RESERVED" or state not in {"AVAILABLE", "DEGRADED"}:
            continue
        if skip_reason and not explicit_override:
            continue
        configured = doc["role_models"].get(role, {}).get(agent, "AUTO")
        discovered = agents[agent]["models"]
        if configured != "AUTO":
            if configured not in discovered and configured not in doc.get("known_models", {}).get(agent, []):
                continue
            models = [configured]
        else:
            models = discovered or ["UNKNOWN"]
        for model in models[:2]:
            if doc["model_states"].get(f"{agent}:{model}") != "EXHAUSTED" and not timed_out(doc, role, agent, model):
                selected.append((agent, model))
    fallback = [] if role == "acceptance" else doc.get("fallbacks", {}).get(role, [])
    if fallback:
        for item in fallback:
            agent, model = item.split(":", 1)
            skip_reason = capability_skip_reason(doc, agent, role)
            if agents[agent]["state"] == "AVAILABLE" and skip_reason in (None, "explicit override") and (
                model == "UNKNOWN" or model in agents[agent]["models"]
                or model in doc.get("known_models", {}).get(agent, [])
            ) and doc["model_states"].get(item) != "EXHAUSTED" and not timed_out(doc, role, agent, model):
                selected.append((agent, model))
        rank = {item: index for index, item in enumerate(fallback)}
        selected.sort(key=lambda pair: rank.get(f"{pair[0]}:{pair[1]}", len(rank)))
    return list(dict.fromkeys(selected))[:8]


def question(label: str, default: str) -> str:
    answer = input(f"{label} [{default}]: ").strip()
    return answer or default


def setup(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    interactive = sys.stdin.isatty() and not args.input
    goal = args.input or (question("1. Goal", "") if interactive else "")
    if not goal:
        raise ValueError("A goal is required")
    lead = args.orchestrator or (question("2. Orchestrator (AUTO/agent)", "AUTO") if interactive else "AUTO")
    availability_text = question("3. Agent availability (AUTO or agent=RESERVED,...)", "AUTO") if interactive else "AUTO"
    availability = {agent: "AUTO" for agent in AGENTS}
    if availability_text != "AUTO":
        for item in availability_text.split(","):
            pair = item.strip().split("=", 1)
            if len(pair) != 2 or pair[0] not in AGENTS:
                raise ValueError("Expected agent=RESERVED or agent=UNAVAILABLE")
            availability[pair[0]] = pair[1].upper()
    for agent in AGENTS:
        option = getattr(args, agent)
        if option:
            availability[agent] = option
    strategy = args.strategy or (question("4. Strategy (BALANCED/ECONOMY/QUALITY)", "BALANCED") if interactive else "BALANCED")
    model_text = args.models or (question("5. Role models (AUTO or role:agent:model)", "AUTO") if interactive else "AUTO")
    fallback_text = args.fallbacks or (question("6. Ordered fallback (AUTO or role:agent:model,...)", "AUTO") if interactive else "AUTO")
    failover = args.failover or (question("7. Failover (AUTO REROUTE/OFF)", "AUTO REROUTE") if interactive else "AUTO REROUTE")
    policy = args.policy or (question("8. Execution policy", "PLAN + EXECUTE + INDEPENDENT AUDIT") if interactive else "PLAN + EXECUTE + INDEPENDENT AUDIT")
    if lead != "AUTO" and lead not in AGENTS:
        raise ValueError("Unsupported orchestrator")
    if strategy not in {"BALANCED", "ECONOMY", "QUALITY"} or failover not in {"AUTO REROUTE", "OFF"}:
        raise ValueError("Unsupported strategy or failover policy")
    if policy not in {"PLAN + EXECUTE + INDEPENDENT AUDIT", "PLAN ONLY", "PLAN + EXECUTE"}:
        raise ValueError("Unsupported execution policy")
    if any(value.upper() not in {"AUTO", "RESERVED", "UNAVAILABLE"} for value in availability.values()):
        raise ValueError("Unsupported agent availability")
    def triples(value: str) -> list[tuple[str, str, str]]:
        if value == "AUTO":
            return []
        result = []
        for item in value.split(","):
            parts = item.strip().split(":", 2)
            if len(parts) != 3 or parts[0] not in {"planning", "implementation", "review", "acceptance"} or parts[1] not in AGENTS:
                raise ValueError("Expected role:agent:model")
            result.append((parts[0], parts[1], parts[2]))
        return result
    role_models: dict[str, dict[str, str]] = {}
    for role, agent, model in triples(model_text):
        role_models.setdefault(role, {})[agent] = model
    fallbacks: dict[str, list[str]] = {}
    for role, agent, model in triples(fallback_text):
        fallbacks.setdefault(role, []).append(f"{agent}:{model}")
    budget = {**default_execution_budget(), **parse_execution_budget(getattr(args, "execution_budget", None))}
    snapshot = evidence(repo)
    token = uuid.uuid4().hex
    agents = inventory(availability, Path(snapshot["root"]))
    model_states = {f"{agent}:{limit.rsplit(':', 1)[0]}": "EXHAUSTED"
                    for agent, record in agents.items() for limit in record["readiness"]["model_limits"]}
    if lead != "AUTO" and agents[lead]["state"] != "AVAILABLE":
        raise ValueError("Selected orchestrator is not available for this session"
                         + (f" ({agents[lead]['unavailable_reason']})" if agents[lead].get("unavailable_reason") else ""))
    if lead != "AUTO" and (agents[lead].get("workspace_trust") or {}).get("state") == workspace_trust.TRUST_REQUIRED:
        raise ValueError(f"Selected orchestrator {AGENT_NAMES.get(lead, lead)} does not trust {snapshot['root']} yet; "
                         "authorize and prepare it with `howlplane factory prepare`")
    return {
        "schema": SCHEMA, "schema_version": SCHEMA_VERSION, "id": uuid.uuid4().hex, "created_at": now(), "repository": snapshot["root"],
        "goal": redact(goal), "constraints": [redact(item) for item in args.constraint],
        "requested_orchestrator": lead, "selected_orchestrator": None if lead == "AUTO" else lead,
        "orchestrator": lead, "agents": agents, "strategy": strategy,
        "role_models": role_models, "fallbacks": fallbacks,
        "failover": failover, "policy": policy, "status": "PLANNED", "stage": "planning",
        "model_states": model_states, "known_models": {}, "attempts": [], "tests": [], "reroutes": [],
        "execution_budget": budget, "timed_out_assignments": [],
        "verify_command": args.verify,
        "repository_evidence": snapshot, "lease": {"token": token, "pid": os.getpid(), "renewed_at": time.time()},
    }


def execute_assignment(doc: dict[str, Any], role: str, agent: str, model: str, repo: Path) -> Any:
    instructions = (
        f"Goal: {doc['goal']}\nRole: {role}. Work only in {repo}. "
        "Report files changed, tests run, remaining risks, and completion status. "
        "Respect repository rules. Do not commit, push, publish, or change other worktrees. "
        f"Constraints: {'; '.join(doc['constraints']) or 'none'}. "
    )
    if role == "review":
        instructions += "Independently inspect the current diff and falsify correctness. Do not edit files. End with exactly AUDIT_STATUS: CLEAN only if you found no issue; otherwise end with AUDIT_STATUS: FINDINGS."
    elif role == "acceptance":
        instructions += "As session orchestrator, inspect implementation, tests, and independent audit. Do not edit files. End with exactly ACCEPTANCE_STATUS: ACCEPTED only if evidence supports the goal; otherwise end with ACCEPTANCE_STATUS: REJECTED."
    elif role == "implementation":
        instructions += "Implement the goal and run relevant local tests. Inspect existing partial changes first."
    else:
        instructions += "Produce a bounded implementation plan and acceptance criteria. Do not edit files."
    if any(item.get("stage") == role and item.get("state") == "TIMED_OUT" and item.get("partial_changes") for item in doc["attempts"]):
        instructions += (" An earlier attempt at this role reached its execution budget and left partial changes: "
                         "inspect and continue them rather than starting over.")
    if model != "UNKNOWN":
        instructions += f" Use model {model} if the CLI supports selecting it; report actual model identity."
    task = TaskSpec(task_id=doc["id"], repository=str(repo), objective=doc["goal"], constraints=doc["constraints"])
    backend = AgentBackendRegistry.get_backend(agent)
    return backend.execute(task, repo, role=role, prompt_override=instructions,
                           timeout_seconds=execution_budget(doc, role), model_id=model)


def checkpoint(doc: dict[str, Any], path: Path, token: str, repo: Path) -> None:
    doc["repository_evidence"] = evidence(repo)
    doc["lease"]["renewed_at"] = time.time()
    with locked(path.parent):
        save(path, doc, token)


def reconcile(doc: dict[str, Any], repo: Path) -> None:
    if doc["attempts"] and doc["attempts"][-1].get("state") == "ASSIGNED":
        doc["attempts"][-1].update({"state": "REVOKED", "failure": "INTERRUPTED_OR_STALE_LEASE", "finished_at": now()})
    actual = evidence(repo)
    previous = doc["repository_evidence"]
    if fingerprint(actual) != fingerprint(previous):
        doc["reconciliation"] = {"recorded": fingerprint(previous), "actual": fingerprint(actual), "at": now(), "needs_validation": True}
        doc["tests"] = []
        doc["stage"] = "implementation" if doc["policy"] != "PLAN ONLY" else "planning"
    doc["repository_evidence"] = actual


def run(doc: dict[str, Any], path: Path, repo: Path, progress: SessionProgress | None = None) -> int:
    token = doc["lease"]["token"]
    stages = ["planning"] if doc["policy"] == "PLAN ONLY" else ["planning", "implementation"]
    if doc["policy"] == "PLAN + EXECUTE + INDEPENDENT AUDIT":
        stages.append("review")
    if doc["policy"] != "PLAN ONLY":
        stages.append("acceptance")
    start = stages.index(doc["stage"]) if doc["stage"] in stages else 0
    for stage in stages[start:]:
        doc["stage"] = stage
        doc["status"] = stage.upper()
        checkpoint(doc, path, token, repo)
        if progress:
            progress.phase(stage, {"planning": "Building implementation plan", "implementation": "Dispatching implementation worker", "review": "Independent audit started", "acceptance": "Orchestrator validating final result"}[stage])
            notices = doc.setdefault("capability_notices", [])
            for candidate in AGENTS:
                reason = capability_skip_reason(doc, candidate, stage)
                # Session-wide reasons are announced once; capacity is per role.
                notice = f"{candidate}:{reason}" if reason in ("explicit override", "unattended execution unavailable") else f"{candidate}:{stage}:{reason}"
                if reason in (None, "reserved") or notice in notices:
                    continue
                if reason == "explicit override":
                    progress.explicit_override_warning(candidate)
                else:
                    progress.route_skip(candidate, reason)
                notices.append(notice)

        def eligible() -> list[tuple[str, str]]:
            options = candidates(doc, stage)
            return [pair for pair in options if not (stage == "review" and pair[0] == doc.get("implementer"))]

        succeeded = False
        transient_retries: dict[tuple[str, str], int] = {}
        attempted: list[tuple[str, str]] = []
        retry: tuple[str, str] | None = None
        prev_reroute: tuple[str, str] | None = None
        while True:
            # Candidates are recomputed from session evidence after every
            # failure, so the next assignment (and the REROUTE naming it) never
            # reflects a list selected before the failure was recorded.
            if retry:
                agent, model = retry
                retry = None
            else:
                pending = [pair for pair in eligible() if pair not in attempted]
                if not pending:
                    break
                # After a deadline stop, prefer a different worker; the timed-out
                # agent's other models stay eligible, just behind the rest.
                slow = {item["agent"] for item in doc["timed_out_assignments"] if item["stage"] == stage}
                pending.sort(key=lambda pair: pair[0] in slow)
                agent, model = pending[0]
            attempted.append((agent, model))
            if prev_reroute and progress:
                source_agent, reason = prev_reroute
                progress.reroute(doc["id"][:8], source_agent, agent, reason)
                prev_reroute = None
            before = evidence(repo)
            assignment = {"task_id": doc["id"][:8], "stage": stage, "agent": agent, "model": model, "started_at": now(), "fence": token, "state": "ASSIGNED",
                          "execution_budget_seconds": execution_budget(doc, stage), "selection_evidence": selection_evidence(doc, agent)}
            doc["attempts"].append(assignment)
            checkpoint(doc, path, token, repo)
            if progress:
                progress.selection(agent, stage, assignment["selection_evidence"])
                progress.assignment(doc["id"][:8], agent, stage)
                progress._write("START", f"{AGENT_NAMES.get(agent, agent)} started {stage}")
                with progress.waiting():
                    result = execute_assignment(doc, stage, agent, model, repo)
            else:
                result = execute_assignment(doc, stage, agent, model, repo)
            failure = ProviderPoolManager.classify_result(agent, result)
            after = evidence(repo)
            assignment.update({"state": "SUCCEEDED" if result.success else "REVOKED", "exit_code": result.exit_code,
                               "failure": None if result.success else failure.value, "finished_at": now(),
                               "repo_before": fingerprint(before), "repo_after": fingerprint(after),
                               "output_sha256": fingerprint({"stdout": result.stdout}),
                               "error": redact((result.error_message or result.stderr)[:200])})
            no_change_detail = ""
            if result.success and stage == "implementation" and before == after:
                assignment["state"] = "REVOKED"
                assignment["failure"] = "NO_REPOSITORY_CHANGE"
                no_change_detail = " (no repository delta detected)"
            if stage in {"review", "acceptance"} and before != after:
                assignment["state"] = "REVOKED"
                assignment["failure"] = "READ_ONLY_ROLE_MUTATED_REPOSITORY"
            if result.success and stage == "review" and not result.stdout.strip().endswith("AUDIT_STATUS: CLEAN"):
                assignment["state"] = "REVOKED"
                assignment["failure"] = "AUDIT_FINDINGS_OR_UNCONFIRMED"
            if result.success and stage == "acceptance" and not result.stdout.strip().endswith("ACCEPTANCE_STATUS: ACCEPTED"):
                assignment["state"] = "REVOKED"
                assignment["failure"] = "ACCEPTANCE_REJECTED_OR_UNCONFIRMED"
            if assignment["failure"] in ATTEMPT_TIMEOUT_FAILURES:
                metadata = getattr(result, "metadata", None) or {}
                budget = execution_budget(doc, stage)
                assignment.update({"state": "TIMED_OUT", "execution_budget_seconds": budget,
                                   "timeout_source": metadata.get("timeout_source"),
                                   "budget_derived": metadata.get("budget_derived") is True,
                                   "partial_changes": before != after})
                record_timeout(doc, stage, agent, model, budget, assignment["timeout_source"], assignment["finished_at"])
                if progress:
                    progress.attempt_timed_out(agent, stage, budget, before != after)
            if assignment["failure"] in WORKSPACE_TRUST_FAILURES:
                assignment["workspace"] = str(repo)
                agent_readiness.record_workspace_trust(agent, str(repo), stage, "orchestrate session")
            elif assignment["failure"] is None or assignment["failure"] in UNAVAILABLE_FAILURES | MODEL_LIMIT_FAILURES | {"EXECUTION_PERMISSION_REQUIRED"}:
                agent_readiness.record_session_outcome(agent, model, assignment["failure"],
                                                       detail=f"{result.error_message or ''}\n{result.stderr}")
            if assignment["state"] == "SUCCEEDED":
                record_capability_success(doc, agent)
                if stage == "planning":
                    doc["orchestrator"] = agent
                    doc["selected_orchestrator"] = agent
                    if progress and doc.get("requested_orchestrator") == "AUTO":
                        progress.selected_orchestrator(agent)
                if stage == "implementation":
                    doc["implementer"] = agent
                if stage == "review":
                    doc["audit"] = "CLEAN"
                if stage == "acceptance":
                    doc["acceptance"] = "ACCEPTED"
                if model != "UNKNOWN":
                    doc["known_models"].setdefault(agent, []).append(model)
                succeeded = True
                checkpoint(doc, path, token, repo)
                if progress:
                    progress.worker_complete(doc["id"][:8], agent, stage)
                break
            doc["reroutes"].append({"from": f"{agent}:{model}", "stage": stage, "reason": assignment["failure"]})
            if progress:
                progress._write("FAIL", f"{AGENT_NAMES.get(agent, agent)} {stage} failed: {assignment['failure']}{no_change_detail}")
            # Record the failure before choosing a replacement: the next
            # candidate list must already exclude what this attempt proved.
            exclusion = record_failure(doc, agent, stage, model, assignment["failure"])
            if progress:
                if assignment["failure"] in WORKSPACE_TRUST_FAILURES:
                    progress._write("TRUST", f"{AGENT_NAMES.get(agent, agent)} requires workspace trust for {repo}; "
                                             "rerouting (the agent stays eligible elsewhere)")
                elif assignment["failure"] == "EXECUTION_PERMISSION_REQUIRED":
                    progress.capability_downgrade(agent)
                elif exclusion:
                    progress.role_excluded(agent, stage, exclusion)
                if assignment["failure"] in MODEL_LIMIT_FAILURES:
                    progress.limit(agent, model, assignment["failure"])
                progress.checkpoint(doc["id"][:8])
            if before != after:
                doc["reconciliation"] = {"at": now(), "needs_validation": True, "from": agent, "stage": stage}
            if failure.value == "TRANSPORT_UNAVAILABLE" and transient_retries.get((agent, model), 0) < 1:
                transient_retries[(agent, model)] = transient_retries.get((agent, model), 0) + 1
                retry = (agent, model)
            elif doc["failover"] != "OFF":
                # Defer the progress REROUTE event until the next iteration so
                # the reported target is the actual replacement selected from
                # the updated eligible candidate list, not a stale intermediate.
                prev_reroute = (agent, assignment["failure"])
            checkpoint(doc, path, token, repo)
            if stage in {"review", "acceptance"} and before != after:
                doc["status"] = "BLOCKED"
                doc["audit"] = "AUDIT BLOCKED: read-only role changed the repository"
                checkpoint(doc, path, token, repo)
                return report(doc)
            if doc["failover"] == "OFF":
                break
        if not succeeded:
            doc["status"] = "BLOCKED" if stage == "review" else "HANDOFF REQUIRED"
            if stage == "review":
                doc["audit"] = "AUDIT BLOCKED: independent reviewers exhausted" if attempted else "AUDIT BLOCKED: no independent reviewer"
            doc["exclusions"] = {"stage": stage, "agents": exclusions(doc, stage)}
            stage_timeouts = [item for item in doc["timed_out_assignments"] if item["stage"] == stage]
            if stage_timeouts:
                doc["timeout_hint"] = (f"{len(stage_timeouts)} {stage} attempt(s) reached the {execution_budget(doc, stage)}s "
                                       "execution budget. Decompose the goal, or resume with "
                                       f"--execution-budget {stage}=<seconds> (max {MAX_EXECUTION_BUDGET_SECONDS}) "
                                       "or --retry-timeouts.")
            checkpoint(doc, path, token, repo)
            if progress:
                for item in doc["exclusions"]["agents"]:
                    progress.excluded(item["agent"], item["reason"])
                reason = doc.get("audit") or f"No eligible {stage} workers remain"
                if doc.get("timeout_hint"):
                    progress._write("TIMEOUT", doc["timeout_hint"])
                progress.blocked("AUDIT BLOCKED" if stage == "review" else "HANDOFF REQUIRED", reason, f"howlplane orchestrate resume --repo {repo}")
            return report(doc)
        if stage == "implementation":
            if progress:
                progress.validation(True, "Running repository diff validation")
            check = subprocess.run(["git", "-C", str(repo), "diff", "--check"], capture_output=True, text=True, timeout=30)
            doc["tests"].append({"command": "git diff --check", "exit_code": check.returncode, "output": redact(check.stderr[:500])})
            if progress:
                progress.validation(False, "Repository diff validation passed" if check.returncode == 0 else "Repository diff validation failed")
            if check.returncode:
                doc["status"] = "HANDOFF REQUIRED"
                checkpoint(doc, path, token, repo)
                return report(doc)
            if doc.get("verify_command"):
                command = doc["verify_command"]
                if progress:
                    progress.validation(True, f"Running configured validation: {' '.join(command)}")
                    with progress.waiting():
                        verified = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=300)
                else:
                    verified = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=300)
                doc["tests"].append({"command": command, "exit_code": verified.returncode, "output": redact((verified.stdout + verified.stderr)[-1000:])})
                if progress:
                    progress.validation(False, "Configured validation passed" if verified.returncode == 0 else "Configured validation failed")
                if verified.returncode:
                    doc["status"] = "HANDOFF REQUIRED"
                    checkpoint(doc, path, token, repo)
                    return report(doc)
                if doc.get("reconciliation"):
                    doc["reconciliation"]["needs_validation"] = False
            elif doc.get("reconciliation", {}).get("needs_validation"):
                doc["status"] = "HANDOFF REQUIRED"
                doc["validation_gap"] = "Partial or recovered changes require an explicit --verify command"
                checkpoint(doc, path, token, repo)
                return report(doc)
            checkpoint(doc, path, token, repo)
    doc["status"] = "COMPLETE WITH WARNINGS" if doc.get("reconciliation") else "COMPLETE"
    checkpoint(doc, path, token, repo)
    if progress:
        progress.complete(doc["status"])
    result = report(doc)
    if doc["status"] in {"COMPLETE", "COMPLETE WITH WARNINGS"} and not doc.get("retain_report"):
        with locked(path.parent):
            path.unlink(missing_ok=True)
    return result


def report(doc: dict[str, Any]) -> int:
    print("HOWL ORCHESTRATION REPORT")
    print(f"Session: {doc['id']}\nGoal: {doc['goal']}\nStatus: {doc['status']}")
    print(f"Orchestrator: {doc['orchestrator']}\nImplementer: {doc.get('implementer', 'none')}")
    print(f"Policy: {doc['policy']}\nConstraints: {', '.join(doc['constraints']) or 'none'}")
    print(f"Assignments: {len(doc['attempts'])}\nReroutes: {len(doc['reroutes'])}")
    print(f"Repository head: {doc['repository_evidence']['head']}")
    print(f"Repository status: {doc['repository_evidence']['status'] or 'clean'}")
    print(f"Tests: {json.dumps(doc['tests'])}")
    print(f"Independent audit: {doc.get('audit', 'completed' if doc['stage'] == 'review' and doc['status'] in TERMINAL else 'not requested or incomplete')}")
    print(f"Recovery: {json.dumps(doc.get('reconciliation', {}))}")
    if doc.get("validation_gap"):
        print(f"Validation gap: {doc['validation_gap']}")
    if doc.get("timed_out_assignments"):
        print(f"Timed-out assignments: {json.dumps([{key: item.get(key) for key in ('stage', 'agent', 'model', 'execution_budget_seconds')} for item in doc['timed_out_assignments']])}")
    if doc.get("timeout_hint") and doc["status"] in TERMINAL:
        print(f"Timeout guidance: {doc['timeout_hint']}")
    if doc.get("exclusions") and doc["status"] in TERMINAL:
        print(f"Excluded {doc['exclusions']['stage']} workers: {json.dumps(doc['exclusions']['agents'])}")
    print(f"Failures: {json.dumps([{'stage': a['stage'], 'agent': a['agent'], 'model': a['model'], 'failure': a.get('failure')} for a in doc['attempts'] if a.get('failure')])}")
    return 0 if doc["status"] in {"COMPLETE", "COMPLETE WITH WARNINGS"} else 2


def invalid_session_report(doc: Any, reason: str, repo: Path) -> int:
    # The manifest is left untouched: rewriting state that cannot be trusted
    # would destroy the only evidence of what the session did.
    session = doc.get("id", "unknown") if isinstance(doc, dict) else "unknown"
    print("HOWL ORCHESTRATION REPORT")
    print(f"Session: {redact(str(session))}\nStatus: SESSION_STATE_INVALID\nReason: {redact(reason)}")
    print(f"Next: howlplane orchestrate inspect --json --repo {repo}, then discard or repair the session")
    return 2


def command(args: argparse.Namespace) -> int:
    repo = Path(args.repo or os.getcwd()).resolve()
    root = state_root()
    with locked(root):
        active = active_sessions(root, repo)
        operation = args.input if args.input in {"resume", "inspect", "discard"} else None
        if active and operation is None and not args.separate and sys.stdin.isatty():
            choice = question("Unfinished session: Resume, Inspect, Discard, or Start separate", "Resume").lower()
            if choice == "start separate":
                args.separate = True
            elif choice in {"resume", "inspect", "discard"}:
                operation = choice
            else:
                raise ValueError("Unknown session action")
        if operation == "inspect":
            sessions = active_sessions(root, repo, include_terminal=True)
            print(json.dumps(sessions if args.json else sessions[:1], indent=2))
            return 0
        if operation == "discard":
            for doc in active:
                path_for(root, doc["id"]).unlink()
            print(f"Discarded {len(active)} active session(s)")
            return 0
        if operation == "resume":
            if not active:
                raise ValueError("No unfinished session")
            doc = active[0]
            try:
                migrations = normalize_session(doc)
            except SessionStateInvalid as error:
                return invalid_session_report(doc, str(error), repo)
            previous_orchestrator = doc["orchestrator"]
            if time.time() - doc["lease"]["renewed_at"] < 330 and doc["lease"]["pid"] != os.getpid():
                try:
                    os.kill(doc["lease"]["pid"], 0)
                except ProcessLookupError:
                    pass
                else:
                    raise ValueError("Session has a live coordinator lease")
            doc["lease"] = {"token": uuid.uuid4().hex, "pid": os.getpid(), "renewed_at": time.time()}
            reconcile(doc, repo)
            budget_change = parse_execution_budget(getattr(args, "execution_budget", None))
            if budget_change:
                doc["execution_budget"].update(budget_change)
                doc.setdefault("budget_changes", []).append({"budget": budget_change, "at": now()})
            if getattr(args, "retry_timeouts", False) and doc["timed_out_assignments"]:
                # An explicit operator retry: clear the ledger once, keep the record.
                doc.setdefault("timeout_retries", []).append({"cleared": doc["timed_out_assignments"], "at": now()})
                doc["timed_out_assignments"] = []
            doc.pop("timeout_hint", None)
            requested_orchestrator = getattr(args, "orchestrator", None)
            if requested_orchestrator and requested_orchestrator != "AUTO" and doc["agents"].get(requested_orchestrator, {}).get("state") in {"AVAILABLE", "DEGRADED"}:
                doc["requested_orchestrator"] = requested_orchestrator
                doc["selected_orchestrator"] = requested_orchestrator
                doc["orchestrator"] = requested_orchestrator
            path = path_for(root, doc["id"])
            secure_write(path, doc)
            if doc["policy"] == "PLAN ONLY" and doc.get("reconciliation", {}).get("needs_validation"):
                doc["status"] = "BLOCKED"
                save(path, doc, doc["lease"]["token"])
                return report(doc)
        else:
            if active:
                if args.separate:
                    raise ValueError("Separate sessions need a distinct Git worktree passed with --repo; overlapping worktree ownership is refused")
                raise ValueError("Unfinished session exists. Use orchestrate resume, inspect, discard, or a separate Git worktree")
            doc = setup(args, repo)
            doc["retain_report"] = args.retain_report
            path = path_for(root, doc["id"])
            save(path, doc, doc["lease"]["token"])
    progress = SessionProgress(
        doc,
        stream=sys.stderr,
        heartbeat_interval=getattr(args, "heartbeat", 30.0),
        enabled=not (getattr(args, "quiet", False) or getattr(args, "no_progress", False) or getattr(args, "json", False)),
    )
    if operation == "resume":
        progress._write("RESUME", f"Loading orchestration session {doc['id'][:8]}")
        for note in migrations:
            progress._write("MIGRATE", note)
        progress._write("RECONCILE", "Checking repository and worker state")
        if doc["orchestrator"] != previous_orchestrator:
            progress._write("TAKEOVER", f"{AGENT_NAMES.get(doc['orchestrator'], doc['orchestrator'])} is now orchestrator")
    else:
        progress.session_started()
    try:
        return run(doc, path, repo, progress)
    except KeyboardInterrupt:
        progress._write("INTERRUPT", "Stopping new dispatches and checkpointing session...")
        doc["status"] = "INTERRUPTED"
        checkpoint(doc, path, doc["lease"]["token"], repo)
        progress._write("CHECKPOINT", f"Session can be resumed with: howlplane orchestrate resume --repo {repo}")
        return 130


def add_parser(subparsers: Any, common_parser: Any) -> None:
    parser = subparsers.add_parser("orchestrate", parents=[common_parser], help="Run a session-scoped agent workflow")
    parser.add_argument("input", nargs="?", help="Goal, resume, inspect, or discard")
    parser.add_argument("--json", action="store_true", help="Machine-readable session inspection")
    parser.add_argument("--quiet", action="store_true", help="Only print the final report and errors")
    parser.add_argument("--no-progress", action="store_true", help="Disable task-level progress and local heartbeats")
    parser.add_argument("--heartbeat", type=float, default=30.0, metavar="SECONDS", help="Heartbeat interval during otherwise silent work (default: 30)")
    parser.add_argument("--separate", action="store_true", help="Start a separate session alongside an unfinished one")
    parser.add_argument("--retain-report", action="store_true")
    parser.add_argument("--orchestrator", choices=["AUTO", *AGENTS])
    for agent in AGENTS:
        parser.add_argument(f"--{agent.replace('_', '-')}", choices=["AUTO", "RESERVED", "UNAVAILABLE"])
    parser.add_argument("--strategy", choices=["BALANCED", "ECONOMY", "QUALITY"])
    parser.add_argument("--models", help="Comma separated role:agent:model selections")
    parser.add_argument("--fallbacks", help="Comma separated ordered role:agent:model alternatives")
    parser.add_argument("--failover", choices=["AUTO REROUTE", "OFF"])
    parser.add_argument("--policy", choices=["PLAN + EXECUTE + INDEPENDENT AUDIT", "PLAN + EXECUTE", "PLAN ONLY"])
    parser.add_argument("--constraint", action="append", default=[])
    parser.add_argument("--verify", nargs="+", help="Explicit verification command and arguments")
    parser.add_argument("--execution-budget", action="append", metavar="ROLE=SECONDS",
                        help=f"Per-assignment deadline for a role, or SECONDS for every role (default "
                             f"{DEFAULT_EXECUTION_BUDGET_SECONDS}, max {MAX_EXECUTION_BUDGET_SECONDS}; repeatable). "
                             "This is HowlPlane's own time limit, not provider quota")
    parser.add_argument("--retry-timeouts", action="store_true",
                        help="On resume, allow assignments that hit their execution budget to run once more unchanged")

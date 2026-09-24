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

from howlplane.control_plane.agent_execution import AgentBackendRegistry
from howlplane.control_plane.atomic_io import safe_load_json
from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager
from howlplane.control_plane.task_spec import TaskSpec

SCHEMA = "howlplane.orchestration/v1"
AGENTS = ("claude_code", "codex", "cursor", "agy", "devin_cli")
BINARIES = {"claude_code": "claude", "codex": "codex", "cursor": "agent", "agy": "agy", "devin_cli": "devin"}
TERMINAL = {"COMPLETE", "COMPLETE WITH WARNINGS", "BLOCKED", "HANDOFF REQUIRED"}
SECRET = re.compile(r"(?i)(bearer\s+|(?:token|password|secret|api[_-]?key)[=: ]+)([^\s,;]+)|\b(?:sk-|ghp_|gho_|github_pat_)[\w-]{8,}")


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

    def route_skip(self, agent: str) -> None:
        self._write("ROUTE", f"{AGENT_NAMES.get(agent, agent)} skipped: unattended execution unavailable")

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
        if doc.get("schema") == SCHEMA and doc.get("repository") == str(repo) and (include_terminal or doc.get("status") not in TERMINAL):
            sessions.append(doc)
    return sorted(sessions, key=lambda item: item["created_at"], reverse=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def discover_models(agent: str) -> list[str]:
    """Read only advertised lists. A silent CLI default remains UNKNOWN."""
    commands = {
        "cursor": ["agent", "--list-models"],
        "agy": ["agy", "models"],
        "devin_cli": ["devin", "models", "list"],
    }
    command = commands.get(agent)
    if not command or not shutil.which(command[0]):
        return []
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    models = []
    for line in result.stdout.splitlines():
        candidate = line.strip().split()[0] if line.strip() else ""
        if candidate == "auto":
            continue
        if re.fullmatch(r"[\w][\w.:-]{1,79}", candidate) and candidate.lower() not in {"model", "models", "name", "available"}:
            models.append(candidate)
    return list(dict.fromkeys(models))


def inventory(availability: dict[str, str]) -> dict[str, dict[str, Any]]:
    found = {}
    for agent in AGENTS:
        installed = shutil.which(BINARIES[agent]) is not None
        requested = availability.get(agent, "AUTO").upper()
        found[agent] = {
            "backend": BINARIES[agent],
            "installed": installed,
            "callable": installed,
            "state": "RESERVED" if requested == "RESERVED" else "AVAILABLE" if installed and requested != "UNAVAILABLE" else "UNAVAILABLE",
            "models": discover_models(agent) if installed and requested != "RESERVED" else [],
            "capabilities": {
                "unattended_execution": None,
                "reason": "CLI metadata does not confirm unattended execution",
                "evidence_time": None,
                "scope": "session",
            },
        }
    return found


def record_capability_failure(doc: dict[str, Any], agent: str, reason: str) -> None:
    state = doc["agents"][agent]
    state["state"] = "DEGRADED"
    state["capabilities"]["unattended_execution"] = False
    state["capabilities"]["reason"] = reason
    state["capabilities"]["evidence_time"] = now()
    state["capabilities"]["scope"] = "session"


def record_capability_success(doc: dict[str, Any], agent: str) -> None:
    state = doc["agents"][agent]
    state["state"] = "AVAILABLE"
    state["capabilities"]["unattended_execution"] = True
    state["capabilities"]["reason"] = "Successful unattended session invocation"
    state["capabilities"]["evidence_time"] = now()
    state["capabilities"]["scope"] = "session"


def capability_skip_reason(doc: dict[str, Any], agent: str, role: str) -> str | None:
    state = doc["agents"][agent]
    if state["state"] == "RESERVED":
        return "reserved"
    capability = state.get("capabilities", {}).get("unattended_execution")
    if capability is not False:
        return None
    if doc.get("requested_orchestrator") == agent:
        return "explicit override"
    return "unattended execution unavailable"


def candidates(doc: dict[str, Any], role: str) -> list[tuple[str, str]]:
    agents = doc["agents"]
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
            if doc["model_states"].get(f"{agent}:{model}") != "EXHAUSTED":
                selected.append((agent, model))
    fallback = [] if role == "acceptance" else doc.get("fallbacks", {}).get(role, [])
    if fallback:
        for item in fallback:
            agent, model = item.split(":", 1)
            if doc["agents"][agent]["state"] == "AVAILABLE" and (
                model == "UNKNOWN" or model in doc["agents"][agent]["models"]
                or model in doc.get("known_models", {}).get(agent, [])
            ) and doc["model_states"].get(item) != "EXHAUSTED":
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
    snapshot = evidence(repo)
    token = uuid.uuid4().hex
    agents = inventory(availability)
    if lead != "AUTO" and agents[lead]["state"] != "AVAILABLE":
        raise ValueError("Selected orchestrator is not available for this session")
    return {
        "schema": SCHEMA, "id": uuid.uuid4().hex, "created_at": now(), "repository": snapshot["root"],
        "goal": redact(goal), "constraints": [redact(item) for item in args.constraint],
        "requested_orchestrator": lead, "selected_orchestrator": None if lead == "AUTO" else lead,
        "orchestrator": lead, "agents": agents, "strategy": strategy,
        "role_models": role_models, "fallbacks": fallbacks,
        "failover": failover, "policy": policy, "status": "PLANNED", "stage": "planning",
        "model_states": {}, "known_models": {}, "attempts": [], "tests": [], "reroutes": [],
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
    if model != "UNKNOWN":
        instructions += f" Use model {model} if the CLI supports selecting it; report actual model identity."
    task = TaskSpec(task_id=doc["id"], repository=str(repo), objective=doc["goal"], constraints=doc["constraints"])
    backend = AgentBackendRegistry.get_backend(agent)
    return backend.execute(task, repo, role=role, prompt_override=instructions, timeout_seconds=300, model_id=model)


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
                notice = f"{candidate}:{reason}"
                if reason == "unattended execution unavailable" and notice not in notices:
                    progress.route_skip(candidate)
                    notices.append(notice)
                elif reason == "explicit override" and notice not in notices:
                    progress.explicit_override_warning(candidate)
                    notices.append(notice)
        options = candidates(doc, stage)
        if stage == "review":
            options = [pair for pair in options if pair[0] != doc.get("implementer")]
        if not options:
            doc["status"] = "BLOCKED" if stage == "review" else "HANDOFF REQUIRED"
            doc["audit"] = "AUDIT BLOCKED: no independent reviewer" if stage == "review" else None
            checkpoint(doc, path, token, repo)
            if progress:
                progress.blocked("AUDIT BLOCKED" if stage == "review" else "HANDOFF REQUIRED", doc.get("audit") or "No eligible worker remains", f"howlplane orchestrate resume --repo {repo}")
            return report(doc)
        succeeded = False
        transient_retries: dict[tuple[str, str], int] = {}
        prev_reroute: tuple[str, str] | None = None
        for agent, model in options:
            if doc["agents"][agent]["state"] not in {"AVAILABLE", "DEGRADED"}:
                continue
            skip = capability_skip_reason(doc, agent, stage)
            if skip and skip != "explicit override":
                continue
            if prev_reroute and progress:
                source_agent, reason = prev_reroute
                progress.reroute(doc["id"][:8], source_agent, agent, reason)
                prev_reroute = None
            before = evidence(repo)
            assignment = {"task_id": doc["id"][:8], "stage": stage, "agent": agent, "model": model, "started_at": now(), "fence": token, "state": "ASSIGNED"}
            doc["attempts"].append(assignment)
            checkpoint(doc, path, token, repo)
            if progress:
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
            if assignment["failure"] == "EXECUTION_PERMISSION_REQUIRED":
                record_capability_failure(doc, agent, assignment["failure"])
                if progress:
                    progress.capability_downgrade(agent)
            elif assignment["failure"] in {
                "ENGINEERING_FAILURE", "NO_REPOSITORY_CHANGE", "MALFORMED_OUTPUT",
                "CAPABILITY_FAILURE", "POLICY_FAILURE", "VERIFICATION_FAILURE",
                "EXECUTION_BUDGET_EXCEEDED", "PROVIDER_STALLED", "MISSING_EXECUTABLE",
                "AUTHENTICATION_REQUIRED", "PROVIDER_UNAVAILABLE",
                "READ_ONLY_ROLE_MUTATED_REPOSITORY", "AUDIT_FINDINGS_OR_UNCONFIRMED",
                "ACCEPTANCE_REJECTED_OR_UNCONFIRMED",
            }:
                # Session-scoped degradation so AUTO routing does not burn
                # another attempt on a backend that just demonstrated it cannot
                # perform this role. A new session starts with fresh evidence.
                if assignment["failure"] in {"MISSING_EXECUTABLE", "AUTHENTICATION_REQUIRED", "PROVIDER_UNAVAILABLE"}:
                    doc["agents"][agent]["state"] = "UNAVAILABLE"
                else:
                    record_capability_failure(doc, agent, assignment["failure"])
            if progress:
                if assignment["failure"] in {"QUOTA_EXHAUSTED", "SESSION_LIMIT", "RATE_LIMITED"}:
                    progress.limit(agent, model, assignment["failure"])
                progress.checkpoint(doc["id"][:8])
            if failure.value in {"QUOTA_EXHAUSTED", "SESSION_LIMIT", "RATE_LIMITED"}:
                doc["model_states"][f"{agent}:{model}"] = "EXHAUSTED"
                if model == "UNKNOWN":
                    doc["agents"][agent]["state"] = "UNAVAILABLE"
            if before != after:
                doc["reconciliation"] = {"at": now(), "needs_validation": True, "from": agent, "stage": stage}
            if failure.value == "TRANSPORT_UNAVAILABLE" and transient_retries.get((agent, model), 0) < 1:
                transient_retries[(agent, model)] = transient_retries.get((agent, model), 0) + 1
                options.insert(options.index((agent, model)) + 1, (agent, model))
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
                doc["audit"] = "AUDIT BLOCKED: independent reviewers exhausted"
            checkpoint(doc, path, token, repo)
            if progress:
                progress.blocked("AUDIT BLOCKED" if stage == "review" else "HANDOFF REQUIRED", doc.get("audit") or "Eligible workers exhausted", f"howlplane orchestrate resume --repo {repo}")
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
    print(f"Failures: {json.dumps([{'stage': a['stage'], 'agent': a['agent'], 'model': a['model'], 'failure': a.get('failure')} for a in doc['attempts'] if a.get('failure')])}")
    return 0 if doc["status"] in {"COMPLETE", "COMPLETE WITH WARNINGS"} else 2


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

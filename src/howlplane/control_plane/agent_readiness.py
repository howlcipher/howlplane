"""Provider-neutral agent CLI readiness: what each CLI can verifiably do, kept apart from its capacity.

Three evidence levels, each independently cached:

* Level 1 (local, free): executable, version, authentication status, advertised
  models. No prompt is ever sent.
* Level 2 (bounded live smoke, opt-in): one tiny non-mutating prompt through
  HowlPlane's *own* backend invocation, in an empty temporary directory, so a
  pass proves HowlPlane can drive the CLI unattended -- not merely that the CLI
  works interactively.
* Level 3 (capacity): only what a CLI actually reports in structured output, or
  what a real invocation proved (quota, session, or rate limit). A CLI that does
  not expose its allowance stays UNKNOWN; numbers are never derived or guessed.

An execution-budget timeout is attempt evidence and never becomes capacity here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from howlplane.control_plane import workspace_trust
from howlplane.control_plane.atomic_io import load_schema_section, private_state_path, write_private_json
from howlplane.control_plane.config_loader import ProviderPolicySettings

SCHEMA = "howlplane.agent_readiness/v1"
SMOKE_TOKEN = "HOWL_READY"
SMOKE_PROMPT = f"Respond exactly: {SMOKE_TOKEN}"
DEFAULT_SMOKE_TIMEOUT_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 15
# Separate lifetimes: static facts change on upgrade, a smoke only proves "now".
TTL_SECONDS = {"local": 24 * 3600, "auth": 3600, "unattended": 24 * 3600, "live_smoke": 1800, "capacity": 600}
_POLICY = ProviderPolicySettings()
# Hard limits last until the provider's reported reset, else these defaults
# (the provider pool's own cooldowns), never permanently.
LIMIT_SECONDS = {
    "QUOTA_EXHAUSTED": _POLICY.quota_cooldown_seconds,
    "SESSION_EXHAUSTED": _POLICY.session_cooldown_seconds,
    "RATE_LIMITED": _POLICY.cooldown_seconds,
}
FAILURE_TO_CAPACITY = {"QUOTA_EXHAUSTED": "QUOTA_EXHAUSTED", "SESSION_LIMIT": "SESSION_EXHAUSTED", "RATE_LIMITED": "RATE_LIMITED"}
EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
# Cursor and Devin refuse an untrusted directory until it is trusted. That is a
# fact about the directory, not the agent: a smoke in a fresh directory proves
# only that new workspaces need preparing first. The refusal markers live in
# one place, `workspace_trust.ADAPTERS`.


def is_workspace_trust_refusal(text: str, agent: str | None = None) -> bool:
    agents = [agent] if agent else list(workspace_trust.ADAPTERS)
    return any(workspace_trust.is_trust_refusal(name, text) for name in agents)


# Some CLIs (Cursor's `agent`) colorize output even when it is piped.
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_seconds(value: Any, at: datetime | None = None) -> float | None:
    moment = parse_time(value)
    return None if moment is None else ((at or now()) - moment).total_seconds()


def fresh(section: dict[str, Any] | None, key: str, at: datetime | None = None) -> bool:
    age = age_seconds((section or {}).get("observed_at"), at)
    return age is not None and 0 <= age < TTL_SECONDS[key]


def scrub(value: Any, limit: int = 200) -> str:
    from howlplane.control_plane.orchestration import redact
    text = EMAIL.sub("<redacted>", redact(str(value))).replace("\n", " ").strip()
    return text[:limit]


# ---------------------------------------------------------------- level 1 parsers
def parse_version(output: str) -> str | None:
    for line in output.splitlines():
        match = re.search(r"\d+(?:\.\d+)+(?:[-+][\w.]+)?", line)
        if match:
            return match.group(0)
    return None


def _auth_from_text(exit_code: int, output: str) -> tuple[bool | None, str]:
    lowered = output.lower()
    if re.search(r"not (?:logged|signed) in|unauthenticated|please (?:log|sign) in", lowered):
        return False, "CLI reports it is not logged in"
    if exit_code == 0 and re.search(r"\blogged in\b|signed in", lowered):
        return True, "CLI reports an active login"
    return None, "CLI auth status was not conclusive"


def parse_claude_auth(exit_code: int, output: str) -> tuple[bool | None, str]:
    try:
        document = json.loads(output)
    except ValueError:
        return _auth_from_text(exit_code, output)
    logged_in = document.get("loggedIn") if isinstance(document, dict) else None
    if not isinstance(logged_in, bool):
        return None, "CLI auth status was not conclusive"
    method = document.get("authMethod")
    return logged_in, f"CLI reports {'an active' if logged_in else 'no'} login" + (f" ({scrub(method, 40)})" if logged_in and method else "")


def parse_codex_models(output: str) -> list[str]:
    try:
        document = json.loads(output)
    except ValueError:
        return []
    items = document.get("models", []) if isinstance(document, dict) else []
    return [item["slug"] for item in items if isinstance(item, dict) and isinstance(item.get("slug"), str)]


def _matching(output: str, pattern: str) -> list[str]:
    found = [match.group(1) for match in re.finditer(pattern, output, re.MULTILINE)]
    return list(dict.fromkeys(model for model in found if model != "auto"))


def parse_cursor_models(output: str) -> list[str]:
    # `agent --list-models`: "<id> - <display name>" beneath a heading line.
    return _matching(output, r"^([\w][\w.:\[\]=,-]*) - \S")


def parse_agy_models(output: str) -> list[str]:
    # `agy models`: "<id>\t<display name>" after a "Fetching..." banner.
    return _matching(output, r"^([\w][\w.:-]*)\t\S")


def parse_devin_models(output: str) -> list[str]:
    # `devin models list`: unindented family headers, "  aliases: x" lines, and
    # "  <id>   <display name> [...]" model rows.
    return _matching(output, r"^ {2}([a-z0-9][\w.:-]*) {2,}\S")


def codex_default_model() -> tuple[str | None, str | None]:
    import tomllib
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        config = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    model = config.get("model")
    return (model, "codex config.toml") if isinstance(model, str) and model else (None, None)


@dataclass(frozen=True)
class AgentProbeSpec:
    agent: str
    name: str
    binary: str
    version_argv: tuple[str, ...]
    auth_argv: tuple[str, ...] | None
    parse_auth: Callable[[int, str], tuple[bool | None, str]] | None
    models_argv: tuple[str, ...] | None
    parse_models: Callable[[str], list[str]] | None
    # Route with the advertised list, or leave the model to the CLI's default.
    route_listed_models: bool = False
    # A successful authenticated listing is the only auth signal the CLI offers.
    auth_from_models: bool = False
    default_model: Callable[[], tuple[str | None, str | None]] | None = None


# Every argv below was observed on the installed CLIs; none sends a prompt.
SPECS: dict[str, AgentProbeSpec] = {
    "codex": AgentProbeSpec("codex", "Codex", "codex", ("codex", "--version"), ("codex", "login", "status"),
                            _auth_from_text, ("codex", "debug", "models"), parse_codex_models,
                            default_model=codex_default_model),
    "claude_code": AgentProbeSpec("claude_code", "Claude Code", "claude", ("claude", "--version"),
                                  ("claude", "auth", "status"), parse_claude_auth, None, None),
    # Cursor's list is reported, but AUTO keeps its `auto` default: the list's
    # order is not a preference, and routing never pinned a Cursor model before.
    "cursor": AgentProbeSpec("cursor", "Cursor", "agent", ("agent", "--version"), ("agent", "status"),
                             _auth_from_text, ("agent", "--list-models"), parse_cursor_models),
    "agy": AgentProbeSpec("agy", "AGY", "agy", ("agy", "--version"), None, None, ("agy", "models"), parse_agy_models,
                          route_listed_models=True, auth_from_models=True),
    "devin_cli": AgentProbeSpec("devin_cli", "Devin", "devin", ("devin", "version"), ("devin", "auth", "status"),
                                _auth_from_text, ("devin", "models", "list"), parse_devin_models, route_listed_models=True),
}
AGENT_ORDER = ("codex", "claude_code", "cursor", "agy", "devin_cli")


def run_probe(argv: Iterable[str], timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[int, str]:
    try:
        completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                                   stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return -1, "probe timed out"
    except OSError as error:
        return 127, str(error)
    return completed.returncode, ANSI.sub("", completed.stdout + ("\n" + completed.stderr if completed.stderr else ""))


def hosted_probes_allowed() -> bool:
    """Auth and model listings may contact the provider; `local_only` forbids that egress."""
    try:
        from howlplane.control_plane.config_loader import is_local_only
        return not is_local_only()
    except Exception:
        return False


LOCAL_ONLY_DETAIL = "not probed: local_only operating mode forbids hosted requests"


def local_fresh(spec: AgentProbeSpec, local: dict[str, Any] | None, at: datetime, hosted: bool = True) -> bool:
    """Cached local facts hold only while the same executable is still what PATH resolves."""
    local = local or {}
    return (fresh(local, "local", at) and local.get("executable") == shutil.which(spec.binary)
            and local.get("hosted", True) == hosted)


def probe_local(spec: AgentProbeSpec, at: datetime, hosted: bool = True) -> dict[str, Any]:
    path = shutil.which(spec.binary)
    section: dict[str, Any] = {"observed_at": iso(at), "installed": path is not None, "executable": path,
                               "version": None, "models": {"status": "NOT_INSTALLED", "items": [], "default": None,
                                                           "default_source": None}, "mutation_capable": None,
                               "hosted": hosted}
    if not path:
        return section
    code, output = run_probe(spec.version_argv)
    section["version"] = parse_version(output) if code == 0 else None
    models = section["models"]
    if not hosted:
        models.update({"status": "NOT_PROBED", "detail": LOCAL_ONLY_DETAIL})
    elif spec.models_argv and spec.parse_models:
        code, output = run_probe(spec.models_argv)
        items = spec.parse_models(output) if code == 0 else []
        models.update({"status": "LISTED" if items else "FAILED", "items": items})
        section["models_exit_code"] = code
        if code:
            section["models_error"] = scrub(output)
    else:
        models["status"] = "UNSUPPORTED"
    if spec.default_model:
        models["default"], models["default_source"] = spec.default_model()
    section["mutation_capable"] = _mutation_capable(spec.agent)
    return section


def _mutation_capable(agent: str) -> bool | None:
    """The backend's own free, local verdict on whether its invocation may edit files."""
    try:
        from howlplane.control_plane.agent_execution import AgentBackendRegistry
        return AgentBackendRegistry.get_backend(agent).probe_readiness().unattended_mutation_capable
    except Exception:
        return None


def probe_auth(spec: AgentProbeSpec, local: dict[str, Any], at: datetime, hosted: bool = True) -> dict[str, Any]:
    section: dict[str, Any] = {"observed_at": iso(at), "authenticated": None, "source": None, "detail": None}
    if not local.get("installed"):
        section["detail"] = "executable not installed"
        return section
    if not hosted:
        section["detail"] = LOCAL_ONLY_DETAIL
        return section
    if spec.auth_argv and spec.parse_auth:
        code, output = run_probe(spec.auth_argv)
        section["authenticated"], section["detail"] = spec.parse_auth(code, output)
        section["source"] = " ".join(spec.auth_argv)
    elif spec.auth_from_models:
        listed = local["models"]["status"] == "LISTED"
        failure_text = (local.get("models_error") or "").lower()
        section["authenticated"] = True if listed else (False if re.search(r"log ?in|auth|credential", failure_text) else None)
        section["source"] = " ".join(spec.models_argv or ())
        section["detail"] = ("inferred: the account's model list was fetched" if listed
                             else "CLI has no auth status command; model listing was not conclusive")
    else:
        section["detail"] = "CLI exposes no auth status command"
    return section


# ---------------------------------------------------------------- level 2 / 3
def parse_reported_model(text: str) -> str | None:
    match = re.search(r"(?mi)^\s*model:\s*([\w.:\[\]=,/-]+)", text)
    return match.group(1) if match else None


def parse_capacity_windows(text: str) -> dict[str, Any] | None:
    """Structured usage windows, only when a CLI emits them (e.g. a `rate_limits` event).

    Returns remaining percentages keyed by window length; nothing is inferred
    from elapsed time or from failures.
    """
    for line in reversed(text.splitlines()):
        line = line.strip()
        if "rate_limits" not in line or not line.startswith("{"):
            continue
        try:
            document = json.loads(line)
        except ValueError:
            continue
        limits = _find_key(document, "rate_limits")
        if not isinstance(limits, dict):
            continue
        found: dict[str, Any] = {}
        for window in limits.values():
            if not isinstance(window, dict) or not isinstance(window.get("used_percent"), (int, float)):
                continue
            remaining = max(0.0, 100.0 - float(window["used_percent"]))
            minutes = window.get("window_minutes")
            if minutes == 300:
                found["five_hour_remaining_percent"] = remaining
            elif minutes == 10080:
                found["weekly_remaining_percent"] = remaining
            reset = window.get("resets_at")
            if isinstance(reset, (int, float)):
                found.setdefault("reset_at", iso(datetime.fromtimestamp(reset, timezone.utc)))
        if found:
            return found
    return None


def _find_key(document: Any, key: str) -> Any:
    if isinstance(document, dict):
        if key in document:
            return document[key]
        for value in document.values():
            hit = _find_key(value, key)
            if hit is not None:
                return hit
    return None


def run_smoke(agent: str, timeout: int, at: datetime, workspace: str | None = None) -> dict[str, Any]:
    """One bounded, non-mutating invocation through HowlPlane's real backend path.

    Without a workspace it runs in a fresh temporary directory (global
    readiness). With one it runs read-only in that directory, proving whether
    the agent can work there unattended.
    """
    from howlplane.control_plane.agent_execution import AgentBackendRegistry
    from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager
    from howlplane.control_plane.task_spec import TaskSpec

    backend = AgentBackendRegistry.get_backend(agent)

    def invoke(directory: str) -> tuple[Any, float]:
        task = TaskSpec(task_id="agent-doctor-smoke", repository=directory, objective="Readiness smoke test")
        started = time.monotonic()
        outcome = backend.execute(task, directory, role="review", prompt_override=SMOKE_PROMPT, timeout_seconds=timeout)
        return outcome, round(time.monotonic() - started, 2)

    if workspace:
        result, latency = invoke(workspace)
    else:
        with tempfile.TemporaryDirectory(prefix="howl-smoke-") as scratch:
            result, latency = invoke(scratch)
    text = f"{result.stdout}\n{result.stderr}"
    failure = None if result.success else ProviderPoolManager.classify_result(agent, result).value
    if result.success and SMOKE_TOKEN in result.stdout:
        status = "PASS"
    elif result.success:
        status, failure = "FAIL", "MALFORMED_OUTPUT"
    elif failure == "WORKSPACE_TRUST_REQUIRED" or is_workspace_trust_refusal(text, agent):
        status, failure = "WORKSPACE_TRUST_REQUIRED", "WORKSPACE_TRUST_REQUIRED"
    elif failure == "EXECUTION_PERMISSION_REQUIRED":
        status = "BLOCKED_PERMISSION"
    elif failure == "EXECUTION_BUDGET_EXCEEDED":
        status = "TIMED_OUT"
    else:
        status = "FAIL"
    return {"observed_at": iso(at), "status": status, "exit_code": result.exit_code, "latency_seconds": latency,
            "failure_class": failure, "model_reported": parse_reported_model(text),
            "capacity_windows": parse_capacity_windows(text),
            "error": scrub(result.stderr.strip() or result.error_message or "") if status != "PASS" else None}


# ---------------------------------------------------------------- cache
def cache_path() -> Path:
    return private_state_path("HOWLPLANE_AGENT_READINESS_FILE", "agent_readiness.json")


def load_cache() -> dict[str, Any]:
    return load_schema_section(cache_path(), SCHEMA, "agents")


def save_cache(agents: dict[str, Any]) -> None:
    write_private_json(cache_path(), {"schema": SCHEMA, "agents": agents})


def _update_cache(agent: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    try:
        agents = load_cache()
        mutate(agents.setdefault(agent, {}))
        save_cache(agents)
    except OSError:
        # Readiness evidence is advisory; failing to persist it must never
        # break the orchestration that produced it.
        pass


def limit_entry(state: str, scope: str, model: str | None, source: str, at: datetime,
                reset_at: str | None = None, reason: str | None = None) -> dict[str, Any]:
    until = parse_time(reset_at) or at + timedelta(seconds=LIMIT_SECONDS.get(state, _POLICY.cooldown_seconds))
    return {"state": state, "scope": scope, "model": model, "source": source, "reason": reason,
            "observed_at": iso(at), "expires_at": iso(until)}


def record_session_outcome(agent: str, model: str, failure: str | None, at: datetime | None = None,
                           detail: str = "") -> None:
    """Feed a real orchestrate outcome back as readiness evidence, at the scope it proves."""
    if agent not in SPECS or failure == "WORKSPACE_TRUST_REQUIRED" or (
            failure == "EXECUTION_PERMISSION_REQUIRED" and is_workspace_trust_refusal(detail, agent)):
        # An untrusted directory says nothing about the agent in trusted ones.
        return
    moment = at or now()

    def mutate(record: dict[str, Any]) -> None:
        if failure is None:
            record["unattended"] = {"observed_at": iso(moment), "value": True, "source": "orchestrate session"}
        elif failure == "EXECUTION_PERMISSION_REQUIRED":
            record["unattended"] = {"observed_at": iso(moment), "value": False, "source": "orchestrate session"}
        elif failure == "AUTHENTICATION_REQUIRED":
            record["auth"] = {"observed_at": iso(moment), "authenticated": False, "source": "orchestrate session",
                              "detail": "a real invocation required authentication"}
        elif failure in FAILURE_TO_CAPACITY:
            scoped_model = None if model in ("", "UNKNOWN") else model
            limits = [item for item in record.get("limits", []) if item.get("model") != scoped_model]
            limits.append(limit_entry(FAILURE_TO_CAPACITY[failure], "model" if scoped_model else "agent", scoped_model,
                                      "orchestrate session", moment, reason=failure))
            record["limits"] = limits
        # EXECUTION_BUDGET_EXCEEDED and every other failure prove nothing about capacity.

    _update_cache(agent, mutate)


def record_workspace_trust(agent: str, workspace: str, role: str, source: str, at: datetime | None = None) -> None:
    """A CLI refused one directory as untrusted: recorded against that directory only.

    Only the facts needed to act on it are kept (never output or transcripts).
    """
    if agent not in SPECS:
        return
    moment = at or now()

    def mutate(record: dict[str, Any]) -> None:
        record.setdefault("workspaces", {})[str(workspace)] = {
            "state": workspace_trust.TRUST_REQUIRED, "role": role, "source": source, "detected_at": iso(moment)}

    _update_cache(agent, mutate)


def clear_workspace_refusal(agent: str, workspace: str) -> None:
    """Preparation re-verified trust for this directory; an older refusal no longer applies."""
    _update_cache(agent, lambda record: (record.get("workspaces") or {}).pop(str(workspace), None))


def workspace_status(agent: str, workspace: str | Path, live: bool = False,
                     cache: dict[str, Any] | None = None) -> dict[str, Any]:
    """One agent's readiness for one directory: vendor trust plus any refusal already observed there.

    A refusal the CLI itself returned outranks the vendor store until preparation
    or a passing workspace smoke clears it, so a wrong trust model can never
    re-dispatch into a folder that already refused.
    """
    path = str(Path(workspace).expanduser().resolve())
    hosted = hosted_probes_allowed()
    trust = (workspace_trust.probe(agent, path) if live and hosted and agent in workspace_trust.PROBE_ARGV
             else workspace_trust.check(agent, path))
    record = (cache if cache is not None else load_cache()).get(agent) or {}
    refusal = (record.get("workspaces") or {}).get(path)
    smoke = (record.get("workspace_smokes") or {}).get(path)
    state = workspace_trust.TRUST_REQUIRED if refusal else trust["state"]
    return {"agent": agent, "workspace": path, "state": state, "ready": state == workspace_trust.READY,
            "trust": trust, "observed_refusal": refusal,
            "live_smoke": None if not smoke else {"status": smoke.get("status"), "verified_at": smoke.get("observed_at"),
                                                  "failure_class": smoke.get("failure_class")}}


def workspace_blocked(agent: str, workspace: str | Path | None) -> str | None:
    """Routing view: why `agent` must not be dispatched into `workspace`, or None."""
    if not workspace or agent not in workspace_trust.ADAPTERS:
        return None
    try:
        status = workspace_status(agent, workspace)
    except OSError:
        return None
    if status["state"] != workspace_trust.TRUST_REQUIRED:
        return None
    source = "refused earlier" if status["observed_refusal"] else status["trust"]["detail"]
    return f"workspace trust required for {status['workspace']} ({source})"


def workspace_report(workspace: str | Path, agents: Iterable[str] | None = None, live: bool = False) -> dict[str, Any]:
    """Workspace readiness for the doctor and Factory preflight. Never sends a model prompt."""
    path = Path(workspace).expanduser().resolve()
    selected = [agent for agent in AGENT_ORDER if agents is None or agent in set(agents)]
    cache = load_cache()
    scope = workspace_trust.authorized_scope_for(path)
    return {"workspace": str(path), "exists": path.is_dir(), "authorized": scope is not None,
            "scope": None if scope is None else {
                key: scope.get(key) for key in ("repo_root", "factory_root", "authorized_at", "strategy",
                                                        "last_verified_at")},
            "agents": {agent: workspace_status(agent, path, live, cache) for agent in selected}}


def active_limits(record: dict[str, Any], at: datetime | None = None) -> list[dict[str, Any]]:
    moment = at or now()
    return [item for item in record.get("limits", [])
            if (parse_time(item.get("expires_at")) or moment) > moment]


# ---------------------------------------------------------------- evaluation
def summarize(agent: str, record: dict[str, Any], at: datetime | None = None) -> dict[str, Any]:
    """The routing/report view of one cached record. Absent evidence is UNKNOWN, never AVAILABLE."""
    moment = at or now()
    spec = SPECS[agent]
    local = record.get("local") or {}
    auth = record.get("auth") or {}
    smoke = record.get("live_smoke") or {}
    unattended = record.get("unattended") or {}
    smoke_age = age_seconds(smoke.get("observed_at"), moment)
    smoke_status = smoke.get("status", "NOT_RUN")
    smoke_stale = smoke_age is not None and smoke_age >= TTL_SECONDS["live_smoke"]
    # Unattended evidence: a real session outranks a smoke; stale evidence reverts to UNKNOWN.
    unattended_value, unattended_source = None, "no unattended invocation evidence"
    if fresh(unattended, "unattended", moment):
        unattended_value, unattended_source = unattended.get("value"), unattended.get("source")
    elif smoke_status in ("PASS", "BLOCKED_PERMISSION") and fresh(smoke, "unattended", moment):
        unattended_value = smoke_status == "PASS"
        unattended_source = "live smoke"
    limits = active_limits(record, moment)
    agent_limit = next((item for item in limits if item.get("scope") in ("agent", "provider")), None)
    windows = smoke.get("capacity_windows") if fresh(smoke, "capacity", moment) else None
    capacity: dict[str, Any] = {"state": "UNKNOWN", "visibility": "UNKNOWN", "source": None, "scope": None,
                                "reason": "CLI does not expose remaining allowance",
                                "five_hour_remaining_percent": None, "weekly_remaining_percent": None,
                                "reset_at": None, "limits": limits}
    if windows:
        capacity.update({"visibility": "VISIBLE", "source": "cli", "reason": "reported by the CLI", **windows})
        remaining = [value for key, value in windows.items() if key.endswith("_percent")]
        capacity["state"] = "QUOTA_EXHAUSTED" if remaining and min(remaining) <= 0 else "AVAILABLE"
    if agent_limit:
        capacity.update({"state": agent_limit["state"], "scope": agent_limit["scope"], "source": agent_limit["source"],
                         "reason": agent_limit.get("reason"), "reset_at": agent_limit["expires_at"]})
    # Stale auth evidence, positive or negative, reverts to UNKNOWN: no permanent blacklist.
    authenticated = auth.get("authenticated") if fresh(auth, "auth", moment) else None
    models = local.get("models") or {"status": "UNKNOWN", "items": [], "default": None, "default_source": None}
    return {
        "agent": agent, "name": spec.name, "backend": spec.binary,
        "executable": local.get("executable"), "installed": bool(local.get("installed")),
        "version": local.get("version"),
        "authenticated": authenticated, "auth_detail": auth.get("detail"), "auth_source": auth.get("source"),
        "unattended_execution": unattended_value, "unattended_source": unattended_source,
        "mutation_capable": local.get("mutation_capable"),
        "live_smoke": {"status": "STALE" if smoke_stale and smoke_status != "NOT_RUN" else smoke_status,
                       "last_status": smoke_status, "verified_at": smoke.get("observed_at"),
                       "age_seconds": None if smoke_age is None else round(smoke_age),
                       "latency_seconds": smoke.get("latency_seconds"), "exit_code": smoke.get("exit_code"),
                       "failure_class": smoke.get("failure_class"), "model_reported": smoke.get("model_reported"),
                       "error": smoke.get("error")},
        "models": models,
        "capacity": capacity,
        "last_verified_at": max(filter(None, (local.get("observed_at"), auth.get("observed_at"),
                                              smoke.get("observed_at"))), default=None),
        "workspace_refusals": dict(record.get("workspaces") or {}),
    }


def evaluate(agents: Iterable[str] | None = None, live: bool = False, refresh: bool = False,
             smoke_timeout: int = DEFAULT_SMOKE_TIMEOUT_SECONDS,
             smoke_runner: Callable[..., dict[str, Any]] | None = None,
             workspace: str | None = None) -> list[dict[str, Any]]:
    """Refresh what is stale (or everything with `refresh`), then summarize.

    Without `live` no prompt is sent. With it, at most one smoke runs per
    eligible agent, and a fresh PASS is reused instead of spending another call.
    With a `workspace`, the live smoke runs read-only in that directory instead
    of a temporary one, and its verdict is kept against that directory.
    """
    selected = [agent for agent in AGENT_ORDER if agents is None or agent in set(agents)]
    cache = load_cache()
    moment = now()
    hosted = hosted_probes_allowed()
    for agent in selected:
        record = cache.setdefault(agent, {})
        relocated = not local_fresh(SPECS[agent], record.get("local"), moment, hosted)
        if refresh or relocated:
            record["local"] = probe_local(SPECS[agent], moment, hosted)
        if refresh or relocated or not fresh(record.get("auth"), "auth", moment):
            record["auth"] = probe_auth(SPECS[agent], record["local"], moment, hosted)
    if live and hosted:
        due = []
        for agent in selected:
            record = cache[agent]
            if not record["local"].get("installed") or record["auth"].get("authenticated") is False:
                continue
            smoke = (record.get("workspace_smokes") or {}).get(workspace) if workspace else record.get("live_smoke")
            smoke = smoke or {}
            if refresh or smoke.get("status") != "PASS" or not fresh(smoke, "live_smoke", moment):
                due.append(agent)
        runner = smoke_runner or run_smoke

        def smoke_for(agent: str) -> dict[str, Any]:
            return runner(agent, smoke_timeout, moment, workspace=workspace) if workspace else runner(
                agent, smoke_timeout, moment)

        with ThreadPoolExecutor(max_workers=max(1, len(due))) as pool:
            outcomes = dict(zip(due, pool.map(smoke_for, due)))
        for agent, smoke in outcomes.items():
            record = cache[agent]
            if workspace:
                record.setdefault("workspace_smokes", {})[workspace] = smoke
                if smoke["status"] == "WORKSPACE_TRUST_REQUIRED":
                    record.setdefault("workspaces", {})[workspace] = {
                        "state": workspace_trust.TRUST_REQUIRED, "role": "review", "source": "live smoke",
                        "detected_at": iso(moment)}
                elif smoke["status"] == "PASS":
                    record.get("workspaces", {}).pop(workspace, None)
                    # A pass in a real workspace is the strongest unattended evidence there is.
                    record["unattended"] = {"observed_at": iso(moment), "value": True,
                                            "source": "live smoke in workspace"}
            else:
                record["live_smoke"] = smoke
            failure = smoke.get("failure_class")
            if failure in FAILURE_TO_CAPACITY:
                model = smoke.get("model_reported")
                record["limits"] = [item for item in record.get("limits", []) if item.get("model") != model] + [
                    limit_entry(FAILURE_TO_CAPACITY[failure], "model" if model else "agent", model, "live smoke",
                                moment, reset_at=(smoke.get("capacity_windows") or {}).get("reset_at"), reason=failure)]
            if failure == "AUTHENTICATION_REQUIRED":
                record["auth"] = {"observed_at": iso(moment), "authenticated": False, "source": "live smoke",
                                  "detail": "the smoke invocation required authentication"}
    for record in cache.values():
        record["limits"] = active_limits(record, moment)
    save_cache(cache)
    return [summarize(agent, cache[agent], moment) for agent in selected]


def cached_summaries() -> dict[str, dict[str, Any]]:
    """Routing view from the cache only: no probe, no prompt."""
    cache = load_cache()
    return {agent: summarize(agent, cache[agent]) for agent in AGENT_ORDER if isinstance(cache.get(agent), dict)}


def routing_models(agent: str) -> list[str]:
    """Models AUTO may pin for an agent. Codex and Claude run on the CLI's own default."""
    spec = SPECS.get(agent)
    if spec is None or not spec.route_listed_models:
        return []
    # `local_only` forbids the hosted model listing here exactly as in the doctor.
    hosted = hosted_probes_allowed()
    if not hosted:
        return []
    cache = load_cache()
    record = cache.setdefault(agent, {})
    moment = now()
    if not local_fresh(spec, record.get("local"), moment, hosted) or record["local"]["models"]["status"] != "LISTED":
        record["local"] = probe_local(spec, moment, hosted)
        try:
            save_cache(cache)
        except OSError:
            pass
    return list(record["local"]["models"]["items"])


# ---------------------------------------------------------------- reporting
def _smoke_text(summary: dict[str, Any]) -> str:
    smoke = summary["live_smoke"]
    status = smoke["status"]
    if status == "NOT_RUN":
        return "NOT RUN (use --live)"
    age = smoke.get("age_seconds")
    when = f" {_duration(age)} ago" if age is not None else ""
    if smoke["last_status"] == "PASS":
        return f"{'PASS' if status != 'STALE' else 'STALE (last PASS'}{when}, {smoke['latency_seconds']}s{')' if status == 'STALE' else ''}"
    if smoke["last_status"] == "BLOCKED_PERMISSION":
        return f"BLOCKED — permission required{when}"
    if smoke["last_status"] == "WORKSPACE_TRUST_REQUIRED":
        return (f"BLOCKED — workspace trust required{when} (the smoke's fresh directory is untrusted;"
                " check a real workspace with --repo)")
    return f"{smoke['last_status']}{when} ({smoke.get('failure_class') or 'no detail'})"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m" if seconds >= 3600 else f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s"


def _capacity_text(capacity: dict[str, Any]) -> list[str]:
    lines = []
    if capacity["visibility"] == "UNKNOWN" and capacity["state"] == "UNKNOWN":
        lines.append("UNKNOWN — CLI does not expose remaining allowance")
    else:
        lines.append(f"{capacity['state']} (source: {capacity['source']}"
                     + (f", until {capacity['reset_at']}" if capacity.get("reset_at") else "") + ")")
    for key, label in (("five_hour_remaining_percent", "Five-hour"), ("weekly_remaining_percent", "Weekly")):
        if capacity.get(key) is not None:
            lines.append(f"{label}: {capacity[key]:.0f}% remaining (reported by CLI)")
    for item in capacity["limits"]:
        if item.get("scope") == "model":
            lines.append(f"Model {item['model']}: {item['state']} until {item['expires_at']} ({item['source']})")
    return lines


def yes_no(value: bool | None, true: str = "YES", false: str = "NO") -> str:
    return "UNKNOWN" if value is None else true if value else false


TRUST_TEXT = {"READY": "READY", "TRUST_REQUIRED": "TRUST REQUIRED", "UNKNOWN": "UNKNOWN",
              "UNSUPPORTED": "UNSUPPORTED", "ERROR": "ERROR"}
PREPARE_TEXT = {"none": "not needed (noninteractive mode has no trust prompt)",
                "cli_flag": "automatic: documented --trust flag, no inference",
                "operator_pty": "one-time: the operator answers the CLI's own prompt"}


def _workspace_text(status: dict[str, Any]) -> str:
    text = TRUST_TEXT.get(status["state"], status["state"])
    trust = status["trust"]
    if status["observed_refusal"]:
        refusal = status["observed_refusal"]
        return f"{text} — the CLI refused this directory ({refusal['source']}, {refusal['detected_at']})"
    if trust.get("trusted_by") and trust["trusted_by"] != status["workspace"]:
        return f"{text} — inherited from {trust['trusted_by']}"
    return f"{text} — {trust.get('detail')}" if trust.get("detail") else text


def render_workspace(report: dict[str, Any]) -> list[str]:
    scope = report["scope"]
    lines = ["WORKSPACE READINESS", f"  Workspace:      {report['workspace']}" + ("" if report["exists"] else " (not created yet)"),
             "  Authorized:     " + (f"YES — {scope['repo_root']} (factory root {scope['factory_root']})"
                                     if scope else "NO — run `howlplane factory prepare --repo <repo>`"), ""]
    for agent, status in report["agents"].items():
        spec = workspace_trust.adapter(agent)
        lines.append(f"  {SPECS[agent].name}: {_workspace_text(status)}")
        if spec and spec.scope != "not_enforced":
            lines.append(f"    Preparation: {PREPARE_TEXT[spec.method]}; scope: this directory and its descendants")
        if status.get("live_smoke"):
            lines.append(f"    Workspace smoke: {status['live_smoke']['status']} ({status['live_smoke']['verified_at']})")
    return lines


def render(summaries: list[dict[str, Any]], workspace: dict[str, Any] | None = None) -> str:
    lines = ["HOWLPLANE AGENT DOCTOR", "", "GLOBAL READINESS", ""]
    for summary in summaries:
        lines.append(summary["name"])
        rows: list[tuple[str, str]] = [("Backend", summary["backend"]),
                                       ("CLI", "AVAILABLE" if summary["installed"] else "NOT INSTALLED")]
        if summary["installed"]:
            models = summary["models"]
            model_text = {"LISTED": f"{len(models['items'])} listed ({', '.join(models['items'][:4])}"
                                    f"{', …' if len(models['items']) > 4 else ''})",
                          "UNSUPPORTED": "not listed by this CLI", "FAILED": "listing failed",
                          "NOT_PROBED": LOCAL_ONLY_DETAIL}.get(models["status"], models["status"])
            if models.get("default"):
                model_text += f"; default {models['default']} ({models['default_source']})"
            rows += [("Version", summary["version"] or "unknown"),
                     ("Auth", {True: "READY", False: "NOT LOGGED IN", None: "UNKNOWN"}[summary["authenticated"]]
                      + (f" — {summary['auth_detail']}" if summary["auth_detail"] else "")),
                     ("Unattended", {True: "YES", False: "INTERACTIVE_ONLY", None: "UNVERIFIED"}[summary["unattended_execution"]]
                      + (f" ({summary['unattended_source']})" if summary["unattended_execution"] is not None else "")),
                     ("Edits allowed", yes_no(summary["mutation_capable"])),
                     ("Models", model_text),
                     ("Live smoke", _smoke_text(summary))]
            if summary["live_smoke"].get("model_reported"):
                rows.append(("Model seen", summary["live_smoke"]["model_reported"]))
            capacity = _capacity_text(summary["capacity"])
            rows.append(("Capacity", capacity[0]))
            rows += [("", extra) for extra in capacity[1:]]
            rows.append(("Verified", summary["last_verified_at"] or "never"))
            if workspace and summary["agent"] in workspace["agents"]:
                rows.append(("Workspace", TRUST_TEXT.get(workspace["agents"][summary["agent"]]["state"], "UNKNOWN")))
        lines += [f"  {label + ':' if label else '':<15} {value}" for label, value in rows]
        lines.append("")
    if workspace:
        lines += render_workspace(workspace)
    return "\n".join(lines).rstrip() + "\n"


def document(summaries: list[dict[str, Any]], workspace: dict[str, Any] | None = None) -> dict[str, Any]:
    body = {"schema": SCHEMA, "generated_at": iso(now()), "agents": summaries}
    if workspace:
        body["workspace"] = workspace
    return body


# ---------------------------------------------------------------- factory preflight
def factory_readiness(summaries: list[dict[str, Any]], workspace: dict[str, Any] | None = None) -> dict[str, Any]:
    """Can an unattended campaign run: an autonomous implementer plus an independent auditor?

    With a workspace report, an agent that would meet a trust prompt in the
    Factory workspace is not a worker there, however ready it is elsewhere.
    """
    trust = (workspace or {}).get("agents", {})

    def trusted_here(summary: dict[str, Any]) -> bool:
        status = trust.get(summary["agent"])
        return status is None or status["state"] != workspace_trust.TRUST_REQUIRED

    def autonomous(summary: dict[str, Any]) -> bool:
        return (summary["installed"] and summary["authenticated"] is not False
                and summary["unattended_execution"] is not False and trusted_here(summary)
                and summary["capacity"]["state"] not in ("QUOTA_EXHAUSTED", "SESSION_EXHAUSTED", "RATE_LIMITED"))
    implementers = [s["name"] for s in summaries if autonomous(s) and s["mutation_capable"] is not False]
    reviewers = [s["name"] for s in summaries if autonomous(s)]
    independent_audit = any(auditor != implementer for implementer in implementers for auditor in reviewers)
    verified = [s["name"] for s in summaries if autonomous(s) and s["unattended_execution"] is True]
    gated = [s["name"] for s in summaries if s["installed"] and not trusted_here(s)]
    # Trust-gated agents do not block a campaign others can run, but it is degraded.
    status = "BLOCKED" if not implementers else (
        "READY" if independent_audit and set(verified) >= set(reviewers) and not gated else "DEGRADED")
    return {
        "status": status,
        "implementation_capable": implementers,
        "review_capable": reviewers,
        "interactive_only": [s["name"] for s in summaries if s["installed"] and s["unattended_execution"] is False],
        "unavailable": [s["name"] for s in summaries if not s["installed"] or s["authenticated"] is False],
        "capacity_unknown": [s["name"] for s in summaries if s["installed"] and s["capacity"]["state"] == "UNKNOWN"],
        "known_exhausted": [f"{s['name']}: {item['state']}" + (f" ({item['model']})" if item.get("model") else "")
                            for s in summaries for item in s["capacity"]["limits"]],
        "live_smoke_passed": [s["name"] for s in summaries if s["live_smoke"]["status"] == "PASS"],
        "workspace": None if workspace is None else workspace["workspace"],
        "workspace_authorized": None if workspace is None else workspace["authorized"],
        # With a workspace, trust there is what gates Factory; a fresh-directory
        # smoke refusal only describes that throwaway directory.
        "workspace_trust_required": (gated if workspace is not None else
                                     [s["name"] for s in summaries
                                      if s["live_smoke"].get("last_status") == "WORKSPACE_TRUST_REQUIRED"]),
        "unverified": [name for name in reviewers if name not in verified],
        "independent_audit_available": independent_audit,
    }


def render_factory(readiness: dict[str, Any], budget: dict[str, int]) -> str:
    def names(values: list[str]) -> str:
        return ", ".join(values) or "none"
    lines = [f"FACTORY READINESS: {readiness['status']}",
             f"  Implementation-capable: {names(readiness['implementation_capable'])}",
             f"  Review-capable:         {names(readiness['review_capable'])}",
             f"  Independent audit:      {'available' if readiness['independent_audit_available'] else 'NOT available'}",
             f"  Interactive-only:       {names(readiness['interactive_only'])}",
             f"  Unavailable:            {names(readiness['unavailable'])}",
             f"  Unattended unverified:  {names(readiness['unverified'])}",
             f"  Capacity unknown:       {names(readiness['capacity_unknown'])}",
             f"  Known exhausted:        {names(readiness['known_exhausted'])}",
             f"  Live smoke passed:      {names(readiness['live_smoke_passed'])}",
             f"  Workspace trust gated:  {names(readiness['workspace_trust_required'])}",
             *([f"  Workspace:              {readiness['workspace']} (authorized: "
                f"{'yes' if readiness['workspace_authorized'] else 'no'})"] if readiness.get("workspace") else []),
             "  Execution budget:       " + ", ".join(f"{role} {seconds}s" for role, seconds in budget.items())]
    return "\n".join(lines) + "\n"


def command(args: Any) -> int:
    """`howlplane agents doctor`. Readiness is reported as data; only a failure to run exits nonzero."""
    selected = getattr(args, "agent", None) or None
    unknown = [agent for agent in selected or () if agent not in SPECS]
    if unknown:
        print(f"Unknown agent(s): {', '.join(unknown)}. Choose from {', '.join(AGENT_ORDER)}.")
        return 1
    live = getattr(args, "live", False)
    if live and not hosted_probes_allowed():
        print(f"--live {LOCAL_ONLY_DETAIL}; reporting local evidence only.", file=sys.stderr)
    repo = getattr(args, "repo", None)
    workspace = str(Path(repo).expanduser().resolve()) if repo else None
    if workspace and not Path(workspace).is_dir():
        print(f"--repo {repo}: not a directory.")
        return 1
    summaries = evaluate(selected, live=live, refresh=getattr(args, "refresh", False),
                         smoke_timeout=getattr(args, "smoke_timeout", DEFAULT_SMOKE_TIMEOUT_SECONDS), workspace=workspace)
    report = workspace_report(workspace, selected, live=live) if workspace else None
    print(json.dumps(document(summaries, report), indent=2) if getattr(args, "json", False) else render(summaries, report),
          end="" if not getattr(args, "json", False) else "\n")
    return 0

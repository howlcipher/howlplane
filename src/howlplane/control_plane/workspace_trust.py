"""Workspace trust: whether an agent CLI will run unattended in one specific directory.

Global readiness (installed, authenticated, unattended backend) says nothing
about a folder the CLI has never seen. Some CLIs gate every new directory behind
a vendor trust prompt, which an unattended session cannot answer. This module
keeps the two facts apart:

* Vendor trust is read from each CLI's own store, read-only, and verified
  against observed CLI behavior. HowlPlane never writes a vendor trust store.
* HowlPlane authorization is a separate record meaning only "the operator
  allowed HowlPlane to prepare and use this scope". It never implies that a
  vendor CLI trusts anything.

Preparation only ever targets an explicitly authorized path, and only through a
vendor-documented mechanism (Cursor's `--trust` flag) or the vendor's own
interactive prompt answered by the operator (Devin). Nothing here answers a
prompt on anyone's behalf.

Observed on the installed CLIs (2026-09-25):

* Codex 0.156.1 `exec`, Claude Code 2.1.282 `-p` and AGY 1.2.10 `-p` run in an
  untrusted directory without any prompt; their noninteractive modes do not
  enforce workspace trust.
* Cursor 2026.09.23 `-p` refuses an untrusted directory ("Workspace Trust
  Required ... Pass --trust"). Trust is a marker file per directory and is
  inherited from a trusted ancestor unless that ancestor is $HOME, above $HOME,
  or shorter than three path components.
* Devin 3000.11.3 `-p` refuses an untrusted directory ("Refusing to run in an
  untrusted workspace"). Trust is a list of canonical paths and covers their
  descendants. Only its interactive prompt adds to that list.

Workspace trust policy (`resolve_policy`) decides what an unattended invocation
does about vendor trust. It is resolved once, here, and applied in the backend
invocation layer, so doctor, orchestration, Factory, and execution all agree:

* strict: never bypass. An untrusted workspace is ineligible until the vendor
  itself trusts it.
* prepare (built-in default): as strict at dispatch time, with vendor trust
  prepared ahead of time through `howlplane factory prepare`.
* bypass: supply each CLI's audited, documented invocation option so trust never
  interrupts unattended work: Cursor `--trust` (which records Cursor trust) and
  Devin `--respect-workspace-trust false` (which skips the check for that one
  invocation and trusts nothing). A CLI without an audited mechanism gets none.

Vendor trust, HowlPlane authorization, and a bypassed check are three separate
facts; `effective` reports them side by side and never rewrites vendor state.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from howlplane.control_plane.atomic_io import load_schema_section, private_state_path, write_private_json

SCHEMA = "howlplane.workspace_authorization/v1"
READY, TRUST_REQUIRED, UNKNOWN, UNSUPPORTED, ERROR = "READY", "TRUST_REQUIRED", "UNKNOWN", "UNSUPPORTED", "ERROR"
STATES = (READY, TRUST_REQUIRED, UNKNOWN, UNSUPPORTED, ERROR)
# Vendor state reported for a CLI whose noninteractive mode has no trust check.
NOT_ENFORCED = "NOT_ENFORCED"
STRICT, PREPARE, BYPASS = "strict", "prepare", "bypass"
POLICIES = (STRICT, PREPARE, BYPASS)
DEFAULT_POLICY = PREPARE
POLICY_ENV = "HOWLPLANE_WORKSPACE_TRUST"
# A model name no CLI accepts: the vendor checks trust first, then rejects the
# model, so a trust probe or `--trust` preparation never reaches inference.
SENTINEL_MODEL = "howlplane-trust-probe-invalid-model"
PROBE_TIMEOUT_SECONDS = 30
BOOTSTRAP_TIMEOUT_SECONDS = 300
# Vendor refusals appear before any work starts. Matching only the head of the
# output keeps an agent transcript that merely discusses trust (for example,
# while editing this very module) from being mistaken for a refusal.
REFUSAL_WINDOW_CHARS = 2000


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TrustAdapter:
    agent: str
    # "not_enforced": the noninteractive mode never asks. "ancestor": trust on a
    # directory also covers everything below it.
    scope: str
    # "none" | "cli_flag" (vendor-documented noninteractive flag) | "operator_pty"
    # (the vendor's own prompt, answered by the operator at a terminal).
    method: str
    note: str
    # Lowercased phrases that positively identify this CLI's trust refusal.
    refusal_markers: tuple[str, ...] = ()
    # Phrases proving the trust check passed before the sentinel model was rejected.
    past_trust_markers: tuple[str, ...] = ()
    # What the `bypass` policy may do, audited per CLI: "not_enforced" (nothing
    # to bypass), "persistent_flag" (a documented flag that records vendor
    # trust), "invocation_flag" (a documented flag that skips the check for one
    # invocation only), or "unsupported" (no audited mechanism: fail closed).
    bypass: str = "unsupported"
    bypass_argv: tuple[str, ...] = ()
    bypass_establishes_trust: bool = False


ADAPTERS: dict[str, TrustAdapter] = {
    "codex": TrustAdapter("codex", "not_enforced", "none",
                          "`codex exec` shows no workspace trust dialog (observed codex-cli 0.156.1)",
                          bypass="not_enforced"),
    "claude_code": TrustAdapter("claude_code", "not_enforced", "none",
                                "`claude -p` skips the workspace trust dialog (claude --help)",
                                bypass="not_enforced"),
    "agy": TrustAdapter("agy", "not_enforced", "none",
                        "`agy -p` runs in an untrusted directory without prompting (observed agy 1.2.10)",
                        bypass="not_enforced"),
    # `agent --help` (2026.09.23): "--trust  Trust the current workspace without
    # prompting". Separate from --force/--yolo, which approve commands.
    "cursor": TrustAdapter("cursor", "ancestor", "cli_flag",
                           "trusted directory and its descendants, except via $HOME or very short paths;"
                           " prepared with the documented `--trust` flag",
                           ("workspace trust required", "pass --trust, --yolo, or -f if you trust this directory"),
                           ("cannot use this model",),
                           bypass="persistent_flag", bypass_argv=("--trust",), bypass_establishes_trust=True),
    # `devin --help` (3000.11.3): "pass --respect-workspace-trust false to skip
    # the check". It adds nothing to Devin's trusted list.
    "devin_cli": TrustAdapter("devin_cli", "ancestor", "operator_pty",
                              "trusted directory and its descendants; only Devin's interactive prompt adds trust",
                              ("refusing to run in an untrusted workspace",),
                              ("unknown model",),
                              bypass="invocation_flag", bypass_argv=("--respect-workspace-trust", "false")),
}


def adapter(agent: str) -> TrustAdapter | None:
    return ADAPTERS.get(agent)


def is_trust_refusal(agent: str, text: str) -> bool:
    """True only for this CLI's own refusal, near the start of its output."""
    spec = ADAPTERS.get(agent)
    if spec is None or not spec.refusal_markers:
        return False
    head = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text or "")[:REFUSAL_WINDOW_CHARS].lower()
    return any(marker in head for marker in spec.refusal_markers)


def result_is_trust_refusal(agent: str, result: Any) -> bool:
    """A failed execution whose stdout or stderr opens with a positively identified refusal."""
    if getattr(result, "success", False):
        return False
    return any(is_trust_refusal(agent, getattr(result, stream, "") or "") for stream in ("stdout", "stderr"))


# ---------------------------------------------------------------- policy
_cli_policy: str | None = None


def validate_policy(value: Any, source: str) -> str:
    text = str(value).strip().lower() if value is not None else ""
    if text not in POLICIES:
        raise ValueError(f"Invalid workspace trust policy {value!r} from {source}; choose one of {', '.join(POLICIES)}")
    return text


def _configured_policy(loader: Any = None) -> Any:
    """`[workspace_trust] policy` when the operator configured it, else None."""
    if loader is None:
        from howlplane.control_plane.config_loader import default_loader as loader
    section = getattr(getattr(loader, "settings", None), "workspace_trust", None)
    if section is None or "policy" not in section.model_fields_set:
        return None
    return section.policy


def set_cli_policy(value: str | None) -> None:
    """An explicit `--workspace-trust` for this process and everything it launches."""
    global _cli_policy
    if value is None:
        return
    _cli_policy = validate_policy(value, "--workspace-trust")
    os.environ[POLICY_ENV] = _cli_policy


def cli_policy() -> str | None:
    """The explicit `--workspace-trust` of this process, if any."""
    return _cli_policy


def reset_cli_policy() -> None:
    global _cli_policy
    _cli_policy = None


def resolve_policy(cli_value: str | None = None) -> dict[str, str]:
    """The one workspace trust policy every subsystem uses, and where it came from.

    Precedence: CLI option, then `HOWLPLANE_WORKSPACE_TRUST` (how a CLI option
    reaches the processes HowlPlane launches), then `[workspace_trust] policy`
    in HowlPlane configuration, then the built-in default (`prepare`).
    """
    if cli_value is not None:
        return {"policy": validate_policy(cli_value, "--workspace-trust"), "source": "cli"}
    if _cli_policy is not None:
        return {"policy": _cli_policy, "source": "cli"}
    env = os.environ.get(POLICY_ENV, "").strip()
    if env:
        return {"policy": validate_policy(env, POLICY_ENV), "source": "environment"}
    configured = _configured_policy()
    if configured is not None:
        return {"policy": validate_policy(configured, "[workspace_trust] policy"), "source": "config"}
    return {"policy": DEFAULT_POLICY, "source": "default"}


def invocation_argv(agent: str, policy: str) -> list[str]:
    """Audited vendor arguments for one unattended invocation; empty unless the policy is bypass."""
    spec = ADAPTERS.get(agent)
    if policy != BYPASS or spec is None or spec.bypass not in ("persistent_flag", "invocation_flag"):
        return []
    return list(spec.bypass_argv)


def mechanism(agent: str, policy: str) -> str | None:
    argv = invocation_argv(agent, policy)
    return " ".join(argv) if argv else None


def effective(agent: str, vendor_state: str, policy: str) -> dict[str, Any]:
    """Whether an unattended invocation can run here under `policy`, next to the vendor's own verdict.

    `vendor_state` is reported unchanged: a bypassed check is not trust.
    """
    spec = ADAPTERS.get(agent)
    base = {"policy": policy, "mechanism": None}
    if spec is None:
        return {**base, "vendor_state": UNSUPPORTED, "effective_state": UNSUPPORTED,
                "detail": "no audited workspace trust model for this agent"}
    if spec.scope == "not_enforced":
        return {**base, "vendor_state": NOT_ENFORCED, "effective_state": READY,
                "detail": "noninteractive mode does not enforce workspace trust"}
    if vendor_state == READY:
        return {**base, "vendor_state": READY, "effective_state": READY, "detail": "the vendor trusts this workspace"}
    flag = mechanism(agent, policy)
    if flag:
        detail = ("vendor trust is recorded by the CLI itself on first use" if spec.bypass_establishes_trust
                  else "vendor trust not required for this invocation (check skipped, directory not trusted)")
        return {**base, "vendor_state": vendor_state, "effective_state": READY, "mechanism": flag, "detail": detail}
    if policy == STRICT:
        detail = "strict policy: ineligible until the vendor trusts this workspace"
    elif spec.method == "cli_flag":
        detail = "prepare with `howlplane factory prepare` (automatic, no inference)"
    else:
        detail = "operator preparation required (`howlplane factory prepare` at a terminal)"
    return {**base, "vendor_state": vendor_state, "effective_state": vendor_state, "detail": detail}


# ---------------------------------------------------------------- vendor stores (read-only)
def _home() -> Path:
    return Path(os.path.expanduser("~"))


def cursor_projects_dir() -> Path:
    override = os.environ.get("CURSOR_DATA_DIR", "").strip()
    return Path(override or _home() / ".cursor") / "projects"


def cursor_slug(path: str) -> str:
    """Cursor's own per-workspace directory name (`workspace-paths.js`)."""
    return re.sub(r"^-+|-+$", "", re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9]", "-", path)))


def _cursor_inheritable(ancestor: Path) -> bool:
    """Cursor ignores trust on $HOME, anything above it, and paths with fewer than 3 parts."""
    home = str(_home())
    text = str(ancestor)
    if text == home or home.startswith(text.rstrip(os.sep) + os.sep):
        return False
    return len([part for part in text.split(os.sep) if part]) >= 3


def _cursor_marker(path: Path) -> Path:
    return cursor_projects_dir() / cursor_slug(str(path)) / ".workspace-trusted"


def check_cursor(path: Path) -> dict[str, Any]:
    if _cursor_marker(path).is_file():
        return {"state": READY, "covering_path": str(path), "detail": "workspace trust marker present"}
    for ancestor in path.parents:
        if _cursor_marker(ancestor).is_file() and _cursor_inheritable(ancestor):
            return {"state": READY, "covering_path": str(ancestor),
                    "detail": "inherited from a trusted ancestor"}
    return {"state": TRUST_REQUIRED, "covering_path": None,
            "detail": "no trust marker for this directory or an eligible ancestor"}


def devin_trust_file() -> Path:
    override = os.environ.get("HOWLPLANE_DEVIN_TRUST_FILE")
    if override and Path(override).is_absolute():
        return Path(override)
    # Devin keeps its data (credentials and trust) under $XDG_DATA_HOME/devin (observed).
    data = os.environ.get("XDG_DATA_HOME")
    base = Path(data) if data and Path(data).is_absolute() else _home() / ".local" / "share"
    return base / "devin" / "cli" / "trusted_workspaces.json"


# Devin's own key for its list of trusted directories.
DEVIN_LIST_KEY = "trusted_paths"


def check_devin(path: Path) -> dict[str, Any]:
    store = devin_trust_file()
    if not store.is_file():
        return {"state": TRUST_REQUIRED, "covering_path": None, "detail": "Devin has no trusted workspaces yet"}
    try:
        listed = json.loads(store.read_text(encoding="utf-8")).get(DEVIN_LIST_KEY, [])
    except (OSError, ValueError, AttributeError) as exc:
        return {"state": ERROR, "covering_path": None, "detail": f"Devin trust store unreadable: {type(exc).__name__}"}
    canonical = os.path.realpath(path)
    for entry in listed if isinstance(listed, list) else []:
        if not isinstance(entry, str) or not entry:
            continue
        root = os.path.realpath(entry)
        if canonical == root or canonical.startswith(root.rstrip(os.sep) + os.sep):
            return {"state": READY, "covering_path": root, "detail": "inside a Devin-trusted workspace"}
    return {"state": TRUST_REQUIRED, "covering_path": None, "detail": "not inside any Devin-trusted workspace"}


STORE_CHECKS: dict[str, Callable[[Path], dict[str, Any]]] = {"cursor": check_cursor, "devin_cli": check_devin}


def check(agent: str, workspace: str | Path) -> dict[str, Any]:
    """Vendor trust for one agent in one directory, from local files only (no prompt, no network)."""
    spec = ADAPTERS.get(agent)
    path = Path(workspace).expanduser().resolve()
    base = {"agent": agent, "workspace": str(path), "checked_at": now(), "source": "vendor trust store"}
    if spec is None:
        return {**base, "state": UNSUPPORTED, "scope": None, "method": None, "covering_path": None,
                "detail": "no workspace trust model known for this agent", "source": None}
    base.update({"scope": spec.scope, "method": spec.method})
    if spec.scope == "not_enforced":
        return {**base, "state": READY, "covering_path": None, "detail": spec.note, "source": "observed CLI behavior"}
    try:
        # A directory not created yet (a Factory worktree about to be added)
        # gets the verdict its ancestors give it, exactly as the CLI would.
        return {**base, **STORE_CHECKS[agent](path)}
    except OSError as exc:
        return {**base, "state": ERROR, "covering_path": None, "detail": f"trust check failed: {type(exc).__name__}"}


def _run(argv: list[str], cwd: Path, timeout: float = PROBE_TIMEOUT_SECONDS) -> tuple[int, str]:
    try:
        completed = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                                   stdin=subprocess.DEVNULL)  # nosec B603 - fixed vendor argv, no shell
    except subprocess.TimeoutExpired:
        return -1, "probe timed out"
    except OSError as exc:
        return 127, str(exc)
    return completed.returncode, f"{completed.stdout}\n{completed.stderr}"


PROBE_ARGV: dict[str, Callable[[Path], list[str]]] = {
    "cursor": lambda path: ["agent", "-p", "noop", "--workspace", str(path), "--model", SENTINEL_MODEL,
                            "--output-format", "text"],
    "devin_cli": lambda path: ["devin", "-p", "noop", "--model", SENTINEL_MODEL],
}


def probe(agent: str, workspace: str | Path, runner: Callable[[list[str], Path], tuple[int, str]] | None = None
          ) -> dict[str, Any]:
    """Ask the CLI itself, with a model it will reject, so no inference ever runs.

    The vendor contacts its own service to reject the model, so callers must
    respect `local_only` before calling this.
    """
    path = Path(workspace).expanduser().resolve()
    spec = ADAPTERS.get(agent)
    if spec is None or agent not in PROBE_ARGV:
        return check(agent, path)
    _code, output = (runner or _run)(PROBE_ARGV[agent](path), path)
    lowered = output.lower()
    if is_trust_refusal(agent, output):
        state, detail = TRUST_REQUIRED, "the CLI refused this directory as untrusted"
    elif any(marker in lowered for marker in spec.past_trust_markers):
        state, detail = READY, "the CLI accepted this directory (stopped at the sentinel model)"
    else:
        state, detail = UNKNOWN, "the CLI response did not identify its trust decision"
    return {"agent": agent, "workspace": str(path), "checked_at": now(), "source": "CLI trust probe",
            "scope": spec.scope, "method": spec.method, "state": state, "covering_path": None, "detail": detail}


# ---------------------------------------------------------------- HowlPlane authorization registry
def registry_path() -> Path:
    return private_state_path("HOWLPLANE_WORKSPACE_AUTHORIZATION_FILE", "workspace_authorizations.json")


def load_registry() -> dict[str, dict[str, Any]]:
    return load_schema_section(registry_path(), SCHEMA, "authorizations")


def save_registry(entries: dict[str, dict[str, Any]]) -> None:
    write_private_json(registry_path(), {"schema": SCHEMA, "authorizations": entries})


def _git_common_dir(path: Path) -> Path | None:
    try:
        completed = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                   capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
                                   check=False)  # nosec B603 B607 - fixed argv, no shell
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(completed.stdout.strip()).resolve() if completed.returncode == 0 and completed.stdout.strip() else None


def authorize(repo_root: Path, common_git_dir: Path, factory_root: Path, workspaces: Iterable[Path],
              agents: Iterable[str], intent: str) -> dict[str, Any]:
    entries = load_registry()
    key = str(repo_root)
    previous = entries.get(key, {})
    entry = {
        "repo_root": key, "common_git_dir": str(common_git_dir), "factory_root": str(factory_root),
        "workspaces": sorted({str(Path(item).resolve()) for item in workspaces} | set(previous.get("workspaces", []))),
        "authorized_at": previous.get("authorized_at") or now(), "operator_intent": intent,
        "strategy": "inherited trust over a stable per-repository Factory workspace root",
        "agents": sorted(set(agents) | set(previous.get("agents", []))),
        "agents_prepared": previous.get("agents_prepared", {}), "last_verified_at": previous.get("last_verified_at"),
    }
    entries[key] = entry
    save_registry(entries)
    return entry


def record_prepared(repo_root: Path, agent: str, results: list[dict[str, Any]]) -> None:
    entries = load_registry()
    entry = entries.get(str(repo_root))
    if entry is None:
        return
    moment = now()
    entry.setdefault("agents_prepared", {})[agent] = {
        "state": READY if results and all(item["state"] == READY for item in results) else (
            results[-1]["state"] if results else UNKNOWN),
        "method": ADAPTERS[agent].method if agent in ADAPTERS else None,
        "workspaces": {item["workspace"]: item["state"] for item in results}, "verified_at": moment}
    entry["last_verified_at"] = moment
    save_registry(entries)


def revoke(repo_root: Path) -> dict[str, Any] | None:
    entries = load_registry()
    removed = entries.pop(str(repo_root), None)
    if removed is not None:
        save_registry(entries)
    return removed


def authorized_scope_for(path: str | Path) -> dict[str, Any] | None:
    """The authorization covering `path`, or None. Arbitrary paths are never covered.

    Covered means: the authorized checkout or a listed workspace exactly, or a
    Git worktree of the same repository inside the authorized Factory root.
    """
    target = Path(path).expanduser().resolve()
    for entry in load_registry().values():
        exact = {entry.get("repo_root")} | set(entry.get("workspaces", []))
        if str(target) in exact:
            return entry
        root = entry.get("factory_root")
        if root and (target == Path(root) or Path(root) in target.parents):
            # Inside the authorized root: the root itself, a worktree of this same
            # repository, or a path Factory has not created yet (it can only be
            # created there as a worktree of this repository).
            if (target == Path(root) or not target.exists()
                    or _git_common_dir(target) == Path(entry.get("common_git_dir", ""))):
                return entry
    return None


# ---------------------------------------------------------------- preparation
class PreparationRefused(RuntimeError):
    """Preparation was asked to touch something the operator did not authorize."""


def prepare_cursor(path: Path, runner: Callable[[list[str], Path], tuple[int, str]] | None = None) -> dict[str, Any]:
    """Cursor's documented `--trust` flag, stopped at the sentinel model before any inference."""
    (runner or _run)(["agent", "-p", "noop", "--trust", "--workspace", str(path), "--model", SENTINEL_MODEL,
                      "--output-format", "text"], path)
    return check("cursor", path)


def _restore_terminal() -> None:
    if sys.stdin.isatty():
        subprocess.run(["stty", "sane"], check=False, stdin=sys.stdin)  # nosec B603 B607 - fixed argv


def prepare_operator_prompt(agent: str, path: Path, argv: list[str],
                            timeout: float = BOOTSTRAP_TIMEOUT_SECONDS,
                            popen: Callable[..., Any] = subprocess.Popen,
                            is_tty: Callable[[], bool] | None = None,
                            poll_interval: float = 0.5) -> dict[str, Any]:
    """Open the vendor's own trust prompt on the operator's terminal; never answer it.

    HowlPlane writes nothing to the process. It watches the vendor store until
    the directory is trusted, then ends the session it started.
    """
    tty = is_tty() if is_tty else (sys.stdin.isatty() and sys.stdout.isatty())
    if not tty:
        return {**check(agent, path), "detail": "operator terminal required for the one-time trust prompt; not attempted"}
    already = check(agent, path)
    if already["state"] == READY:
        return already
    print(f"\n{agent}: the vendor trust prompt is opening for\n  {path}\n"
          "Answer it yourself. HowlPlane sends no input and closes the session once trust is recorded.\n", flush=True)
    process = popen(argv, cwd=str(path))
    deadline = time.monotonic() + timeout
    result = already
    try:
        while time.monotonic() < deadline:
            result = check(agent, path)
            if result["state"] == READY or process.poll() is not None:
                break
            time.sleep(poll_interval)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        _restore_terminal()
    result = check(agent, path)
    if result["state"] != READY:
        result["detail"] = "trust was not recorded (prompt declined, closed, or timed out)"
    return result


PREPARERS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "cursor": prepare_cursor,
    "devin_cli": lambda path: prepare_operator_prompt("devin_cli", path, ["devin"]),
}


def prepare(agent: str, path: Path) -> dict[str, Any]:
    """Establish vendor trust for one authorized directory, then re-verify it."""
    target = Path(path).expanduser().resolve()
    if authorized_scope_for(target) is None:
        raise PreparationRefused(f"{target} is not inside a HowlPlane-authorized scope; refusing to prepare trust")
    spec = ADAPTERS.get(agent)
    if spec is None:
        return check(agent, target)
    current = check(agent, target)
    if current["state"] == READY or spec.method == "none":
        return current
    return PREPARERS[agent](target)

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


ADAPTERS: dict[str, TrustAdapter] = {
    "codex": TrustAdapter("codex", "not_enforced", "none",
                          "`codex exec` shows no workspace trust dialog (observed codex-cli 0.156.1)"),
    "claude_code": TrustAdapter("claude_code", "not_enforced", "none",
                                "`claude -p` skips the workspace trust dialog (claude --help)"),
    "agy": TrustAdapter("agy", "not_enforced", "none",
                        "`agy -p` runs in an untrusted directory without prompting (observed agy 1.2.10)"),
    "cursor": TrustAdapter("cursor", "ancestor", "cli_flag",
                           "trusted directory and its descendants, except via $HOME or very short paths;"
                           " prepared with the documented `--trust` flag",
                           ("workspace trust required", "pass --trust, --yolo, or -f if you trust this directory"),
                           ("cannot use this model",)),
    "devin_cli": TrustAdapter("devin_cli", "ancestor", "operator_pty",
                              "trusted directory and its descendants; only Devin's interactive prompt adds trust",
                              ("refusing to run in an untrusted workspace",),
                              ("unknown model",)),
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

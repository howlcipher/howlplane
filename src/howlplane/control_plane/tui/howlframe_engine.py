"""`howlplane tui --engine howlframe`: launch the pure HowlFrame TUI (ADR 0010).

This module holds no TUI logic. It resolves absolute paths, writes the
grant profile and replaces itself with `howlframe run` on the packaged
``tui_app.hfbc``. The HowlFrame runtime then owns the terminal, timers and
child processes; every HowlPlane action is an exact-argv `howlplane`
subcommand the profile allows, run with the operator's environment through
``process:exec_inherit_env=`` so providers are found and can authenticate.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Dict, List, Optional

from howlplane.control_plane.config_loader import TuiSettings
from howlplane.control_plane.resources import data_file

ENTRY_NAME = "howlplane-tui-entry"
CANCEL_REASON = "cancelled-from-howlplane-tui"


class LaunchError(Exception):
    """The HowlFrame TUI cannot start; the message says why and what to do."""


def app_artifact() -> Path:
    """The committed tui_app bytecode (checkout copy first, then the packaged copy)."""
    return data_file("integrations", "howlframe", "tui", "tui_app.hfbc")


def _plain(path: Path, what: str) -> str:
    """A canonical absolute path that a grant token can carry verbatim."""
    text = os.path.realpath(path)
    if any(c.isspace() for c in text) or "," in text:
        raise LaunchError(f"{what} path {text!r} contains whitespace or a comma, which a HowlFrame grant cannot carry; "
                          "use a path without them or the default engine (--engine python)")
    return text


def write_entry(state_root: Path) -> Path:
    """A private entry script for this Python and HowlPlane.

    HowlFrame refuses to exec a symlink, and a virtual environment's
    interpreter usually is one, so the grant names this regular file instead;
    its interpreter line keeps the operator's environment and venv.
    """
    bindir = state_root / "bin"
    bindir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(bindir, 0o700)
    entry = bindir / ENTRY_NAME
    body = (f"#!{sys.executable}\n"
            "import runpy\n"
            "runpy.run_module('howlplane.control_plane', run_name='__main__', alter_sys=True)\n")
    tmp = entry.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.chmod(tmp, 0o700)
    os.replace(tmp, entry)
    return entry


def grant_profile(entry: str, repo: str, task_runs: str, log_dir: str) -> List[str]:
    """The exact authority the TUI program holds, and nothing more."""
    cli = f"process:exec={entry}"
    return [
        f"process:exec_inherit_env={entry}",
        f"{cli} providers --json --repo {repo}",
        f"{cli} route --probe --json --repo {repo} -- **",
        f"{cli} route --probe --json --repo {repo} --agent * -- **",
        f"{cli} work --execute --json --progress always --task-id * --repo {repo} -- **",
        f"{cli} work --execute --json --progress always --task-id * --repo {repo} --agent * -- **",
        f"{cli} cancel --json --repo {repo} --reason {CANCEL_REASON} -- *",
        f"filesystem:read={task_runs}",
        f"filesystem:write={log_dir}",
    ]


def launch_argv(howlframe: str, target_repo: Path, settings: TuiSettings, state_root: Path,
                session_id: Optional[str] = None) -> List[str]:
    """The complete `howlframe run` command line for one session."""
    artifact = app_artifact()
    if not artifact.is_file():
        raise LaunchError(f"The HowlFrame TUI artifact is missing at {artifact}; reinstall HowlPlane")
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_dir = state_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_dir, 0o700)
    entry = _plain(write_entry(state_root), "HowlPlane entry")
    repo = _plain(target_repo, "Repository")
    task_runs = os.path.join(repo, ".task_runs")
    logs = _plain(log_dir, "Log")
    config: Dict[str, object] = {
        "cli": [entry],
        "repo": repo,
        "task_runs": task_runs,
        "log_path": os.path.join(logs, "tui_events.jsonl"),
        "session_id": session_id or secrets.token_hex(8),
        "max_objective_chars": settings.max_objective_chars,
        "poll_ms": int(settings.progress_poll_seconds * 1000),
        "cancel_grace_ms": int(settings.cancel_grace_seconds * 1000),
        "cancel_reason": CANCEL_REASON,
    }
    return [howlframe, "run",
            "--allow-caps", ",".join(grant_profile(entry, repo, task_runs, logs)),
            "--max-event-instructions", str(settings.max_instructions),
            "--max-event-memory-bytes", str(settings.max_memory_bytes),
            "--max-state-bytes", str(settings.max_step_bytes),
            str(artifact), json.dumps(config)]


def run(howlframe: str, target_repo: Path, settings: TuiSettings, state_root: Path) -> int:
    """Replaces this process with the HowlFrame TUI; returns only on failure to start."""
    argv = launch_argv(howlframe, target_repo, settings, state_root)
    os.execv(howlframe, argv)
    return 127  # pragma: no cover - execv does not return

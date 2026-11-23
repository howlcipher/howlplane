"""The only place `howlplane tui` touches HowlPlane (ADR 0009).

`Backend` is the seam between the terminal host and HowlPlane, so tests can
substitute a fake that records calls and never reaches a provider.
`HowlPlaneBackend` adds no configuration, routing or authority of its own:

* inventory and route preview use the shared `ProviderPoolManager` in process,
  read only, with readiness probed (TUI-00 finding HP-F2);
* a run is `howlplane work OBJECTIVE --execute --json` in a child process with
  the operator's own environment: the same governed path as the CLI;
* cancel interrupts that child, then records the cancellation through the
  existing `HumanLifecycleManager.cancel` path.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from howlplane.control_plane.atomic_io import safe_load_json


@dataclass
class RunHandle:
    """A governed run in progress."""

    task_id: str
    process: Optional[subprocess.Popen]
    stderr_path: Optional[Path] = None
    cancel_requested: bool = False

    def finished(self) -> bool:
        return self.process is None or self.process.poll() is not None


class Backend(Protocol):
    def inventory(self) -> Dict[str, Any]: ...

    def route(self, objective: str, resource_id: Optional[str]) -> Dict[str, Any]: ...

    def start_run(self, objective: str, resource_id: Optional[str], task_id: str) -> RunHandle: ...

    def progress(self, handle: RunHandle) -> Optional[Dict[str, Any]]: ...

    def result(self, handle: RunHandle) -> Dict[str, Any]:
        """``{"event": "work_result" | "failure", "document": {...}}`` once ``handle.finished()``."""
        ...

    def cancel(self, handle: RunHandle, grace_seconds: float) -> Dict[str, Any]: ...


class HowlPlaneBackend:
    """Delegates every action to existing HowlPlane machinery."""

    def __init__(self, target_repo: Path, private_dir: Path):
        self.target_repo = Path(target_repo)
        self.private_dir = Path(private_dir)

    def _pool(self):
        from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager
        return ProviderPoolManager.from_config(read_only=True, probe_on_start=True)

    def inventory(self) -> Dict[str, Any]:
        from howlplane.control_plane.resource_cli import inventory_document
        return inventory_document(self._pool())

    def route(self, objective: str, resource_id: Optional[str]) -> Dict[str, Any]:
        from howlplane.control_plane.cli import create_task_plan
        from howlplane.control_plane.project_adapter import ProjectAdapter
        pool = self._pool()
        args = argparse.Namespace(objective=objective, task_id=None, risk=None, tier=None, agent=resource_id,
                                  task_class=None, criteria=None, constraints=None, actions=None, skills=None,
                                  preferred_agent=None, repo=str(self.target_repo))
        spec, _ = create_task_plan(ProjectAdapter.discover(self.target_repo), self.target_repo, None, args,
                                   resource_pool=pool)
        return pool.select_resource(spec, role="implementation", explicit_resource_id=resource_id).to_dict()

    def start_run(self, objective: str, resource_id: Optional[str], task_id: str) -> RunHandle:
        argv = [sys.executable, "-m", "howlplane.control_plane", "work", objective, "--execute", "--json",
                "--progress", "always", "--task-id", task_id, "--repo", str(self.target_repo)]
        if resource_id:
            argv += ["--agent", resource_id]
        self.private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=f"run-{task_id}-", suffix=".stderr", dir=self.private_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as stderr:
            # The operator's own environment: the same authority as typing the command.
            process = subprocess.Popen(  # nosec B603 - fixed argv, objective is one verbatim argument
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=stderr, text=True,
                start_new_session=True)
        return RunHandle(task_id=task_id, process=process, stderr_path=Path(name))

    def progress(self, handle: RunHandle) -> Optional[Dict[str, Any]]:
        path = self.target_repo / ".task_runs" / handle.task_id / "progress.json"
        try:
            document = safe_load_json(path) if path.is_file() else None
        except Exception:
            return None
        return document if isinstance(document, dict) else None

    def result(self, handle: RunHandle) -> Dict[str, Any]:
        stdout = handle.process.stdout.read() if handle.process and handle.process.stdout else ""
        code = handle.process.wait() if handle.process else None
        try:
            document = json.loads(stdout) if stdout.strip() else None
        except ValueError:
            document = None
        if isinstance(document, dict) and document.get("schema") == "howlplane.work_result/v1":
            return {"event": "work_result", "document": document}
        error = _last_error_document(handle.stderr_path)
        if error:
            return {"event": "failure", "document": error}
        return {"event": "failure", "document": {
            "schema": "howlplane.error/v1", "code": "RUN_OUTPUT_UNREADABLE",
            "message": f"The governed run exited {code} without a work result.",
            "why": "howlplane work did not print a howlplane.work_result/v1 document.",
            "next_action": f"Inspect {handle.stderr_path} and the task's run directory.",
            "command": f"howlplane status --repo {self.target_repo}"}}

    def cancel(self, handle: RunHandle, grace_seconds: float) -> Dict[str, Any]:
        handle.cancel_requested = True
        process = handle.process
        if process and process.poll() is None:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    process.wait(timeout=grace_seconds)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        from howlplane.control_plane.human_boundary import HumanLifecycleManager
        try:
            outcome = HumanLifecycleManager.cancel(target_repo=str(self.target_repo), task_id=handle.task_id,
                                                   reason="cancelled from howlplane tui", ledger=None)
            recorded = f"Recorded as cancelled (exit {outcome.exit_code}); code changes are kept in the working tree."
        except Exception as exc:  # the stop already happened; say plainly what did not
            recorded = f"The cancellation could not be recorded: {exc}"
        return {"schema": "howlplane.error/v1", "code": "RUN_CANCELLED",
                "message": f"Run {handle.task_id} was stopped at your request.",
                "why": recorded,
                "next_action": "Review the working tree; resume or start a new objective.",
                "command": f"howlplane status --repo {self.target_repo}",
                "retry_safe": "RECONCILE_FIRST"}


def _last_error_document(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if not path or not path.is_file():
        return None
    found = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if line.startswith("{") and '"howlplane.error/v1"' in line:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    found = value
    return found

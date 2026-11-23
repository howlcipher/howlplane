"""Runs one HowlFrame reducer step with no capability grant (ADR 0009).

The reducer decides state, view and intents; this module only moves JSON
across the boundary and refuses anything abnormal. A failed step never
returns a partial state: the caller keeps the previous one.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 - fixed argv, no shell, empty environment
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from howlplane.control_plane.config_loader import TuiSettings
from howlplane.control_plane.howlframe_runner import find_howlframe_binary
from howlplane.control_plane.resources import data_file

STEP_SCHEMA = "howlplane.tui_step/v1"
STEP_RESULT_SCHEMA = "howlplane.tui_step_result/v1"


def reducer_artifact() -> Path:
    """The committed reducer bytecode (checkout copy first, then the packaged copy)."""
    return data_file("integrations", "howlframe", "tui", "tui_reducer.hfbc")


class ReducerError(Exception):
    """A reducer step failed; ``code``, ``why`` and ``next_action`` explain it."""

    def __init__(self, code: str, message: str, why: str, next_action: str, retry: str = "UNKNOWN"):
        super().__init__(message)
        self.code = code
        self.why = why
        self.next_action = next_action
        self.retry = retry

    def failure_document(self) -> Dict[str, Any]:
        """The same shape as ``howlplane.error/v1`` so the reducer explains it uniformly."""
        return {"schema": "howlplane.error/v1", "code": self.code, "message": str(self),
                "why": self.why, "next_action": self.next_action, "command": None,
                "retry_safe": self.retry}


@dataclass
class StepResult:
    state: Dict[str, Any]
    view: List[str]
    prompt: str
    intents: List[Dict[str, Any]] = field(default_factory=list)


class ReducerRunner:
    """Executes ``howlframe run`` for one step under the configured limits."""

    def __init__(self, settings: TuiSettings, howlframe_bin: Optional[str] = None,
                 artifact: Optional[Path] = None):
        self.settings = settings
        self.howlframe_bin = howlframe_bin or find_howlframe_binary()
        self.artifact = Path(artifact) if artifact else reducer_artifact()

    def check_ready(self) -> None:
        if not self.howlframe_bin:
            raise ReducerError(
                "HOWLFRAME_UNAVAILABLE", "The howlframe runtime was not found.",
                "howlplane tui runs its state machine on the HowlFrame VM.",
                "Install HowlFrame or set HOWLFRAME_BIN to the howlframe executable.", "SAFE_TO_RETRY")
        if not self.artifact.is_file():
            raise ReducerError(
                "TUI_ARTIFACT_MISSING", f"The TUI reducer artifact is missing: {self.artifact}",
                "The installation is incomplete.", "Reinstall HowlPlane.", "NEVER_AUTO_RETRY")

    def command(self) -> List[str]:
        s = self.settings
        # No --allow-caps: the reducer runs with an empty grant set.
        return [str(self.howlframe_bin), "run",
                "--max-instructions", str(s.max_instructions),
                "--max-memory-bytes", str(s.max_memory_bytes),
                "--deadline", f"{s.step_deadline_seconds}s",
                str(self.artifact)]

    def step(self, state: Dict[str, Any], event: Dict[str, Any]) -> StepResult:
        self.check_ready()
        document = json.dumps({"schema": STEP_SCHEMA, "state": state, "event": event,
                               "config": {"max_objective_chars": self.settings.max_objective_chars}})
        if len(document.encode("utf-8")) > self.settings.max_step_bytes:
            raise ReducerError(
                "TUI_STEP_TOO_LARGE",
                f"A {event.get('type')} step exceeds tui.max_step_bytes ({self.settings.max_step_bytes}).",
                "The host refuses oversized input before the VM starts.",
                "Raise tui.max_step_bytes in ~/.config/howlplane/config.toml if the document is legitimate.",
                "NEVER_AUTO_RETRY")
        try:
            proc = subprocess.run(  # nosec B603 - fixed argv built above, shell is never used
                self.command(), input=document, capture_output=True, text=True, env={},
                timeout=self.settings.step_deadline_seconds + 5)
        except subprocess.TimeoutExpired as exc:
            raise ReducerError("TUI_STEP_TIMEOUT", "The reducer step did not finish in time.",
                               "The HowlFrame VM exceeded its wall-clock bound.",
                               "Report this with the event type; the previous screen state was kept.",
                               "SAFE_TO_RETRY") from exc
        except OSError as exc:
            raise ReducerError("HOWLFRAME_UNAVAILABLE", f"The howlframe runtime could not start: {exc}",
                               "The configured howlframe executable is not runnable.",
                               "Check HOWLFRAME_BIN or reinstall HowlFrame.", "SAFE_TO_RETRY") from exc
        return self._parse(proc, event)

    def _parse(self, proc: subprocess.CompletedProcess, event: Dict[str, Any]) -> StepResult:
        try:
            out = json.loads(proc.stdout) if proc.stdout.strip() else None
        except ValueError:
            out = None
        if proc.returncode != 0 or not isinstance(out, dict) or out.get("ok") is not True:
            vm_error = _first_json(proc.stderr)
            code = (vm_error or {}).get("code") or (out or {}).get("code") or "TUI_STEP_FAILED"
            detail = (vm_error or {}).get("message") or (out or {}).get("diagnostic") or "no diagnostic"
            raise ReducerError(
                code, f"The TUI state machine refused a {event.get('type')} step: {detail}",
                "HowlFrame reported a failure, or its output was not a step result.",
                "The previous screen state was kept. If this repeats, report the code and event type.",
                "UNKNOWN")
        if out.get("schema") != STEP_RESULT_SCHEMA or not isinstance(out.get("state"), dict):
            raise ReducerError("TUI_STEP_MALFORMED", "The reducer returned an unexpected document.",
                               f"Expected {STEP_RESULT_SCHEMA}.", "Reinstall HowlPlane.", "NEVER_AUTO_RETRY")
        return StepResult(state=out["state"], view=[str(x) for x in out.get("view") or []],
                          prompt=str(out.get("prompt") or "> "), intents=list(out.get("intents") or []))


def _first_json(text: str) -> Optional[Dict[str, Any]]:
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None

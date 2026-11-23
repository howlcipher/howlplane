"""The `howlplane tui` loop: terminal events in, reducer steps, closed intent table out.

The HowlFrame reducer owns state, validation, view and decisions. This loop
only feeds it events, executes the intents in `INTENTS` through the `Backend`,
and paints. Anything a step cannot do leaves the previous state in place.
"""

from __future__ import annotations

import secrets
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from howlplane.control_plane.config_loader import TuiSettings
from howlplane.control_plane.factory.oplog import OperationalLog
from howlplane.control_plane.tui.backend import Backend, RunHandle
from howlplane.control_plane.tui.reducer import ReducerError, ReducerRunner
from howlplane.control_plane.tui.terminal import InputEvent, Terminal

TUI_EVENT_SCHEMA = "howlplane.tui_event/v1"
HEADER = "HowlPlane"


def new_task_id(clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> str:
    return f"TUI-{clock().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def initial_state() -> Dict[str, Any]:
    return {"mode": "idle", "objective": None, "explicit_resource": None, "rows": [], "available": [],
            "notices": []}


class TuiApp:
    """One interactive session for one repository."""

    def __init__(self, backend: Backend, reducer: ReducerRunner, terminal: Terminal, settings: TuiSettings,
                 log: OperationalLog, task_id_factory: Callable[[], str] = new_task_id):
        self.backend = backend
        self.reducer = reducer
        self.terminal = terminal
        self.settings = settings
        self.log = log
        self.task_id_factory = task_id_factory
        self.state = initial_state()
        self.view: List[str] = []
        self.prompt = "> "
        self.events: Deque[Dict[str, Any]] = deque()
        self.run: Optional[RunHandle] = None
        self.last_progress: Optional[Dict[str, Any]] = None
        self.host_error: Optional[str] = None
        self.done = False
        # The closed intent table: the reducer can propose nothing else.
        self.intents: Dict[str, Callable[[Dict[str, Any]], None]] = {
            "inventory_refresh": self._inventory_refresh,
            "route_preview": self._route_preview,
            "run_objective": self._run_objective,
            "cancel": self._cancel,
            "quit": self._quit,
        }

    # -- reducer ------------------------------------------------------------------
    def dispatch(self, event: Dict[str, Any]) -> None:
        self.events.append(event)
        while self.events:
            current = self.events.popleft()
            try:
                result = self.reducer.step(self.state, current)
            except ReducerError as exc:
                self.host_error = f"FAILED: {exc} ({exc.code}). {exc.next_action}"
                self.log.emit(exc.code, str(exc), "ERROR", source="tui.reducer", event_type=current.get("type"),
                              retry=exc.retry, task_id=self.state.get("task_id"))
                if exc.code == "HOWLFRAME_UNAVAILABLE" or exc.code == "TUI_ARTIFACT_MISSING":
                    self.done = True
                continue
            self.host_error = None
            self.state, self.view, self.prompt = result.state, result.view, result.prompt
            for intent in result.intents:
                self._execute(intent)

    def _execute(self, intent: Dict[str, Any]) -> None:
        kind = intent.get("kind")
        handler = self.intents.get(kind)
        if handler is None:
            self.log.emit("TUI_INTENT_REFUSED", f"unknown intent {kind!r}", "WARNING", source="tui.host")
            return
        self.log.emit("TUI_INTENT", f"executing {kind}", "INFO", source="tui.host", intent=kind,
                      task_id=self.state.get("task_id"))
        try:
            handler(intent)
        except Exception as exc:  # a backend failure is a displayed failure, never a crash
            self.log.emit("TUI_INTENT_FAILED", f"{kind} failed: {type(exc).__name__}", "ERROR",
                          source="tui.host", intent=kind)
            self.events.append({"type": "failure", "ends_run": kind == "run_objective", "document": {
                "schema": "howlplane.error/v1", "code": "TUI_ACTION_FAILED",
                "message": f"{kind} failed: {exc}",
                "why": "HowlPlane raised an error while carrying out the action.",
                "next_action": "Check readiness, then try again.", "command": "howlplane doctor"}})

    # -- intents --------------------------------------------------------------------
    def _inventory_refresh(self, intent: Dict[str, Any]) -> None:
        self.events.append({"type": "inventory", "document": self.backend.inventory()})

    def _route_preview(self, intent: Dict[str, Any]) -> None:
        objective = intent.get("objective")
        if not objective:
            return
        self.events.append({"type": "inventory", "document": self.backend.inventory()})
        self.events.append({"type": "route_preview",
                            "document": self.backend.route(objective, intent.get("resource_id"))})

    def _run_objective(self, intent: Dict[str, Any]) -> None:
        if self.run is not None and not self.run.finished():
            return
        task_id = self.task_id_factory()
        self.run = self.backend.start_run(intent["objective"], intent.get("resource_id"), task_id)
        self.last_progress = None
        self.log.emit("TUI_RUN_STARTED", "governed run started", "INFO", source="tui.host", task_id=task_id,
                      resource_id=intent.get("resource_id"))
        self.events.append({"type": "run_started", "task_id": task_id})

    def _cancel(self, intent: Dict[str, Any]) -> None:
        if self.run is None or self.run.finished():
            return
        document = self.backend.cancel(self.run, self.settings.cancel_grace_seconds)
        self.log.emit("TUI_RUN_CANCELLED", "governed run cancelled", "WARNING", source="tui.host",
                      task_id=self.run.task_id, retry=document.get("retry_safe"))
        self.run = None
        self.events.append({"type": "failure", "ends_run": True, "document": document})

    def _quit(self, intent: Dict[str, Any]) -> None:
        if self.run is not None and not self.run.finished():
            self._cancel({"kind": "cancel"})
        self.done = True

    # -- run supervision --------------------------------------------------------------
    def poll_run(self) -> None:
        if self.run is None:
            return
        for progress in self.backend.progress(self.run):
            if progress != self.last_progress:
                self.last_progress = progress
                self.dispatch({"type": "progress", "document": progress})
        if self.run is not None and self.run.finished():
            handle, self.run = self.run, None
            outcome = self.backend.result(handle)
            final = (outcome.get("document") or {}).get("final_state") or (outcome.get("document") or {}).get("code")
            self.log.emit("TUI_RUN_FINISHED", "governed run finished", "INFO", source="tui.host",
                          task_id=handle.task_id, outcome=final)
            event = {"type": outcome["event"], "document": outcome["document"]}
            if outcome["event"] == "failure":
                event["ends_run"] = True
            self.dispatch(event)

    # -- loop ---------------------------------------------------------------------------
    def paint(self) -> None:
        view = list(self.view) + ([self.host_error] if self.host_error else [])
        self.terminal.paint(HEADER, view, self.prompt)

    def handle_input(self, event: InputEvent) -> None:
        if event.kind == "line":
            self.dispatch({"type": "line", "text": event.text})
        elif event.kind == "interrupt":
            # Ctrl-C stops a run; when idle it leaves.
            if self.run is not None:
                self.dispatch({"type": "line", "text": "/cancel"})
            else:
                self._quit({"kind": "quit"})
        elif event.kind == "eof":
            self._quit({"kind": "quit"})

    def run_loop(self, max_idle_polls: Optional[int] = None) -> int:
        self.log.emit("TUI_STARTED", "session started", "INFO", source="tui.host")
        self.dispatch({"type": "line", "text": "/refresh"})
        polls = 0
        try:
            while not self.done:
                self.paint()
                event = self.terminal.read(self.settings.progress_poll_seconds if self.run else None)
                if event is not None:
                    self.handle_input(event)
                self.poll_run()
                if max_idle_polls is not None:
                    polls += 1
                    if polls > max_idle_polls:
                        break
        finally:
            if self.run is not None and not self.run.finished():
                self._cancel({"kind": "cancel"})
            self.log.emit("TUI_STOPPED", "session stopped", "INFO", source="tui.host")
        return 0


def session_log(state_root: Path, session_id: str) -> OperationalLog:
    return OperationalLog(state_root, {"session_id": session_id}, filename="tui_events.jsonl",
                          schema=TUI_EVENT_SCHEMA, sequenced=True, private=True)


def default_state_root() -> Path:
    import os
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "howlplane" / "tui"

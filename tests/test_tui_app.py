"""`howlplane tui` session loop with a recording fake backend and the real reducer.

The fake backend proves what crosses into HowlPlane: previews never start a
run, a run needs a typed confirmation, cancel and quit stop it, and backend
or reducer failures are displayed without losing state.
"""

import json
import stat
from pathlib import Path

import pytest

from howlplane.control_plane.config_loader import TuiSettings
from howlplane.control_plane.howlframe_runner import find_howlframe_binary
from howlplane.control_plane.tui.app import TUI_EVENT_SCHEMA, TuiApp, session_log
from howlplane.control_plane.tui.backend import RunHandle
from howlplane.control_plane.tui.reducer import ReducerError, ReducerRunner
from howlplane.control_plane.tui.terminal import InputEvent, Terminal

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "integrations" / "howlframe" / "tui00" / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeHandle(RunHandle):
    def __init__(self, task_id):
        super().__init__(task_id=task_id, process=None)
        self.done = False

    def finished(self):
        return self.done


class FakeBackend:
    def __init__(self, inventory="ai_resources_codex_ready", route="route_selected_unprobed"):
        self.calls = []
        self._inventory, self._route = inventory, route
        self.handle = None
        self.progress_doc = None
        self.outcome = {"event": "work_result", "document": fixture("work_result_complete")}

    def inventory(self):
        self.calls.append(("inventory",))
        return fixture(self._inventory)

    def route(self, objective, resource_id):
        self.calls.append(("route", objective, resource_id))
        return fixture(self._route)

    def start_run(self, objective, resource_id, task_id):
        self.calls.append(("start_run", objective, resource_id, task_id))
        self.handle = FakeHandle(task_id)
        return self.handle

    def progress(self, handle):
        return self.progress_doc

    def result(self, handle):
        self.calls.append(("result", handle.task_id))
        return self.outcome

    def cancel(self, handle, grace_seconds):
        self.calls.append(("cancel", handle.task_id))
        handle.done = True
        return {"schema": "howlplane.error/v1", "code": "RUN_CANCELLED", "message": "stopped",
                "why": "requested", "next_action": "review", "command": None, "retry_safe": "RECONCILE_FIRST"}


class ScriptTerminal(Terminal):
    """Feeds scripted input; ``None`` entries are idle polls."""

    def __init__(self, script, on_poll=None):
        self.script = list(script)
        self.frames = []
        self.on_poll = on_poll

    def read(self, timeout):
        if not self.script:
            return InputEvent("eof")
        item = self.script.pop(0)
        if callable(item):
            item()
            return None
        return None if item is None else InputEvent(*item) if isinstance(item, tuple) else InputEvent("line", item)

    def paint(self, header, view, prompt):
        self.frames.append((list(view), prompt))


@pytest.fixture(scope="module")
def reducer():
    hf = find_howlframe_binary()
    assert hf, "Real howlframe binary is required; set HOWLFRAME_BIN or put it on PATH"
    return ReducerRunner(TuiSettings(), howlframe_bin=hf)


def make_app(tmp_path, reducer, backend, script):
    terminal = ScriptTerminal(script)
    log = session_log(tmp_path / "state", "session-test")
    app = TuiApp(backend, reducer, terminal, TuiSettings(progress_poll_seconds=0.1), log,
                 task_id_factory=lambda: "TUI-TEST-1")
    return app, terminal, tmp_path / "state" / "logs" / "tui_events.jsonl"


def names(backend):
    return [call[0] for call in backend.calls]


def test_preview_never_starts_a_run(tmp_path, reducer):
    backend = FakeBackend()
    app, terminal, _ = make_app(tmp_path, reducer, backend, ["Add a /health endpoint", "/agent codex", "/quit"])
    assert app.run_loop() == 0
    assert "start_run" not in names(backend)
    assert ("route", "Add a /health endpoint", "codex") in backend.calls
    assert any("Route:     codex for implementation" in view for view, _ in terminal.frames)


def test_run_needs_typed_confirmation_and_reports_the_outcome(tmp_path, reducer):
    backend = FakeBackend()

    def finish():
        backend.progress_doc = {"schema": "howlplane.task_progress/v1", "task_id": "TUI-TEST-1",
                                "phase": "IMPLEMENTING", "state": "RUNNING", "resource_id": "codex",
                                "elapsed_seconds": 3, "details": None}

    def complete():
        backend.handle.done = True

    app, terminal, _ = make_app(tmp_path, reducer, backend, ["fix it", "/run", "n", "/run", "yes", finish, complete,
                                                             "/quit"])
    app.run_loop()
    assert names(backend).count("start_run") == 1
    assert ("start_run", "fix it", None, "TUI-TEST-1") in backend.calls
    views = [line for view, _ in terminal.frames for line in view]
    assert "! Run not started." in views
    assert any(line.startswith("Progress:  IMPLEMENTING codex 3s") for line in views)
    assert "Outcome:   complete (exit 0)" in views


@pytest.mark.parametrize("stop", [("interrupt",), "/cancel", "/quit"])
def test_running_objective_can_be_stopped(tmp_path, reducer, stop):
    backend = FakeBackend()
    app, terminal, log_path = make_app(tmp_path, reducer, backend, ["fix it", "/run", "y", None, stop, "/quit"])
    app.run_loop()
    assert ("cancel", "TUI-TEST-1") in backend.calls
    if stop != "/quit":
        assert any("Code:      RUN_CANCELLED" in view for view, _ in terminal.frames)
        assert any("Retry:     RECONCILE_FIRST" in view for view, _ in terminal.frames)
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert "TUI_RUN_CANCELLED" in [e["code"] for e in events]


def test_interrupt_when_idle_leaves(tmp_path, reducer):
    backend = FakeBackend()
    app, _, _ = make_app(tmp_path, reducer, backend, [("interrupt",), "never read"])
    app.run_loop()
    assert app.done and "start_run" not in names(backend)


def test_backend_failure_is_displayed_not_raised(tmp_path, reducer):
    backend = FakeBackend()

    def broken(objective, resource_id):
        raise RuntimeError("pool exploded")
    backend.route = broken
    app, terminal, _ = make_app(tmp_path, reducer, backend, ["fix it", "/quit"])
    app.run_loop()
    assert any("Code:      TUI_ACTION_FAILED" in view for view, _ in terminal.frames)


def test_reducer_failure_keeps_the_previous_state(tmp_path, reducer):
    backend = FakeBackend()
    app, terminal, log_path = make_app(tmp_path, reducer, backend, ["fix it"])
    app.run_loop()
    before = dict(app.state)

    class Broken:
        def step(self, state, event):
            raise ReducerError("LIMIT_EXCEEDED", "instruction limit exceeded", "budget", "raise tui.max_instructions")
    app.reducer = Broken()
    app.dispatch({"type": "line", "text": "another"})
    assert app.state == before
    assert app.host_error.startswith("FAILED: instruction limit exceeded (LIMIT_EXCEEDED)")
    assert "LIMIT_EXCEEDED" in log_path.read_text()


def test_unknown_intent_is_refused(tmp_path, reducer):
    backend = FakeBackend()
    app, _, log_path = make_app(tmp_path, reducer, backend, [])
    app._execute({"kind": "rm_rf"})
    assert "TUI_INTENT_REFUSED" in log_path.read_text()
    assert backend.calls == []


def test_session_log_is_private_versioned_and_sequenced(tmp_path, reducer):
    app, _, log_path = make_app(tmp_path, reducer, FakeBackend(), ["fix it", "/quit"])
    app.run_loop()
    events = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert {e["schema"] for e in events} == {TUI_EVENT_SCHEMA}
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert {e["session_id"] for e in events} == {"session-test"}
    assert events[0]["code"] == "TUI_STARTED" and events[-1]["code"] == "TUI_STOPPED"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700

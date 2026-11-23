"""`howlplane tui` reducer: the committed HowlFrame artifact and its step contract.

Runs the real HowlFrame runtime (HOWLFRAME_BIN or PATH) against the committed
bytecode. No provider is involved: every document is a captured fixture.
"""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from howlplane.control_plane.config_loader import TuiSettings
from howlplane.control_plane.howlframe_runner import find_howlframe_binary
from howlplane.control_plane.tui import reducer as reducer_mod
from howlplane.control_plane.tui.app import initial_state
from howlplane.control_plane.tui.reducer import ReducerError, ReducerRunner

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
TUI_DIR = ROOT / "integrations" / "howlframe" / "tui"
FIXTURES = ROOT / "integrations" / "howlframe" / "tui00" / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def runner():
    hf = find_howlframe_binary()
    assert hf, "Real howlframe binary is required; set HOWLFRAME_BIN or put it on PATH"
    return ReducerRunner(TuiSettings(), howlframe_bin=hf)


def steps(runner, events, state=None):
    state = state or initial_state()
    result = None
    for event in events:
        result = runner.step(state, event)
        state = result.state
    return result


def kinds(result):
    return [intent["kind"] for intent in result.intents]


# -- artifact -------------------------------------------------------------------

def test_provenance_matches_committed_bytes():
    provenance = json.loads((TUI_DIR / "ARTIFACT_PROVENANCE.json").read_text(encoding="utf-8"))
    assert provenance["producer_source_revision"] == "928c00503f1de75e21487d020864076e67323eb4"
    for name, digest in provenance["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name


def test_committed_artifact_is_what_the_source_builds(tmp_path, runner):
    """Fails when tui_reducer.howl or tui_core.howl changed without scripts/build_tui_reducer.sh."""
    shutil.copy(TUI_DIR / "tui_reducer.howl", tmp_path)
    shutil.copy(TUI_DIR / "tui_core.howl", tmp_path)
    out = tmp_path / "rebuilt.hfbc"
    built = subprocess.run([runner.howlframe_bin, "build", str(tmp_path / "tui_reducer.howl"), "-o", str(out)],
                           capture_output=True, text=True, timeout=60)
    assert built.returncode == 0, built.stdout + built.stderr
    assert out.read_bytes() == (TUI_DIR / "tui_reducer.hfbc").read_bytes()


def test_reducer_runs_with_no_capability_grant(runner):
    command = runner.command()
    assert "--allow-caps" not in command
    assert command[command.index("--max-instructions") + 1] == str(TuiSettings().max_instructions)


# -- interaction ----------------------------------------------------------------

def test_objective_proposes_a_route_preview(runner):
    result = steps(runner, [{"type": "line", "text": "  Add a /health endpoint "}])
    assert result.state["objective"] == "Add a /health endpoint"
    assert result.intents == [{"kind": "route_preview", "objective": "Add a /health endpoint", "resource_id": None}]


def test_run_needs_an_objective_and_a_selected_route(runner):
    assert kinds(steps(runner, [{"type": "line", "text": "/run"}])) == []
    blocked = steps(runner, [{"type": "line", "text": "fix it"},
                             {"type": "route_preview", "document": fixture("route_blocked_local_only")},
                             {"type": "line", "text": "/run"}])
    assert blocked.state["mode"] == "idle" and kinds(blocked) == []
    assert any("BLOCKED" in note for note in blocked.state["notices"])


def selected(runner):
    return steps(runner, [{"type": "inventory", "document": fixture("ai_resources_codex_ready")},
                          {"type": "line", "text": "fix it"},
                          {"type": "route_preview", "document": fixture("route_selected_unprobed")},
                          {"type": "line", "text": "/run"}])


def test_run_requires_explicit_confirmation(runner):
    confirm = selected(runner)
    assert confirm.state["mode"] == "confirm" and kinds(confirm) == []
    assert confirm.prompt.startswith("Run with codex?")
    yes = runner.step(confirm.state, {"type": "line", "text": "y"})
    assert yes.intents == [{"kind": "run_objective", "objective": "fix it", "resource_id": None}]
    assert yes.state["mode"] == "running"


@pytest.mark.parametrize("answer", ["", "n", "no", "run it"])
def test_anything_but_yes_declines(runner, answer):
    declined = runner.step(selected(runner).state, {"type": "line", "text": answer})
    assert kinds(declined) == [] and declined.state["mode"] == "idle"


def test_quit_at_the_confirmation_quits(runner):
    assert kinds(runner.step(selected(runner).state, {"type": "line", "text": "/quit"})) == ["quit"]


def test_running_mode_only_accepts_cancel(runner):
    running = runner.step(selected(runner).state, {"type": "line", "text": "y"})
    running = runner.step(running.state, {"type": "run_started", "task_id": "TUI-1"})
    assert kinds(runner.step(running.state, {"type": "line", "text": "another objective"})) == []
    assert runner.step(running.state, {"type": "line", "text": "/cancel"}).intents == [
        {"kind": "cancel", "task_id": "TUI-1"}]


def test_progress_and_outcome_are_shown(runner):
    running = runner.step(selected(runner).state, {"type": "line", "text": "y"})
    progress = {"schema": "howlplane.task_progress/v1", "task_id": "TUI-1", "phase": "REVIEWING",
                "state": "RUNNING", "resource_id": "codex", "elapsed_seconds": 42, "details": None}
    shown = runner.step(running.state, {"type": "progress", "document": progress})
    assert any(line.startswith("Progress:  REVIEWING codex 42s") for line in shown.view)
    done = runner.step(shown.state, {"type": "work_result", "document": fixture("work_result_failed")})
    assert done.state["mode"] == "idle"
    assert "Outcome:   failed (exit 1)" in done.view
    assert "Verification (reported by HowlPlane): none" in done.view


def test_explicit_provider_must_be_usable(runner):
    state = steps(runner, [{"type": "inventory", "document": fixture("ai_resources_codex_missing")}]).state
    refused = runner.step(state, {"type": "line", "text": "/agent codex"})
    assert refused.state["explicit_resource"] is None
    assert any("not currently usable" in note for note in refused.state["notices"])


def test_notices_survive_follow_up_events_until_the_next_input(runner):
    state = steps(runner, [{"type": "inventory", "document": fixture("ai_resources_codex_missing")},
                           {"type": "line", "text": "fix it"}]).state
    warned = runner.step(state, {"type": "line", "text": "/agent codex"})
    after = runner.step(warned.state, {"type": "route_preview", "document": fixture("route_blocked_local_only")})
    assert any("not currently usable" in note for note in after.state["notices"])
    assert runner.step(after.state, {"type": "line", "text": "/help"}).state["notices"][0].startswith("Type an")


def test_failure_documents_are_explained(runner):
    shown = runner.step(initial_state(), {"type": "failure", "ends_run": True,
                                          "document": fixture("error_not_a_repo")})
    assert "Code:      NOT_A_GIT_REPOSITORY" in shown.view
    assert "Retry:     UNKNOWN" in shown.view


# -- refusals ---------------------------------------------------------------------

def test_unknown_inventory_version_fails_closed(runner):
    doc = dict(fixture("ai_resources_codex_ready"), schema="howlplane.ai_resources/v2")
    result = runner.step(initial_state(), {"type": "inventory", "document": doc})
    assert result.state["failure"]["code"] == "UNSUPPORTED_INVENTORY_SCHEMA"


def test_instruction_budget_is_enforced(runner):
    tight = ReducerRunner(TuiSettings(max_instructions=1000), howlframe_bin=runner.howlframe_bin)
    big = fixture("ai_resources_codex_ready")
    big["resources"] = big["resources"] * 20
    with pytest.raises(ReducerError) as caught:
        tight.step(initial_state(), {"type": "inventory", "document": big})
    assert caught.value.code == "LIMIT_EXCEEDED"


def test_oversized_step_is_refused_before_the_vm_starts(runner, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("the VM must not start for an oversized step")
    monkeypatch.setattr(reducer_mod.subprocess, "run", never)
    small = ReducerRunner(TuiSettings(max_step_bytes=16 * 1024), howlframe_bin=runner.howlframe_bin)
    with pytest.raises(ReducerError) as caught:
        small.step(initial_state(), {"type": "line", "text": "x" * 20000})
    assert caught.value.code == "TUI_STEP_TOO_LARGE"


def test_missing_runtime_is_an_actionable_error(monkeypatch):
    monkeypatch.setattr(reducer_mod, "find_howlframe_binary", lambda: None)
    with pytest.raises(ReducerError) as caught:
        ReducerRunner(TuiSettings()).check_ready()
    assert caught.value.code == "HOWLFRAME_UNAVAILABLE"
    assert "HOWLFRAME_BIN" in caught.value.next_action


def test_owner_boundary_shows_the_commands_but_proposes_nothing(runner):
    """Show-only approvals: the operator runs approve/reject; the reducer never proposes them."""
    doc = dict(fixture("work_result_complete"), final_state="awaiting_human", exit_code=2, next_action={
        "command": "howlplane approve T-9", "then": "howlplane resume T-9",
        "alternatives": ["howlplane reject T-9"], "reason": "OWNER_REQUIRED",
        "detail": "Read /repo/.task_runs/T-9/decision_packet.md first."})
    shown = runner.step(initial_state(), {"type": "work_result", "document": doc})
    assert shown.intents == []
    for line in ("Outcome:   awaiting_human (exit 2)",
                 "Detail:    Read /repo/.task_runs/T-9/decision_packet.md first.",
                 "Next:      howlplane approve T-9", "Then:      howlplane resume T-9",
                 "Or:        howlplane reject T-9"):
        assert line in shown.view

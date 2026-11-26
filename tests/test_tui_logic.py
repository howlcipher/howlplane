"""The TUI's decisions, replayed through the real program (`howlframe run --replay`).

The HowlFrame runtime feeds recorded events to the committed `tui_app.hfbc`
and writes one `howlframe.terminal_replay/v1` record per handled event: the
state, view, prompt and effects. No effect runs, so these tests need no grant,
no terminal and no HowlPlane child; replies are the TUI-00 fixtures, given
back as `process_exit` events.
"""

import json
import subprocess
from pathlib import Path

import pytest

from howlplane.control_plane.howlframe_runner import find_howlframe_binary
from howlplane.control_plane.tui import howlframe_engine

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "integrations" / "howlframe" / "tui00" / "fixtures"
ENTRY, REPO = "/opt/hp/entry", "/work/repo"
CFG = {"cli": [ENTRY], "repo": REPO, "task_runs": f"{REPO}/.task_runs", "log_path": "/state/logs/tui_events.jsonl",
       "session_id": "s1", "max_objective_chars": 64, "poll_ms": 500, "cancel_grace_ms": 3000,
       "cancel_reason": howlframe_engine.CANCEL_REASON}
OBJECTIVE = "Add a /health endpoint"


def doc(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def start():
    return {"type": "start", "tty": True, "cols": 80, "rows": 24}


def line(text, pasted=False):
    return {"type": "line", "text": text, "pasted": pasted}


def exited(pid, stdout="", code=0, stderr=""):
    return {"type": "process_exit", "id": pid, "exit_code": code, "stdout": stdout, "stderr": stderr, "error": None}


READY = doc("ai_resources_codex_ready.json")
MISSING = doc("ai_resources_codex_missing.json")
SELECTED = doc("route_selected_unprobed.json")
BLOCKED = doc("route_blocked_local_only.json")
# Started, inventory in, an objective previewed: inventory-2 and route-1 answered.
PREVIEWED = [start(), exited("inventory-1", READY), line(OBJECTIVE), exited("route-1", SELECTED), exited("inventory-2", READY)]


class Replay:
    def __init__(self, done):
        assert done.returncode in (0, 143), done.stderr
        self.code = done.returncode
        self.records = [json.loads(raw) for raw in done.stdout.splitlines() if raw.strip()]
        assert all(r["ok"] for r in self.records), [r["error"] for r in self.records if not r["ok"]]

    def view(self, i=-1):
        return [e["text"] for e in self.records[i]["view"]]

    def styles(self, i=-1):
        return {e["text"]: e["style"] for e in self.records[i]["view"]}

    def notices(self, i=-1):
        return [t[2:] for t in self.view(i) if t.startswith("! ")]

    def effects(self, i=-1, kind=None):
        return [e for e in self.records[i]["effects"] if kind is None or e["kind"] == kind]

    def state(self, i=-1):
        return self.records[i]["state"]

    def prompt(self, i=-1):
        return self.records[i]["prompt"]


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    howlframe = find_howlframe_binary()
    assert howlframe, "Real howlframe binary is required; set HOWLFRAME_BIN or put it on PATH"

    def run(events, cfg=CFG):
        path = tmp_path_factory.mktemp("replay") / "events.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        done = subprocess.run([howlframe, "run", "--replay", str(path), str(howlframe_engine.app_artifact()), json.dumps(cfg)],
                              capture_output=True, text=True, timeout=60)
        return Replay(done)
    return run


def run_started(replay, *more):
    """A previewed objective, confirmed by typing y; returns the replay and the task id."""
    r = replay(PREVIEWED + [line("/run"), line("y")] + list(more))
    task = r.records[6]["state"]["task_id"]
    return r, task


# -- previews ---------------------------------------------------------------------------------

def test_start_probes_the_inventory_and_logs_the_session(replay):
    r = replay([start()])
    (probe,) = r.effects(kind="exec_start")
    assert probe["id"] == "inventory-1" and probe["argv"] == [ENTRY, "providers", "--json", "--repo", REPO]
    (log,) = r.effects(kind="append_log")
    event = json.loads(log["line"])
    assert log["path"] == CFG["log_path"]
    assert (event["schema"], event["seq"], event["code"], event["session_id"], event["source"]) == \
        ("howlplane.tui_event/v1", 1, "TUI_STARTED", "s1", "tui.howlframe")
    assert "Resources: not loaded (/refresh)" in r.view()


def test_a_preview_probes_the_inventory_and_the_route_together(replay):
    r = replay([start(), exited("inventory-1", READY), line(OBJECTIVE)])
    probes = {e["id"]: e["argv"] for e in r.effects(kind="exec_start")}
    assert probes == {"inventory-2": [ENTRY, "providers", "--json", "--repo", REPO],
                      "route-1": [ENTRY, "route", "--probe", "--json", "--repo", REPO, "--", OBJECTIVE]}
    r = replay(PREVIEWED)
    assert "Route:     codex for implementation" in r.view()
    assert r.styles()["Route:     codex for implementation"] == "ok"
    assert r.notices(3) == ["Type /run to run this objective, or edit it."]
    assert r.state()["inventory_live"] == "" and r.state()["route_live"] == ""


def test_replies_from_replaced_probes_are_ignored(replay):
    r = replay([start(), exited("inventory-1", READY), line("first objective"), line("second objective"),
                exited("inventory-2", MISSING), exited("route-1", BLOCKED),
                {"type": "effect_failed", "kind": "exec_start", "id": "route-1", "error": {"code": "LIMIT_EXCEEDED", "message": "x"}},
                exited("route-2", SELECTED), exited("inventory-3", READY)])
    for i in range(4, 7):  # nothing from the first objective's probes reaches the view
        assert not any("became unavailable" in n for n in r.notices(i))
        assert not any(t.startswith(("Route:", "FAILED")) for t in r.view(i)), r.view(i)
    assert "Route:     codex for implementation" in r.view()
    assert r.state()["objective"] == "second objective"


def test_a_selection_the_inventory_does_not_report_usable_is_flagged_once(replay):
    flag = "Preview selected a provider the latest inventory does not report as usable; HowlPlane re-checks readiness at execution."
    for answers in ([exited("route-1", SELECTED), exited("inventory-2", MISSING)],
                    [exited("inventory-2", MISSING), exited("route-1", SELECTED)]):
        r = replay([start(), exited("inventory-1", MISSING), line(OBJECTIVE)] + answers + [{"type": "timer", "id": "poll"}])
        assert flag not in r.notices(3), "not before both replies are in"
        assert r.notices(4).count(flag) == 1 and r.notices(5).count(flag) == 1, [r.notices(i) for i in range(len(r.records))]
        assert r.state()["route"]["readiness_confirmed"] is False
    r = replay(PREVIEWED)
    assert all(flag not in r.notices(i) for i in range(len(r.records)))


def test_explicit_provider_choice_and_losing_it(replay):
    r = replay(PREVIEWED + [line("/agent codex")])
    route = next(e for e in r.effects(kind="exec_start") if e["id"].startswith("route-"))
    assert route["argv"][-4:] == ["--agent", "codex", "--", OBJECTIVE]
    assert "Provider:  codex" in r.view()
    r = replay(PREVIEWED + [line("/agent codex"), exited("route-2", SELECTED), exited("inventory-3", MISSING)])
    assert r.notices()[1:] == ["codex became unavailable: MISSING_EXECUTABLE",
                               "Explicit provider choice cleared; HowlPlane will select automatically.",
                               "No configured provider is currently usable."]
    assert r.state()["explicit_resource"] is None and "route" not in r.state()
    (again,) = r.effects(kind="exec_start")
    assert again["id"] == "route-3" and "--agent" not in again["argv"], "the preview is asked again without the lost choice"
    r = replay(PREVIEWED + [line("/agent nosuch"), line("/agent auto")])
    assert r.notices(5) == ["Provider nosuch is not currently usable; selection unchanged."]
    assert r.notices() == ["Automatic provider selection."]


def test_unknown_contract_versions_are_refused(replay):
    newer = json.dumps({**json.loads(READY), "schema": "howlplane.ai_resources/v2"})
    r = replay([start(), exited("inventory-1", newer)])
    failure = r.state()["failure"]
    assert (failure["code"], failure["retry"]) == ("UNSUPPORTED_INVENTORY_SCHEMA", "NEVER_AUTO_RETRY")
    assert r.styles()["FAILED:    Expected howlplane.ai_resources/v1, got howlplane.ai_resources/v2"] == "error"
    r = replay([start(), exited("inventory-1", READY), line(OBJECTIVE), exited("route-1", json.dumps({"schema": "x/v9"}))])
    assert r.state()["failure"]["code"] == "UNSUPPORTED_ROUTE_SCHEMA"


def test_objectives_are_validated_before_any_probe(replay):
    r = replay([start(), line(""), line("x" * 65)])
    assert r.notices(1) == ["Type an objective, or /help."]
    failure = r.state()["failure"]
    assert (failure["code"], failure["retry"]) == ("OBJECTIVE_TOO_LONG", "SAFE_TO_RETRY")
    assert r.effects(1) == [] and r.effects(2) == []


# -- the run confirmation ---------------------------------------------------------------------

@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES"])
def test_a_typed_yes_starts_the_run(replay, answer):
    r = replay(PREVIEWED + [line("/run"), line(answer)])
    assert r.prompt(5) == "Run with codex? [y/N] "
    task = r.state()["task_id"]
    assert task.startswith("TUI-")
    (run,) = r.effects(kind="exec_start")
    assert run["id"] == "run" and run["argv"] == [ENTRY, "work", "--execute", "--json", "--progress", "always",
                                                  "--task-id", task, "--repo", REPO, "--", OBJECTIVE]
    assert r.effects(kind="timer") == [{"kind": "timer", "id": "poll", "ms": 500}]
    assert json.loads(r.effects(kind="append_log")[0]["line"])["code"] == "TUI_RUN_STARTED"
    assert r.prompt() == "running (/cancel) > "


@pytest.mark.parametrize("answer", ["y", "yes", "YES"])
def test_a_pasted_yes_never_starts_a_run(replay, answer):
    r = replay(PREVIEWED + [line("/run"), line(answer, pasted=True)])
    assert r.effects() == [] and r.state()["mode"] == "confirm"
    assert r.notices() == ["A pasted answer cannot start a run. Type y to run it, or anything else to cancel."]
    assert r.prompt() == "Run with codex? [y/N] "
    r = replay(PREVIEWED + [line("/run"), line(answer, pasted=True), line("y")])
    assert [e["id"] for e in r.effects(kind="exec_start")] == ["run"], "typing it afterwards still runs"


@pytest.mark.parametrize("answer,pasted", [("n", False), ("", False), ("no thanks", False), ("n", True)])
def test_any_other_answer_declines(replay, answer, pasted):
    r = replay(PREVIEWED + [line("/run"), line(answer, pasted=pasted)])
    assert r.effects() == [] and r.state()["mode"] == "idle"
    assert r.notices() == ["Run not started."]


def test_quit_at_the_confirmation_leaves(replay):
    r = replay(PREVIEWED + [line("/run"), line("/quit")])
    assert r.effects(kind="quit") == [{"kind": "quit", "code": 0}] and r.code == 0
    assert json.loads(r.effects(kind="append_log")[0]["line"])["code"] == "TUI_STOPPED"


def test_run_needs_a_selected_route(replay):
    r = replay([start(), exited("inventory-1", READY), line("/run"), line(OBJECTIVE), line("/run"),
                exited("route-1", BLOCKED), line("/run")])
    assert r.notices(2) == ["Type an objective first."]
    assert r.notices(4) == ["Wait for the route preview first."]
    assert r.styles(5)["Route:     BLOCKED NO_ELIGIBLE_AI_RESOURCE"] == "warning"
    assert r.notices() == ["The route is BLOCKED; nothing can run. Fix the exclusions or /refresh."]
    assert r.state()["mode"] == "idle"


# -- a running objective ----------------------------------------------------------------------

def test_while_running_only_cancel_and_quit_act(replay):
    r, _ = run_started(replay, line("another objective"), line("/run"))
    assert r.notices(7) == ["A run is in progress. Type /cancel to stop it."]
    assert r.notices() == ["A run is in progress. Type /cancel to stop it."]
    assert r.effects(7) == [] and r.effects(8) == []


def test_progress_events_update_the_view_and_failover_is_noticed(replay):
    def event(seq, kind, message=None):
        e = {"schema": "howlplane.task_progress_event/v1", "seq": seq, "task_id": "T", "kind": kind, "phase": "implementation",
             "state": "running", "resource_id": "codex", "elapsed_seconds": 12, "details": "editing files"}
        return json.dumps({**e, "message": message} if message else e)
    r, task = run_started(replay, {"type": "timer", "id": "poll"},
                          {"type": "tail_lines", "id": "progress", "path": "p", "offset": 300, "missing": False,
                           "lines": [event(1, "phase"), "not json", json.dumps({"schema": "other/v1"}),
                                     event(2, "failover", "codex unavailable | switching")]})
    (tail,) = r.effects(7, kind="tail")
    assert tail == {"kind": "tail", "id": "progress", "offset": 0, "path": f"{REPO}/.task_runs/{task}/progress_events.jsonl"}
    assert r.effects(7, kind="timer") == [{"kind": "timer", "id": "poll", "ms": 500}], "polling continues while the run lives"
    assert "Progress:  implementation codex 12s editing files" in r.view()
    assert r.notices() == ["failover: codex unavailable | switching"]
    assert r.state()["events_offset"] == 300


def test_cancel_interrupts_waits_kills_then_records(replay):
    r, task = run_started(replay, {"type": "interrupt"}, {"type": "timer", "id": "grace"},
                          exited("run", code=-1), exited("cancel", json.dumps({"ok": True})))
    assert r.effects(7, kind="exec_signal") == [{"kind": "exec_signal", "id": "run", "signal": "interrupt"}]
    assert r.effects(7, kind="timer") == [{"kind": "timer", "id": "grace", "ms": 3000}]
    assert r.prompt(7) == "cancelling > "
    assert r.effects(8) == [{"kind": "exec_signal", "id": "run", "signal": "kill"}]
    (cancel,) = r.effects(9, kind="exec_start")
    assert cancel["argv"] == [ENTRY, "cancel", "--json", "--repo", REPO, "--reason", howlframe_engine.CANCEL_REASON, "--", task]
    assert r.effects(9, kind="tail"), "the last progress is read after the run ends"
    failure = r.state()["failure"]
    assert (failure["code"], failure["retry"], failure["command"]) == ("RUN_CANCELLED", "RECONCILE_FIRST", f"howlplane status {task}")
    assert r.state()["mode"] == "idle" and r.effects(kind="quit") == []
    codes = [json.loads(e["line"])["code"] for i in range(len(r.records)) for e in r.effects(i, kind="append_log")]
    assert codes[-2:] == ["TUI_RUN_CANCEL_REQUESTED", "TUI_RUN_CANCELLED"]


def test_quit_while_running_cancels_first_then_leaves(replay):
    r, _ = run_started(replay, line("/quit"), exited("run", code=-1), exited("cancel", "{}"))
    assert r.effects(7, kind="exec_signal") == [{"kind": "exec_signal", "id": "run", "signal": "interrupt"}]
    assert r.effects(8, kind="quit") == [], "nothing quits while the run is still being cancelled"
    assert r.effects(kind="quit") == [{"kind": "quit", "code": 0}] and r.code == 0


def test_the_outcome_shows_the_owner_boundary_commands(replay):
    result = {**json.loads(doc("work_result_complete.json")), "final_state": "awaiting_human", "exit_code": 2,
              "next_action": {"reason": "AWAITING_HUMAN", "command": "howlplane approve T1", "then": "howlplane resume T1",
                              "alternatives": ["howlplane reject T1"], "detail": "Read .task_runs/T1/decision_packet.md"}}
    r, _ = run_started(replay, exited("run", json.dumps(result), code=2))
    view = r.view()
    for want in ("Outcome:   awaiting_human (exit 2)", "Detail:    Read .task_runs/T1/decision_packet.md",
                 "Next:      howlplane approve T1", "Then:      howlplane resume T1", "Or:        howlplane reject T1"):
        assert want in view, view
    assert r.styles()["Outcome:   awaiting_human (exit 2)"] == "warning"
    assert r.state()["mode"] == "idle" and "failure" not in r.state()


def test_an_outcome_with_nothing_proposed_shows_no_commands(replay):
    r, _ = run_started(replay, exited("run", doc("work_result_complete.json")))
    view = r.view()
    assert "Outcome:   complete (exit 0)" in view and r.styles()["Outcome:   complete (exit 0)"] == "ok"
    assert "Verification (reported by HowlPlane): none" in view
    assert "Detail:    Review the change in the working tree and commit it." in view
    assert not any(t.startswith(("Next:", "Then:", "Or:")) for t in view), view


# -- failures ---------------------------------------------------------------------------------

def test_a_howlplane_error_document_on_stderr_explains_the_failure(replay):
    r = replay([start(), exited("inventory-1", code=2, stderr="Traceback noise\n" + json.dumps(json.loads(doc("error_not_a_repo.json"))) + "\n")])
    failure = r.state()["failure"]
    assert (failure["source"], failure["code"], failure["retry"]) == ("howlplane", "NOT_A_GIT_REPOSITORY", "UNKNOWN")
    assert "Next:      Run it inside a Git repository, or pass --repo PATH." in r.view()


def test_unreadable_replies_name_the_right_next_step(replay):
    r = replay([start(), exited("inventory-1", "not json", code=1)])
    failure = r.state()["failure"]
    assert (failure["code"], failure["command"], failure["retry"]) == ("RUN_OUTPUT_UNREADABLE", "howlplane doctor", "SAFE_TO_RETRY")
    r, task = run_started(replay, exited("run", "garbage", code=1))
    failure = r.state()["failure"]
    assert (failure["code"], failure["command"], failure["retry"]) == ("RUN_OUTPUT_UNREADABLE", f"howlplane status {task}", "RECONCILE_FIRST")
    assert failure["why"] == "The child exited with 1 and no JSON reply."


def test_refused_effects_and_failed_handlers_are_explained(replay):
    refused = {"type": "effect_failed", "kind": "exec_start", "id": "run",
               "error": {"code": "CAPABILITY_DENIED", "message": "process:exec denied"}}
    r, _ = run_started(replay, refused)
    failure = r.state()["failure"]
    assert (failure["code"], failure["what"]) == ("TUI_ACTION_FAILED", "exec_start run failed: process:exec denied")
    assert r.state()["mode"] == "idle" and r.state()["run_live"] is False
    crashed = {"type": "handler_failed", "event_type": "line",
               "error": {"schema": "howlframe.runtime_error/v1", "phase": "runtime", "code": "LIMIT_EXCEEDED",
                         "opcode": "CALL", "message": "instruction limit"}}
    r = replay([start(), crashed])
    failure = r.state()["failure"]
    assert (failure["source"], failure["code"], failure["why"]) == ("howlframe", "LIMIT_EXCEEDED", "HowlFrame runtime error at CALL")
    lost = {"type": "effect_failed", "kind": "exec_start", "id": "cancel", "error": {"code": "LIMIT_EXCEEDED", "message": "busy"}}
    r, _ = run_started(replay, line("/cancel"), exited("run", code=-1), lost)
    assert "The run was stopped but HowlPlane could not record the cancel; run `howlplane cancel` yourself." in r.notices()
    assert r.state()["mode"] == "idle"


# -- the session ------------------------------------------------------------------------------

def test_notices_stay_until_the_operator_types_again(replay):
    r = replay([start(), line("/cancel"), exited("inventory-1", READY), {"type": "timer", "id": "poll"}, line("/help")])
    assert r.notices(1) == r.notices(2) == r.notices(3) == ["Nothing is running."]
    assert r.notices()[0] == "Type an objective to preview its route. Commands:"
    assert r.notices()[-1].startswith("Keys: Up and Down recall earlier lines")
    assert all(r.styles()["! " + n] == "info" for n in r.notices())


def test_interrupt_and_end_of_input_leave_an_idle_session(replay):
    for ending in ({"type": "interrupt"}, {"type": "eof"}):
        r = replay([start(), exited("inventory-1", READY), ending])
        assert r.effects(kind="quit") == [{"kind": "quit", "code": 0}] and r.code == 0
    assert replay([start(), {"type": "terminate"}]).code == 143


def test_logging_off_writes_nothing(replay):
    r = replay([start(), line("/quit")], cfg={**CFG, "log_path": None})
    assert all(r.effects(i, kind="append_log") == [] for i in range(len(r.records)))
    r = replay([start(), line("/help"), line("/quit")])
    logged = [json.loads(e["line"]) for i in range(len(r.records)) for e in r.effects(i, kind="append_log")]
    assert [(e["seq"], e["code"]) for e in logged] == [(1, "TUI_STARTED"), (2, "TUI_STOPPED")]

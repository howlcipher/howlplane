"""Contracts for Factory v0.1: a finite supervisor over approved orchestration tasks.

Every test drives the real ``orchestration.command`` session; only the agent
backend is faked. The factory must add selection and a ledger, never a second
routing, recovery, or verification path, and never write the human-owned queue.

Task revisions: the fingerprint is the execution contract. The same revision
resumes its own resumable session (only with ``--retry``); a new revision
retires an older revision's resumable session through ``orchestration.supersede``
and starts fresh. Sessions the ledger cannot attribute to the task are never
retired and keep blocking their repository.
"""

import argparse
import io
import json
import os
import time
from pathlib import Path

import pytest

from howlplane.control_plane import cli
from howlplane.control_plane import orchestration
from howlplane.control_plane.agent_execution import AgentExecutionResult
from howlplane.control_plane.factory import task_queue as module
from tests.test_orchestration import enable_fake_codex, repository as _repository


def repository(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return _repository(tmp_path)


pytestmark = pytest.mark.contract


def worker(calls, fail_goals=()):
    """Fake agent: plans, edits on implementation, accepts; records the goal it served."""
    def execute(doc, role, agent, model, repo):
        calls.append((doc["goal"], role))
        if doc["goal"] in fail_goals:
            return AgentExecutionResult(agent_id=agent, role=role, command="fake", exit_code=1, stdout="",
                                        stderr="build error", duration_seconds=0, success=False)
        if role == "implementation":
            (repo / "CHANGED").write_text(doc["goal"] + "\n")
        stdout = "ACCEPTANCE_STATUS: ACCEPTED" if role == "acceptance" else "plan"
        return AgentExecutionResult(agent_id=agent, role=role, command="fake", exit_code=0, stdout=stdout,
                                    stderr="", duration_seconds=0, success=True)
    return execute


@pytest.fixture
def env(tmp_path, monkeypatch):
    enable_fake_codex(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(orchestration, "execute_assignment", worker(calls))
    return calls


def write_queue(tmp_path, tasks, **extra):
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"schema": module.QUEUE_SCHEMA, "tasks": tasks, **extra}))
    return path


def task(task_id, repo, **fields):
    return {"id": task_id, "goal": f"Goal {task_id}", "status": "APPROVED", "repo": str(repo),
            "orchestrator": "codex", "policy": "PLAN ONLY", **fields}


def load_session(session_id):
    return orchestration.safe_load_json(orchestration.path_for(orchestration.state_root(), session_id))


def sessions_in(repo):
    return orchestration.active_sessions(orchestration.state_root(), repo, include_terminal=True)


def handed_off(tmp_path, monkeypatch, env, repo, **fields):
    """Queue task A in ``repo`` that stops at HANDOFF REQUIRED; returns (queue path, session id)."""
    monkeypatch.setattr(orchestration, "execute_assignment", worker(env, fail_goals={"Goal A"}))
    path = write_queue(tmp_path, [task("A", repo, **fields)])
    [result] = factory_for(path).run(no_progress=True)["results"]
    assert result["status"] == "HANDOFF REQUIRED"
    return path, result["session_id"]


def edited_after_handoff(tmp_path, monkeypatch, env, capsys):
    """A handed-off task A whose goal the operator has since edited; returns (queue path, old session id)."""
    repo = repository(tmp_path)
    path, session_id = handed_off(tmp_path, monkeypatch, env, repo)
    write_queue(tmp_path, [task("A", repo, goal="Fixed Goal A")])
    capsys.readouterr()
    return path, session_id


def run_cli_json(path, capsys):
    assert cli.main(["factory", "queue", str(path), "--json", "--no-progress"]) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert [(r["task_id"], r["status"]) for r in summary["results"]] == [("A", "COMPLETE")]
    return summary, captured.err


def factory_for(path, **kwargs):
    tasks = module.load_queue(path, path.parent)
    return module.QueueFactory(tasks, module.Ledger(module.ledger_path(path)), stream=io.StringIO(), **kwargs)


def test_runs_only_approved_tasks_in_priority_order_and_records_sessions(tmp_path, env, capsys):
    repo = repository(tmp_path)
    path = write_queue(tmp_path, [
        task("LATER", repo, priority=5),
        task("DRAFT", repo, status="PROPOSED"),
        task("FIRST", repo, priority=1),
    ])
    original = path.read_bytes()

    summary = factory_for(path).run(no_progress=True)

    assert [result["task_id"] for result in summary["results"]] == ["FIRST", "LATER"]
    assert all(result["status"] == "COMPLETE" for result in summary["results"])
    assert [goal for goal, _ in env] == ["Goal FIRST", "Goal LATER"]
    assert summary["skipped"] == {"DRAFT": "status PROPOSED is not APPROVED"}
    assert summary["stop_reason"] == "no eligible task remains"
    assert path.read_bytes() == original, "the factory must never write the human-owned queue"
    for result in summary["results"]:
        session = orchestration.safe_load_json(orchestration.path_for(orchestration.state_root(), result["session_id"]))
        assert session["goal"] == f"Goal {result['task_id']}" and session["status"] == "COMPLETE"

    rerun = factory_for(path).run(no_progress=True)
    assert rerun["results"] == []
    assert rerun["skipped"]["FIRST"] == "already complete"


def test_uncommitted_result_blocks_the_next_task_in_that_repository(tmp_path, env):
    repo = repository(tmp_path)
    path = write_queue(tmp_path, [task("A", repo, policy="PLAN + EXECUTE"), task("B", repo, policy="PLAN + EXECUTE")])

    summary = factory_for(path).run(no_progress=True)

    assert [(r["task_id"], r["status"]) for r in summary["results"]] == [("A", "COMPLETE")]
    assert (repo / "CHANGED").read_text() == "Goal A\n"
    assert "uncommitted changes" in summary["skipped"]["B"]


def test_failed_dependency_holds_dependents_and_is_not_retried_silently(tmp_path, env, monkeypatch):
    first, second = repository(tmp_path / "one"), repository(tmp_path / "two")
    monkeypatch.setattr(orchestration, "execute_assignment", worker(env, fail_goals={"Goal A"}))
    path = write_queue(tmp_path, [task("A", first), task("B", second, depends_on=["A"])])

    summary = factory_for(path).run(no_progress=True)

    assert [(r["task_id"], r["status"]) for r in summary["results"]] == [("A", "HANDOFF REQUIRED")]
    assert summary["skipped"]["B"] == "waiting on A"
    rerun = factory_for(path).run(no_progress=True)
    assert rerun["results"] == []
    session_id = summary["results"][0]["session_id"]
    assert rerun["skipped"]["A"] == (
        f"previous outcome HANDOFF REQUIRED; edit the task or pass --retry A to resume session {session_id[:8]}")


def test_manual_session_completion_is_live_evidence_that_releases_dependents(tmp_path, env, monkeypatch):
    first, second = repository(tmp_path / "one"), repository(tmp_path / "two")
    monkeypatch.setattr(orchestration, "execute_assignment", worker(env, fail_goals={"Goal A"}))
    path = write_queue(tmp_path, [task("A", first), task("B", second, depends_on=["A"])])
    session_id = factory_for(path).run(no_progress=True)["results"][0]["session_id"]
    # An operator finishes the handed-off session with `howlplane orchestrate resume`.
    session_path = orchestration.path_for(orchestration.state_root(), session_id)
    session = orchestration.safe_load_json(session_path)
    session["status"] = "COMPLETE"
    orchestration.secure_write(session_path, session)

    summary = factory_for(path).run(no_progress=True)

    assert [(r["task_id"], r["status"]) for r in summary["results"]] == [("B", "COMPLETE")]


def test_retry_relaunches_a_failed_launch(tmp_path, env):
    repo = repository(tmp_path)
    path = write_queue(tmp_path, [task("A", repo)])

    def broken(args):
        raise ValueError("Selected orchestrator is not available for this session")

    failed = factory_for(path, launcher=broken).run()
    assert failed["results"][0]["status"] == module.LAUNCH_FAILED
    assert "not available" in failed["results"][0]["error"]
    assert factory_for(path).run(no_progress=True)["results"] == []

    retried = factory_for(path, retry={"A"}).run(no_progress=True)
    assert [(r["task_id"], r["status"]) for r in retried["results"]] == [("A", "COMPLETE")]


def test_crashed_factory_run_is_not_relaunched_without_evidence(tmp_path, env):
    repo = repository(tmp_path)
    path = write_queue(tmp_path, [task("A", repo)])
    tasks = module.load_queue(path, tmp_path)
    ledger = module.Ledger(module.ledger_path(path))
    ledger.append({"event": "started", "task_id": "A", "fingerprint": tasks[0].fingerprint, "run_id": "dead",
                   "repo": str(repo)})

    summary = factory_for(path).run(no_progress=True)

    assert summary["results"] == []
    assert summary["skipped"]["A"].startswith(f"previous outcome {module.INTERRUPTED}")


def test_interrupted_session_stops_the_run(tmp_path, env):
    repos = [repository(tmp_path / name) for name in ("one", "two")]
    path = write_queue(tmp_path, [task("A", repos[0]), task("B", repos[1])])

    def interrupted(args):
        raise KeyboardInterrupt

    summary = factory_for(path, launcher=interrupted).run()

    assert [(r["task_id"], r["status"]) for r in summary["results"]] == [("A", module.INTERRUPTED)]
    assert summary["stop_reason"] == "interrupted"


def test_max_tasks_and_stop_on_failure_bound_the_run(tmp_path, env, monkeypatch):
    repos = [repository(tmp_path / name) for name in ("one", "two", "three")]
    path = write_queue(tmp_path, [task(name, repo) for name, repo in zip("ABC", repos)])
    bounded = factory_for(path).run(max_tasks=1, no_progress=True)
    assert [r["task_id"] for r in bounded["results"]] == ["A"]
    assert bounded["stop_reason"] == "reached --max-tasks 1"

    monkeypatch.setattr(orchestration, "execute_assignment", worker(env, fail_goals={"Goal B"}))
    stopped = factory_for(path).run(stop_on_failure=True, no_progress=True)
    assert [(r["task_id"], r["status"]) for r in stopped["results"]] == [("B", "HANDOFF REQUIRED")]
    assert stopped["stop_reason"] == "stopped after B ended HANDOFF REQUIRED"


def test_second_concurrent_run_on_one_queue_is_refused(tmp_path, env):
    path = write_queue(tmp_path, [task("A", repository(tmp_path))])
    holder = module.Ledger(module.ledger_path(path))
    with holder.exclusive():
        with pytest.raises(ValueError, match="already working this queue"):
            factory_for(path).run(no_progress=True)


@pytest.mark.parametrize("tasks, message", [
    ([{"id": "A", "goal": "g", "status": "approved"}], "status must be one of"),
    ([{"id": "A", "goal": "g", "status": "APPROVED", "owner": "x"}], "unknown fields: owner"),
    ([{"id": "A", "goal": "resume", "status": "APPROVED"}], "collides with an orchestrate"),
    ([{"id": "A", "goal": "g", "status": "APPROVED", "policy": "YOLO"}], "invalid choice"),
    ([{"id": "A", "goal": "g", "status": "APPROVED", "agents": {"gpt": "RESERVED"}}], "unknown agent"),
    ([{"id": "A", "goal": "g", "status": "APPROVED"}] * 2, "Duplicate queue task ids: A"),
    ([{"id": "A", "goal": "g", "status": "APPROVED", "depends_on": ["Z"]}], "unknown tasks: Z"),
    ([{"id": "A", "goal": "g", "status": "APPROVED", "depends_on": ["B"]},
      {"id": "B", "goal": "g", "status": "APPROVED", "depends_on": ["A"]}], "dependency cycle: A -> B -> A"),
])
def test_invalid_queue_is_rejected_before_anything_runs(tmp_path, tasks, message):
    with pytest.raises(ValueError, match=message):
        module.load_queue(write_queue(tmp_path, tasks), tmp_path)


def test_session_options_reach_orchestration_verbatim(tmp_path):
    repo = repository(tmp_path)
    path = write_queue(tmp_path, [task("A", "repo", policy="PLAN + EXECUTE", strategy="QUALITY",
                                       failover="OFF", agents={"claude_code": "RESERVED"},
                                       constraints=["--no-network"], verify=["pytest", "-q"])])

    args = module.load_queue(path, tmp_path)[0].launch_args(quiet=True, no_progress=False, heartbeat=5.0)

    assert (args.repo, args.input, args.policy, args.strategy, args.failover) == (
        str(repo), "Goal A", "PLAN + EXECUTE", "QUALITY", "OFF")
    assert (args.claude_code, args.constraint, args.verify) == ("RESERVED", ["--no-network"], ["pytest", "-q"])
    assert args.retain_report and args.quiet and args.heartbeat == 5.0 and not args.separate


def test_cli_dry_run_reports_plan_without_launching(tmp_path, env, capsys):
    repo = repository(tmp_path)
    (repo / "dirty").write_text("x")
    path = write_queue(tmp_path, [task("A", repo), task("B", repo, depends_on=["A"])])

    assert cli.main(["factory", "queue", str(path), "--dry-run", "--json"]) == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["next"] is None
    assert "uncommitted changes" in plan["skipped"]["A"]
    assert plan["skipped"]["B"] == "waiting on A"
    assert env == []


def test_cli_run_keeps_json_stdout_clean(tmp_path, env, capsys):
    path = write_queue(tmp_path, [task("A", repository(tmp_path))])

    _, err = run_cli_json(path, capsys)

    assert "HOWL ORCHESTRATION REPORT" in err


def test_cli_returns_nonzero_when_a_session_does_not_complete(tmp_path, env, monkeypatch, capsys):
    monkeypatch.setattr(orchestration, "execute_assignment", worker(env, fail_goals={"Goal A"}))
    path = write_queue(tmp_path, [task("A", repository(tmp_path))])

    assert cli.main(["factory", "queue", str(path), "--no-progress"]) == 2
    assert "A: HANDOFF REQUIRED" in capsys.readouterr().out


def test_cli_rejects_zero_max_tasks(tmp_path):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["factory", "queue", str(tmp_path / "q.json"), "--max-tasks", "0"])


def test_edited_task_changes_fingerprint_and_is_eligible_without_retry(tmp_path, env, monkeypatch):
    """A materially edited task is a new revision: its handed-off predecessor is superseded, not a blocker."""
    repo = repository(tmp_path)
    monkeypatch.setattr(orchestration, "execute_assignment", worker(env, fail_goals={"Goal A"}))
    path = write_queue(tmp_path, [task("A", repo)])

    first = factory_for(path).run(no_progress=True)
    assert first["results"][0]["status"] == "HANDOFF REQUIRED"
    old_session, old_fingerprint = first["results"][0]["session_id"], first["results"][0]["fingerprint"]
    assert orchestration.is_resumable(load_session(old_session)), "HANDOFF REQUIRED is resumable since PR #118"

    # Same goal -> same revision -> the handoff is not silently retried or duplicated.
    unmodified = factory_for(path).run(no_progress=True)
    assert unmodified["results"] == [] and unmodified["superseded"] == []
    assert "previous outcome HANDOFF REQUIRED" in unmodified["skipped"]["A"]

    # The operator edits the goal: a new revision, eligible without --retry.
    write_queue(tmp_path, [task("A", repo, goal="Fixed Goal A")])
    stream = io.StringIO()
    second = module.QueueFactory(module.load_queue(path, tmp_path), module.Ledger(module.ledger_path(path)),
                                 stream=stream).run(no_progress=True)

    new = second["results"][0]
    assert (new["task_id"], new["status"], new["action"]) == ("A", "COMPLETE", "start")
    assert new["fingerprint"] != old_fingerprint and new["session_id"] != old_session
    [record] = second["superseded"]
    assert (record["session_id"], record["fingerprint"], record["replacement_fingerprint"]) == (
        old_session, old_fingerprint, new["fingerprint"])
    assert (record["status"], record["previous_status"], record["reason"]) == (
        "SUPERSEDED", "HANDOFF REQUIRED", "task definition changed")
    # History is retained in orchestration's own manifest, and it no longer owns the worktree.
    old = load_session(old_session)
    assert old["status"] == "SUPERSEDED" and not orchestration.is_resumable(old)
    assert old["superseded"]["previous_status"] == "HANDOFF REQUIRED"
    assert new["fingerprint"][:12] in old["superseded"]["replaced_by"]
    assert orchestration.active_sessions(orchestration.state_root(), repo) == []
    log = stream.getvalue()
    assert (f"SUPERSEDE A revision {old_fingerprint[:12]} -> {new['fingerprint'][:12]}; "
            f"retiring orchestration session {old_session}") in log
    assert log.index("SUPERSEDE") < log.index(f"START     A revision {new['fingerprint'][:12]}")
    # The ledger reconstructs the lineage and the superseded revision is never relaunched.
    events = [(r["event"], r["fingerprint"][:12]) for r in module.Ledger(module.ledger_path(path)).records]
    assert events == [("started", old_fingerprint[:12]), ("finished", old_fingerprint[:12]),
                      ("superseded", old_fingerprint[:12]), ("started", new["fingerprint"][:12]),
                      ("finished", new["fingerprint"][:12])]
    assert factory_for(path).run(no_progress=True)["skipped"]["A"] == "already complete"


def test_queue_top_level_repo_defaults_tasks(tmp_path):
    repo = repository(tmp_path / "sub")
    path = write_queue(tmp_path, [{"id": "A", "goal": "Goal A", "status": "APPROVED"}], repo="sub/repo")
    tasks = module.load_queue(path, tmp_path)
    assert tasks[0].repo == repo.resolve()


def test_cli_human_readable_report_output(tmp_path, env, capsys):
    path = write_queue(tmp_path, [task("A", repository(tmp_path))])
    assert cli.main(["factory", "queue", str(path), "--no-progress"]) == 0
    captured = capsys.readouterr()
    assert "HOWL FACTORY REPORT" in captured.out
    assert "A: COMPLETE session=" in captured.out
    assert "Stopped: no eligible task remains" in captured.out


def test_corrupt_ledger_rejected_by_cli(tmp_path, capsys):
    repo = repository(tmp_path)
    path = write_queue(tmp_path, [task("A", repo)])
    ledger_file = tmp_path / "corrupt.jsonl"
    ledger_file.write_text("not json\n")
    assert cli.main(["factory", "queue", str(path), "--ledger", str(ledger_file)]) == 1
    assert "Factory: Factory ledger" in capsys.readouterr().err


def test_fingerprint_is_the_execution_contract_not_scheduling(tmp_path):
    repo = repository(tmp_path)
    other = repository(tmp_path / "other")
    base = task("A", repo, depends_on=["B", "C"], constraints=["x"], verify=["true"])

    def fingerprint(**changes):
        entries = [{**base, **changes}, task("B", repo), task("C", repo)]
        return module.load_queue(write_queue(tmp_path, entries), tmp_path)[0].fingerprint

    original = fingerprint()
    # Approval and scheduling decide when a revision runs, not what it is.
    assert fingerprint(priority=9) == original
    assert fingerprint(status="HOLD") == original
    assert fingerprint(depends_on=["C", "B"]) == original
    # Everything the session would do differently is a new revision.
    for change in ({"goal": "Other goal"}, {"constraints": ["y"]}, {"verify": ["false"]}, {"repo": str(other)},
                   {"depends_on": ["B"]}, {"policy": "PLAN + EXECUTE"}, {"agents": {"cursor": "RESERVED"}},
                   {"execution_budget": "implementation=600"}, {"strategy": "QUALITY"}):
        assert fingerprint(**change) != original, change


def test_same_revision_handoff_stays_canonical_and_retry_resumes_it(tmp_path, env, monkeypatch):
    repo = repository(tmp_path)
    path, session_id = handed_off(tmp_path, monkeypatch, env, repo)

    # A reprioritized but otherwise unchanged task is the same revision.
    write_queue(tmp_path, [task("A", repo, priority=3)])
    unchanged = factory_for(path).run(no_progress=True)
    assert unchanged["results"] == [] and unchanged["superseded"] == []
    assert f"--retry A to resume session {session_id[:8]}" in unchanged["skipped"]["A"]
    assert [doc["id"] for doc in sessions_in(repo)] == [session_id]
    assert load_session(session_id)["status"] == "HANDOFF REQUIRED"

    # --retry continues that session through orchestrate resume; no parallel session appears.
    # Orchestration, not the factory, decides what resume may do: its hard-failed worker stays excluded.
    env.clear()
    resumed = factory_for(path, retry={"A"}).run(no_progress=True)
    [result] = resumed["results"]
    assert (result["action"], result["session_id"]) == ("resume", session_id)
    assert result["status"] == load_session(session_id)["status"] == "HANDOFF REQUIRED"
    assert resumed["superseded"] == [] and env == []
    assert [doc["id"] for doc in sessions_in(repo)] == [session_id]


def test_retry_resume_lets_orchestration_reconcile_an_external_repair(tmp_path, env, monkeypatch):
    repo = repository(tmp_path)
    check = tmp_path / "verify.sh"
    check.write_text("#!/bin/sh\ngrep -q FIXED README\n")
    check.chmod(0o755)

    def broken(doc, role, agent, model, cwd):
        if role == "implementation":
            (cwd / "README").write_text("BROKEN\n")
        return worker(env)(doc, role, agent, model, cwd)

    monkeypatch.setattr(orchestration, "execute_assignment", broken)
    path = write_queue(tmp_path, [task("A", repo, policy="PLAN + EXECUTE", verify=[str(check)])])
    [first] = factory_for(path).run(no_progress=True)["results"]
    assert first["status"] == "HANDOFF REQUIRED"

    (repo / "README").write_text("FIXED\n")  # the operator repairs the worktree by hand
    env.clear()
    monkeypatch.setattr(orchestration, "execute_assignment", worker(env))
    [result] = factory_for(path, retry={"A"}).run(no_progress=True)["results"]

    assert (result["action"], result["session_id"]) == ("resume", first["session_id"])
    assert result["status"] in module.DONE
    assert "implementation" not in [role for _, role in env], "the repair is reconciled, not redone"
    assert (repo / "README").read_text() == "FIXED\n"


def test_unrelated_handoff_session_blocks_and_is_never_discarded(tmp_path, env, monkeypatch, capsys):
    repo = repository(tmp_path)
    monkeypatch.setattr(orchestration, "execute_assignment", worker(env, fail_goals={"Manual work"}))
    args = argparse.Namespace(**vars(module.load_queue(write_queue(tmp_path, [task("X", repo)]), tmp_path)[0]
                                     .launch_args(quiet=True, no_progress=True, heartbeat=30.0)))
    args.input = "Manual work"
    orchestration.command(args)  # an operator's own session, outside the queue
    [manual] = sessions_in(repo)
    assert manual["status"] == "HANDOFF REQUIRED"
    capsys.readouterr()

    path = write_queue(tmp_path, [task("A", repo)])
    summary = factory_for(path).run(no_progress=True)

    assert summary["results"] == [] and summary["superseded"] == []
    assert (f"unfinished orchestration session {manual['id'][:8]} (HANDOFF REQUIRED) that is not this task's"
            in summary["skipped"]["A"])
    assert load_session(manual["id"]) == manual
    assert "Goal A" not in [goal for goal, _ in env]


def test_session_the_ledger_cannot_attribute_is_not_superseded(tmp_path, env, monkeypatch):
    """Another queue's task with the same id is a different task identity."""
    repo = repository(tmp_path)
    (tmp_path / "first").mkdir()
    _, session_id = handed_off(tmp_path / "first", monkeypatch, env, repo)

    other = write_queue(tmp_path, [task("A", repo, goal="Fixed Goal A")])
    summary = factory_for(other).run(no_progress=True)

    assert summary["results"] == [] and summary["superseded"] == []
    assert "that is not this task's" in summary["skipped"]["A"]
    assert load_session(session_id)["status"] == "HANDOFF REQUIRED"


def test_new_revision_waits_for_a_clean_worktree_before_retiring_anything(tmp_path, env, monkeypatch):
    repo = repository(tmp_path)
    path, session_id = handed_off(tmp_path, monkeypatch, env, repo)
    (repo / "leftover").write_text("unreviewed work\n")

    write_queue(tmp_path, [task("A", repo, goal="Fixed Goal A")])
    summary = factory_for(path).run(no_progress=True)

    assert summary["results"] == [] and summary["superseded"] == []
    assert "uncommitted changes" in summary["skipped"]["A"]
    assert load_session(session_id)["status"] == "HANDOFF REQUIRED"
    assert (repo / "leftover").read_text() == "unreviewed work\n"


def test_dependents_wait_for_the_current_revision_not_a_superseded_one(tmp_path, env, monkeypatch):
    first, second = repository(tmp_path / "one"), repository(tmp_path / "two")
    monkeypatch.setattr(orchestration, "execute_assignment", worker(env, fail_goals={"Goal A", "Goal A v2"}))
    path = write_queue(tmp_path, [task("A", first), task("B", second, depends_on=["A"])])
    v1 = factory_for(path).run(no_progress=True)["results"][0]

    write_queue(tmp_path, [task("A", first, goal="Goal A v2"), task("B", second, depends_on=["A"])])
    summary = factory_for(path).run(no_progress=True)
    assert [r["session_id"] for r in summary["superseded"]] == [v1["session_id"]]
    assert [(r["task_id"], r["status"]) for r in summary["results"]] == [("A", "HANDOFF REQUIRED")]
    assert summary["skipped"]["B"] == "waiting on A"

    # Finishing the superseded v1 session by hand is not evidence for v2.
    session = load_session(v1["session_id"])
    session["status"] = "COMPLETE"
    orchestration.secure_write(orchestration.path_for(orchestration.state_root(), v1["session_id"]), session)
    assert factory_for(path).run(no_progress=True)["skipped"]["B"] == "waiting on A"

    # Completing a revision that was complete before the edit does not release dependents either.
    monkeypatch.setattr(orchestration, "execute_assignment", worker(env))
    write_queue(tmp_path, [task("A", first, goal="Goal A v3"), task("B", second, depends_on=["A"])])
    final = factory_for(path).run(no_progress=True)
    assert [(r["task_id"], r["status"]) for r in final["results"]] == [("A", "COMPLETE"), ("B", "COMPLETE")]


def test_completed_task_edit_is_a_new_revision(tmp_path, env):
    repo = repository(tmp_path)
    path = write_queue(tmp_path, [task("A", repo)])
    assert factory_for(path).run(no_progress=True)["results"][0]["status"] == "COMPLETE"

    write_queue(tmp_path, [task("A", repo, priority=4)])
    assert factory_for(path).run(no_progress=True)["skipped"]["A"] == "already complete"

    write_queue(tmp_path, [task("A", repo, goal="Goal A again")])
    rerun = factory_for(path).run(no_progress=True)
    assert [(r["task_id"], r["status"], r["action"]) for r in rerun["results"]] == [("A", "COMPLETE", "start")]
    assert rerun["superseded"] == [], "a finished session is history already; there is nothing to retire"


def test_retry_does_not_resurrect_a_superseded_revision(tmp_path, env, monkeypatch):
    repo = repository(tmp_path)
    path, session_id = handed_off(tmp_path, monkeypatch, env, repo)
    write_queue(tmp_path, [task("A", repo, goal="Goal A v2")])
    factory_for(path).run(no_progress=True)

    # Reverting the edit names the old revision again; its session stays retired.
    write_queue(tmp_path, [task("A", repo)])
    held = factory_for(path).run(no_progress=True)
    assert held["results"] == [] and held["skipped"]["A"].startswith("previous outcome SUPERSEDED")
    monkeypatch.setattr(orchestration, "execute_assignment", worker(env))
    [retried] = factory_for(path, retry={"A"}).run(no_progress=True)["results"]
    assert retried["action"] == "start" and retried["session_id"] != session_id
    assert load_session(session_id)["status"] == "SUPERSEDED"


def test_dry_run_reports_supersede_without_mutating_anything(tmp_path, env, monkeypatch, capsys):
    path, session_id = edited_after_handoff(tmp_path, monkeypatch, env, capsys)

    def state():
        return (module.ledger_path(path).read_bytes(), load_session(session_id),
                sorted(orchestration.state_root().iterdir()), len(env))

    before = state()

    assert cli.main(["factory", "queue", str(path), "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["next"] == "A"
    assert plan["dispatch"]["action"] == "start"
    assert [item["session_id"] for item in plan["dispatch"]["supersede"]] == [session_id]

    assert cli.main(["factory", "queue", str(path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"A: would supersede previous revision {plan['dispatch']['supersede'][0]['revision']}" in out
    assert f"would retire orchestration session {session_id} (HANDOFF REQUIRED)" in out
    assert "A: would start revision" in out

    assert state() == before


def test_dry_run_reports_resume_for_a_retried_handoff(tmp_path, env, monkeypatch, capsys):
    repo = repository(tmp_path)
    path, session_id = handed_off(tmp_path, monkeypatch, env, repo)
    capsys.readouterr()

    assert cli.main(["factory", "queue", str(path), "--dry-run", "--retry", "A"]) == 0
    assert "A: would resume revision" in capsys.readouterr().out
    assert load_session(session_id)["status"] == "HANDOFF REQUIRED"


def test_cli_supersede_keeps_json_stdout_clean(tmp_path, env, monkeypatch, capsys):
    path, session_id = edited_after_handoff(tmp_path, monkeypatch, env, capsys)

    summary, err = run_cli_json(path, capsys)

    assert [r["session_id"] for r in summary["superseded"]] == [session_id]
    assert "FACTORY SUPERSEDE" in err and "FACTORY START" in err


def test_cli_human_report_shows_superseded_revision(tmp_path, env, monkeypatch, capsys):
    path, session_id = edited_after_handoff(tmp_path, monkeypatch, env, capsys)

    assert cli.main(["factory", "queue", str(path), "--no-progress"]) == 0

    out = capsys.readouterr().out
    assert "SUPERSEDED by" in out and f"session={session_id}" in out


def test_live_coordinator_on_old_revision_blocks_the_new_one(tmp_path, env, monkeypatch):
    """Ownership is never taken from a coordinator that is still running."""
    repo = repository(tmp_path)
    path, session_id = handed_off(tmp_path, monkeypatch, env, repo)
    session_path = orchestration.path_for(orchestration.state_root(), session_id)
    session = load_session(session_id)
    session["lease"].update(pid=os.getppid(), renewed_at=time.time())
    orchestration.secure_write(session_path, session)
    held = session_path.read_bytes()
    write_queue(tmp_path, [task("A", repo, goal="Fixed Goal A")])
    launched = []

    summary = factory_for(path, launcher=launched.append).run(no_progress=True)

    assert launched == [] and summary["results"] == [] and summary["superseded"] == []
    assert summary["skipped"]["A"] == summary["blocked"]["A"]
    assert summary["skipped"]["A"].startswith("could not retire previous revision")
    assert "live coordinator lease" in summary["skipped"]["A"]
    assert session_path.read_bytes() == held
    assert [e["event"] for e in module.Ledger(module.ledger_path(path)).records] == ["started", "finished"]


def test_corrupt_session_state_fails_closed(tmp_path, env, monkeypatch, capsys):
    repo = repository(tmp_path)
    path, session_id = handed_off(tmp_path, monkeypatch, env, repo)
    write_queue(tmp_path, [task("A", repo, goal="Fixed Goal A")])
    session_path = orchestration.path_for(orchestration.state_root(), session_id)
    session_path.write_text("{not json")
    ledger_before = module.ledger_path(path).read_bytes()
    env.clear()
    capsys.readouterr()

    assert cli.main(["factory", "queue", str(path), "--no-progress"]) == 1

    assert env == []
    assert session_path.read_text() == "{not json"
    assert module.ledger_path(path).read_bytes() == ledger_before


@pytest.mark.parametrize("line", [
    '{"schema": "howlplane.factory_ledger/v1"}',
    '{"schema": "howlplane.factory_ledger/v1", "event": "exploded", "task_id": "A", "fingerprint": "f", "at": "t"}',
    '[1, 2]',
])
def test_malformed_ledger_record_is_rejected(tmp_path, line):
    ledger_file = tmp_path / "ledger.jsonl"
    ledger_file.write_text(line + "\n")
    with pytest.raises(ValueError, match="Factory ledger"):
        module.Ledger(ledger_file)


def test_supersede_never_leaves_two_resumable_sessions_on_one_worktree(tmp_path, env, monkeypatch):
    repo = repository(tmp_path)
    path, _ = handed_off(tmp_path, monkeypatch, env, repo)
    observed = []

    def launcher(args):
        observed.append([doc["id"] for doc in orchestration.active_sessions(orchestration.state_root(), repo)])
        return orchestration.command(args)

    write_queue(tmp_path, [task("A", repo, goal="Fixed Goal A")])
    monkeypatch.setattr(orchestration, "execute_assignment", worker(env))
    factory_for(path, launcher=launcher).run(no_progress=True)

    assert observed == [[]], "the old revision is retired before the new session is created"

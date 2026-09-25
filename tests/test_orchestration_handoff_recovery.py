"""Regression tests for resumable orchestration handoffs, external repair, and existing WIP.

Validates that:
1. HANDOFF REQUIRED is a paused/resumable state, not a terminal dead-end.
2. An exhausted session matching incident f248f0b465a143c0b1ab8b244b2ae56e can be
   resumed with increased execution budget; timed-out workers become eligible while
   hard failures remain excluded.
3. External repair reconciles cleanly: if repository is repaired externally, resume
   reruns verification directly and completes through audit without fake worker dispatch.
4. Existing-WIP goals with verified uncommitted changes accept NO_CHANGE_REQUIRED
   without cycling providers, while ordinary change requests without WIP fail closed.
5. COMPLETE states and corrupt manifests remain non-resumable.
6. CLI guidance is truthful: RESUME commands are only displayed when resumable.
"""

import json
from pathlib import Path
import subprocess

import pytest

from howlplane.control_plane import orchestration as module
from howlplane.control_plane.agent_execution import (
    TIMEOUT_SOURCE_BUDGET,
    TIMEOUT_SOURCE_HARNESS,
    AgentExecutionResult,
)
from tests.test_orchestration import arguments, repository, result
from tests.test_orchestration_capability_recovery import accepted, events, install_all, persist


pytestmark = pytest.mark.contract


def deadline_stop(agent, role):
    outcome = result(agent, role, False, "")
    outcome.timed_out = True
    outcome.metadata = {"timeout_source": TIMEOUT_SOURCE_HARNESS}
    return outcome


def only(*agents):
    """Reserve all agents except the specified ones."""
    return {agent: "RESERVED" for agent in module.AGENTS if agent not in agents}


def test_real_handoff_scenario_exhausted_workers_resumes_with_increased_budget(tmp_path, monkeypatch, capsys):
    """Replicates incident f248f0b465a143c0b1ab8b244b2ae56e:

    - Codex implementation: NO_REPOSITORY_CHANGE (hard failure).
    - Cursor implementation: times out at 300s budget.
    - AGY implementation: times out at 300s budget.
    - Other agents reserved -> session pauses at HANDOFF REQUIRED.
    - Session is discovered by active_sessions() as resumable.
    - Inspect and report report Resumable: yes.
    - Resume with --execution-budget implementation=600 re-enables Cursor and AGY
      while Codex remains excluded.
    - Cursor succeeds under the 600s budget and the session completes.
    """
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))

    calls = []

    def execute_phase1(doc, role, agent, model, cwd):
        calls.append((role, agent, model, module.execution_budget(doc, role)))
        if role == "planning":
            return accepted(agent, role)
        if role == "implementation":
            if agent == "codex":
                # Makes no file changes -> triggers NO_REPOSITORY_CHANGE
                return result(agent, role, True)
            if agent in {"cursor", "agy"}:
                return deadline_stop(agent, role)
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute_phase1)

    args = arguments(
        repo,
        input="Implement critical feature",
        orchestrator="codex",
        policy="PLAN + EXECUTE + INDEPENDENT AUDIT",
        execution_budget=["implementation=300"],
        codex="AUTO",
        cursor="AUTO",
        agy="AUTO",
        claude_code="RESERVED",
        devin_cli="RESERVED",
    )

    exit_code = module.command(args)
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "Status: HANDOFF REQUIRED" in captured.out
    assert "Resumable: yes" in captured.out
    assert "howlplane orchestrate resume --repo" in captured.err

    # Verify active_sessions finds this session
    root = module.state_root()
    sessions = module.active_sessions(root, repo)
    assert len(sessions) == 1
    session = sessions[0]
    assert session["status"] == "HANDOFF REQUIRED"
    assert module.is_resumable(session) is True
    assert module.is_final(session) is False

    # Inspect returns Resumable: yes
    inspect_args = arguments(repo, input="inspect", json=False)
    assert module.command(inspect_args) == 0
    inspect_out = capsys.readouterr().out
    assert "Resumable: yes" in inspect_out

    # Inspect JSON includes "resumable": true
    inspect_json_args = arguments(repo, input="inspect", json=True)
    assert module.command(inspect_json_args) == 0
    inspect_json = json.loads(capsys.readouterr().out)
    assert inspect_json[0]["resumable"] is True

    # Phase 2: Resume with increased budget implementation=600
    phase2_calls = []

    def execute_phase2(doc, role, agent, model, cwd):
        phase2_calls.append((role, agent, model, module.execution_budget(doc, role)))
        if role == "implementation":
            if agent == "codex":
                pytest.fail("Codex should have remained excluded due to NO_REPOSITORY_CHANGE")
            (cwd / "README").write_text("feature implemented by cursor\n")
            return accepted(agent, role)
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute_phase2)

    resume_args = arguments(
        repo,
        input="resume",
        orchestrator=None,
        execution_budget=["implementation=600"],
    )
    exit_code2 = module.command(resume_args)
    assert exit_code2 == 0

    captured2 = capsys.readouterr()
    assert "Status: COMPLETE" in captured2.out
    # Cursor ran with 600s budget
    cursor_impl = [c for c in phase2_calls if c[0] == "implementation" and c[1] == "cursor"]
    assert len(cursor_impl) == 1
    assert cursor_impl[0][3] == 600


def test_external_repair_reconciliation_skips_fake_worker(tmp_path, monkeypatch, capsys):
    """External repair flow:

    1. Implementation worker modifies repository but verification fails.
    2. Session pauses at HANDOFF REQUIRED.
    3. User / external script repairs repository.
    4. Resume detects external modification via reconcile(), reruns verification directly.
    5. Verification passes, no implementation worker is dispatched.
    6. Independent audit and acceptance proceed and session completes.
    """
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))

    verify_script = repo / "verify.sh"
    verify_script.write_text("#!/bin/sh\ngrep -q 'FIXED' README\n")
    verify_script.chmod(0o755)

    calls = []

    def execute(doc, role, agent, model, cwd):
        calls.append((role, agent, model))
        if role == "implementation":
            (cwd / "README").write_text("BROKEN content\n")
            return accepted(agent, role)
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute)

    args = arguments(
        repo,
        input="Fix README defect",
        orchestrator="codex",
        policy="PLAN + EXECUTE + INDEPENDENT AUDIT",
        verify=[str(verify_script)],
        codex="AUTO",
        claude_code="AUTO",
        cursor="RESERVED",
        agy="RESERVED",
        devin_cli="RESERVED",
    )

    # Initial run: verification fails because README has "BROKEN content"
    exit_code = module.command(args)
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "Configured validation failed" in captured.err
    assert "Status: HANDOFF REQUIRED" in captured.out
    assert "Resumable: yes" in captured.out

    # External repair: modify README so verification script will pass
    (repo / "README").write_text("FIXED content\n")

    # Resume session
    resume_calls = []

    def execute_resume(doc, role, agent, model, cwd):
        resume_calls.append((role, agent, model))
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute_resume)

    resume_args = arguments(repo, input="resume", orchestrator=None)
    exit_code_resume = module.command(resume_args)
    assert exit_code_resume == 0

    captured_resume = capsys.readouterr()
    assert "Configured validation passed" in captured_resume.err
    assert "Status: COMPLETE" in captured_resume.out or "Status: COMPLETE WITH WARNINGS" in captured_resume.out

    # Critical check: No implementation worker was called on resume!
    impl_calls_on_resume = [c for c in resume_calls if c[0] == "implementation"]
    assert impl_calls_on_resume == []

    # Independent review and acceptance were executed
    review_calls = [c for c in resume_calls if c[0] == "review"]
    assert len(review_calls) >= 1


def test_no_change_required_accepted_with_existing_wip(tmp_path, monkeypatch, capsys):
    """When existing uncommitted WIP is intentional and verification passes,

    an implementation worker reporting NO_CHANGE_REQUIRED succeeds rather than failing
    with NO_REPOSITORY_CHANGE.
    """
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))

    # Existing WIP in repository
    (repo / "feature.py").write_text("def run(): return 42\n")

    calls = []

    def execute(doc, role, agent, model, cwd):
        calls.append((role, agent, model))
        if role == "implementation":
            # Worker inspects existing WIP and decides no further change is needed
            res = AgentExecutionResult(
                agent_id=agent,
                role=role,
                command="fake",
                exit_code=0,
                stdout="Existing uncommitted code is correct.\nIMPLEMENTATION_STATUS: NO_CHANGE_REQUIRED",
                stderr="",
                duration_seconds=5,
                success=True,
            )
            return res
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute)

    args = arguments(
        repo,
        input="Validate and finalize existing work in progress",
        orchestrator="codex",
        policy="PLAN + EXECUTE + INDEPENDENT AUDIT",
        verify=["git", "diff", "--check"],
        codex="AUTO",
        claude_code="AUTO",
        cursor="RESERVED",
        agy="RESERVED",
        devin_cli="RESERVED",
    )

    exit_code = module.command(args)
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "NO_CHANGE_REQUIRED" in captured.err or "NO_CHANGE_REQUIRED" in captured.out
    assert "Status: COMPLETE" in captured.out

    # Verify only ONE implementation worker was dispatched (did not cycle providers)
    impl_calls = [c for c in calls if c[0] == "implementation"]
    assert len(impl_calls) == 1
    assert impl_calls[0][1] == "codex"


def test_no_change_fails_closed_for_ordinary_request_without_wip(tmp_path, monkeypatch, capsys):
    """If repository is clean and request is not existing-WIP, a worker that does not

    modify the repository fails with NO_REPOSITORY_CHANGE (fail-closed).
    """
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch, models=("m1",))

    def execute(doc, role, agent, model, cwd):
        if role == "implementation":
            return AgentExecutionResult(
                agent_id=agent,
                role=role,
                command="fake",
                exit_code=0,
                stdout="I decided not to do anything.",
                stderr="",
                duration_seconds=5,
                success=True,
            )
        return accepted(agent, role)

    monkeypatch.setattr(module, "execute_assignment", execute)

    args = arguments(
        repo,
        input="Create a new microservice",
        orchestrator="codex",
        policy="PLAN + EXECUTE",
        verify=["git", "diff", "--check"],
        **only("codex"),
    )

    exit_code = module.command(args)
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "NO_REPOSITORY_CHANGE" in captured.out or "NO_REPOSITORY_CHANGE" in captured.err
    assert "Status: HANDOFF REQUIRED" in captured.out


def test_complete_state_is_truly_terminal_and_not_resumable(tmp_path, monkeypatch, capsys):
    """COMPLETE and COMPLETE WITH WARNINGS cannot be resumed."""
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)

    doc = module.setup(arguments(repo), repo)
    doc["status"] = "COMPLETE"
    persist(doc)

    assert module.is_resumable(doc) is False
    assert module.is_final(doc) is True

    # active_sessions() excludes it unless include_terminal is passed
    assert module.active_sessions(module.state_root(), repo) == []

    # resume command fails to find an unfinished session
    resume_args = arguments(repo, input="resume", orchestrator=None)
    with pytest.raises(ValueError, match="No unfinished session"):
        module.command(resume_args)


def test_corrupt_manifest_is_not_resumable_and_fails_closed(tmp_path, monkeypatch):
    """SESSION_STATE_INVALID manifests are fail-closed and cannot be resumed."""
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)

    # Missing required keys
    corrupt_doc = {"schema": module.SCHEMA, "id": "corrupt123"}
    assert module.is_resumable(corrupt_doc) is False
    with pytest.raises(module.SessionStateInvalid):
        module.normalize_session(corrupt_doc)

    # Completely non-manifest dictionary
    non_manifest = {"foo": "bar"}
    assert module.is_resumable(non_manifest) is False
    with pytest.raises(module.SessionStateInvalid):
        module.normalize_session(non_manifest)


def test_cli_guidance_truthfulness():
    """Verify that RESUME guidance is only emitted when a session is truly resumable."""
    resumable_doc = {"status": "HANDOFF REQUIRED", "id": "resumable1"}
    terminal_doc = {"status": "COMPLETE", "id": "terminal1"}

    # Mock progress
    class MockProgress(module.SessionProgress):
        def __init__(self, doc):
            self.document = doc
            self.written = []
            self.clock = lambda: 0.0
            self.phase_name = "INIT"
            self.last_change_at = 0.0

        def _write(self, tag, message):
            self.written.append((tag, message))

    prog_resumable = MockProgress(resumable_doc)
    prog_resumable.blocked("HANDOFF REQUIRED", "Need external fix", "howlplane orchestrate resume --repo /tmp/test")
    resume_tags = [w for w in prog_resumable.written if w[0] == "RESUME"]
    assert len(resume_tags) == 1
    assert "howlplane orchestrate resume" in resume_tags[0][1]

    prog_terminal = MockProgress(terminal_doc)
    prog_terminal.blocked("COMPLETE", "Done", "howlplane orchestrate resume --repo /tmp/test")
    resume_tags_term = [w for w in prog_terminal.written if w[0] == "RESUME"]
    assert len(resume_tags_term) == 0


def test_supersede_retires_a_handoff_session_but_keeps_its_history(tmp_path, monkeypatch, capsys):
    """Superseding is the evidence-preserving alternative to discard for replacement work."""
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    doc = module.setup(arguments(repo), repo)
    doc["status"] = "HANDOFF REQUIRED"
    doc["lease"]["renewed_at"] = 0
    persist(doc)
    root = module.state_root()

    retired = module.supersede(root, doc["id"], "task definition changed", "replacement revision")

    stored = module.safe_load_json(module.path_for(root, doc["id"]))
    assert stored == retired and stored["status"] == module.SUPERSEDED
    assert stored["superseded"]["previous_status"] == "HANDOFF REQUIRED"
    assert (stored["superseded"]["reason"], stored["superseded"]["replaced_by"]) == (
        "task definition changed", "replacement revision")
    assert stored["attempts"] == doc["attempts"]
    assert not module.is_resumable(stored) and module.is_final(stored)
    assert module.active_sessions(root, repo) == []
    assert [s["id"] for s in module.active_sessions(root, repo, include_terminal=True)] == [doc["id"]]
    # Neither discard nor resume touches retired history.
    assert module.command(arguments(repo, input="discard")) == 0
    assert "Discarded 0 active session(s)" in capsys.readouterr().out
    assert module.path_for(root, doc["id"]).exists()
    with pytest.raises(ValueError, match="No unfinished session"):
        module.command(arguments(repo, input="resume", orchestrator=None))
    with pytest.raises(ValueError, match="not resumable"):
        module.supersede(root, doc["id"], "again", "x")


def test_supersede_refuses_finished_missing_and_live_sessions(tmp_path, monkeypatch):
    import os
    import time
    repo = repository(tmp_path)
    install_all(tmp_path, monkeypatch)
    root = module.state_root()
    complete = module.setup(arguments(repo), repo)
    complete["status"] = "COMPLETE"
    persist(complete)
    with pytest.raises(ValueError, match="not resumable"):
        module.supersede(root, complete["id"], "r", "x")
    assert module.safe_load_json(module.path_for(root, complete["id"]))["status"] == "COMPLETE"

    with pytest.raises(ValueError, match="does not exist"):
        module.supersede(root, "0" * 32, "r", "x")

    live = module.setup(arguments(repo, input="Other"), repo)
    live["status"] = "HANDOFF REQUIRED"
    path = persist(live)
    live["lease"].update(pid=os.getppid(), renewed_at=time.time())  # a coordinator that is still running
    module.secure_write(path, live)
    with pytest.raises(ValueError, match="live coordinator lease"):
        module.supersede(root, live["id"], "r", "x")
    assert module.safe_load_json(module.path_for(root, live["id"]))["status"] == "HANDOFF REQUIRED"

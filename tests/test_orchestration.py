"""Hermetic contracts for orchestration state, routing, and recovery."""

import argparse
import io
import json
import subprocess
from pathlib import Path

import pytest

from howlplane.control_plane.agent_execution import AgentExecutionResult
from howlplane.control_plane import orchestration as module


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "README").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    return repo


def arguments(repo, **overrides):
    values = dict(repo=str(repo), input="Update README", orchestrator="codex", strategy="BALANCED",
                  models=None, fallbacks=None, failover=None, policy="PLAN ONLY", constraint=[],
                  verify=None, retain_report=True, separate=False, json=False, quiet=False,
                  no_progress=False, heartbeat=30.0)
    values.update({agent: None for agent in module.AGENTS})
    values.update(overrides)
    return argparse.Namespace(**values)


def result(agent, role, success=True, error=""):
    return AgentExecutionResult(agent_id=agent, role=role, command="fake", exit_code=0 if success else 1,
                                stdout="plan", stderr=error, duration_seconds=0, success=success)


def enable_fake_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fake/codex" if name == "codex" else None)
    monkeypatch.setattr(module, "discover_models", lambda agent: [])


def enable_fake_review_pair(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fake/" + name if name in {"codex", "claude"} else None)
    monkeypatch.setattr(module, "discover_models", lambda agent: [])


def test_reserved_agent_and_model_fallback(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fake/" + name)
    monkeypatch.setattr(module, "discover_models", lambda agent: ["first", "second"])
    doc = module.setup(arguments(repo, claude_code="RESERVED"), repo)
    assert doc["agents"]["claude_code"]["state"] == "RESERVED"
    assert module.candidates(doc, "planning")[:2] == [("codex", "first"), ("codex", "second")]
    doc["model_states"]["codex:first"] = "EXHAUSTED"
    assert module.candidates(doc, "planning")[0] == ("codex", "second")
    assert all(agent != "claude_code" for agent, _ in module.candidates(doc, "planning"))


def test_interactive_questionnaire_has_eight_questions(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    monkeypatch.setattr(module.sys, "stdin", type("TTY", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    prompts = []
    answers = iter(["Plan a change", "", "claude_code=RESERVED", "", "", "", "", "PLAN ONLY"])
    def answer(prompt):
        prompts.append(prompt)
        return next(answers)
    monkeypatch.setattr("builtins.input", answer)
    document = module.setup(arguments(repo, input=None, orchestrator=None, strategy=None, policy=None), repo)
    assert len(prompts) == 8
    assert document["agents"]["claude_code"]["state"] == "RESERVED"


def test_secret_filtered_state_and_git_reconciliation(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    doc = module.setup(arguments(repo, input="Use token=supersecret to update README", orchestrator="AUTO"), repo)
    path = tmp_path / "state" / (doc["id"] + ".json")
    path.parent.mkdir()
    module.save(path, doc, doc["lease"]["token"])
    assert "supersecret" not in path.read_text()
    (repo / "new_file").write_text("first")
    module.reconcile(doc, repo)
    assert doc["reconciliation"]["needs_validation"]
    first = doc["repository_evidence"]["untracked_sha256"]["new_file"]
    (repo / "new_file").write_text("second")
    assert module.evidence(repo)["untracked_sha256"]["new_file"] != first
    with pytest.raises(ValueError, match="stale assignment"):
        module.save(path, doc, "wrong-token")


def test_exhausted_worker_requires_handoff(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    enable_fake_codex(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "execute_assignment", lambda doc, role, agent, model, repo: result(agent, role, False, "quota exhausted"))
    args = arguments(repo, policy="PLAN + EXECUTE")
    assert module.command(args) == 2
    assert "HANDOFF REQUIRED" in capsys.readouterr().out
    args.input = "inspect"
    args.json = True
    assert module.command(args) == 0
    assert "QUOTA_EXHAUSTED" in capsys.readouterr().out


def test_complete_plan_cleans_active_state(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    enable_fake_codex(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "execute_assignment", lambda doc, role, agent, model, repo: result(agent, role))
    assert module.command(arguments(repo, retain_report=False)) == 0
    assert module.active_sessions(module.state_root(), repo) == []
    assert not list(module.state_root().glob("*.json"))


def test_independent_audit_exhaustion_is_blocked(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    enable_fake_review_pair(tmp_path, monkeypatch)

    def execute(doc, role, agent, model, cwd):
        if role == "implementation":
            (cwd / "README").write_text("changed\n")
        return result(agent, role, role != "review", "quota exhausted" if role == "review" else "")

    monkeypatch.setattr(module, "execute_assignment", execute)
    args = arguments(repo, policy="PLAN + EXECUTE + INDEPENDENT AUDIT")
    assert module.command(args) == 2
    output = capsys.readouterr().out
    assert "AUDIT BLOCKED" in output
    assert "Status: BLOCKED" in output


def test_lead_accepts_after_independent_clean_audit(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    enable_fake_review_pair(tmp_path, monkeypatch)
    roles = []

    def execute(doc, role, agent, model, cwd):
        roles.append((role, agent))
        if role == "implementation":
            (cwd / "README").write_text("changed\n")
        outcome = result(agent, role)
        outcome.stdout = {"review": "AUDIT_STATUS: CLEAN", "acceptance": "ACCEPTANCE_STATUS: ACCEPTED"}.get(role, "plan")
        return outcome

    monkeypatch.setattr(module, "execute_assignment", execute)
    assert module.command(arguments(repo, policy="PLAN + EXECUTE + INDEPENDENT AUDIT")) == 0
    assert roles == [("planning", "codex"), ("implementation", "codex"), ("review", "claude_code"), ("acceptance", "codex")]
    assert "Status: COMPLETE" in capsys.readouterr().out


def scripted_execute(**stdout_by_stage):
    """Worker stub: implementation edits README; other stages reply with the scripted text."""
    defaults = {"review": "AUDIT_STATUS: CLEAN", "acceptance": "ACCEPTANCE_STATUS: ACCEPTED"}

    def execute(doc, stage, agent, model, cwd):
        if stage == "implementation":
            (cwd / "README").write_text("changed\n")
        outcome = result(agent, stage)
        outcome.stdout = stdout_by_stage.get(stage, defaults.get(stage, "plan"))
        return outcome

    return execute


@pytest.mark.parametrize("role, verdict, failure, reason", [
    ("review", "Finding: add --due validation\nAUDIT_STATUS: FINDINGS", "AUDIT_FINDINGS_OR_UNCONFIRMED", "Finding: add --due validation"),
    ("review", "", "AUDIT_NO_VERDICT", None),
    ("acceptance", "Missing test, token=hunter2abc\nACCEPTANCE_STATUS: REJECTED", "ACCEPTANCE_REJECTED_OR_UNCONFIRMED", "Missing test"),
])
def test_non_clean_verdicts_are_classified_and_visible(tmp_path, monkeypatch, capsys, role, verdict, failure, reason):
    # DOG-001: verdict text was hashed and discarded, so a stop reason was unreadable.
    # DOG-003: an exit-0 reviewer with no text is a provider fault (AUDIT_NO_VERDICT), not findings.
    repo = repository(tmp_path)
    enable_fake_review_pair(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "execute_assignment", scripted_execute(**{role: verdict}))
    assert module.command(arguments(repo, policy="PLAN + EXECUTE + INDEPENDENT AUDIT")) == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    # Genuine findings are not a worker fault: they trigger rework, not a "failed" reroute (DOG-011).
    expected = "review reported findings" if failure == "AUDIT_FINDINGS_OR_UNCONFIRMED" else f"{role} failed: {failure}"
    assert expected in output
    if reason:
        assert f"({role}, {failure})" in captured.out
        assert reason in captured.out
    else:
        assert "AUDIT_FINDINGS_OR_UNCONFIRMED" not in output
    assert "hunter2abc" not in output


def test_rejected_acceptance_recovers_after_the_repository_is_repaired(tmp_path, monkeypatch, capsys):
    # DOG-007: the rejecting orchestrator stayed excluded from acceptance, so `resume` after the
    # user fixed what the verdict named found no worker and re-printed the same handoff.
    repo = repository(tmp_path)
    enable_fake_review_pair(tmp_path, monkeypatch)
    policy = "PLAN + EXECUTE + INDEPENDENT AUDIT"
    monkeypatch.setattr(module, "execute_assignment", scripted_execute(acceptance="Missing a test.\nACCEPTANCE_STATUS: REJECTED"))
    assert module.command(arguments(repo, policy=policy)) == 2
    first = capsys.readouterr()
    assert "Missing a test." in first.out
    assert f"howlplane orchestrate resume --repo {repo}" in first.out and "orchestrate discard" in first.out

    monkeypatch.setattr(module, "execute_assignment", lambda *args: pytest.fail("repository unchanged: no worker may be dispatched"))
    assert module.command(arguments(repo, input="resume", policy=policy, orchestrator=None)) == 2
    capsys.readouterr()

    (repo / "test_added.py").write_text("def test_it():\n    pass\n")
    monkeypatch.setattr(module, "execute_assignment", scripted_execute())
    assert module.command(arguments(repo, input="resume", policy=policy, orchestrator=None, verify=["git", "diff", "--check"])) == 0
    assert "Status: COMPLETE" in capsys.readouterr().out


def rework_session(tmp_path, monkeypatch, review_rounds):
    """Review replies come from `review_rounds` in order (last one repeats); each implementation writes new content.

    Returns the session exit status, the implementation prompts' rework state, and the review count.
    """
    repo = repository(tmp_path)
    enable_fake_review_pair(tmp_path, monkeypatch)
    seen = {"implementations": [], "reviews": 0}

    def execute(doc, stage, agent, model, cwd):
        if stage == "implementation":
            seen["implementations"].append((doc.get("rework") or {}).get("findings"))
            (cwd / "README").write_text(f"change {len(seen['implementations'])}\n")
        outcome = result(agent, stage)
        outcome.stdout = "plan"
        if stage == "review":
            outcome.stdout = review_rounds[min(seen["reviews"], len(review_rounds) - 1)]
            seen["reviews"] += 1
        if stage == "acceptance":
            outcome.stdout = "ACCEPTANCE_STATUS: ACCEPTED"
        return outcome

    monkeypatch.setattr(module, "execute_assignment", execute)
    status = module.command(arguments(repo, policy="PLAN + EXECUTE + INDEPENDENT AUDIT"))
    return status, seen


def test_review_findings_go_back_to_the_implementer_and_are_rechecked(tmp_path, monkeypatch, capsys):
    # DOG-011 (run-008): a reviewer's real findings ended in a handoff because nothing acted on them.
    status, seen = rework_session(tmp_path, monkeypatch, ["README claims JSON errors; false.\nAUDIT_STATUS: FINDINGS", "AUDIT_STATUS: CLEAN"])
    captured = capsys.readouterr()
    assert status == 0, captured.err + captured.out
    assert seen["implementations"][0] is None and "README claims JSON errors" in seen["implementations"][1]
    assert seen["reviews"] == 2
    assert "Rework rounds: 1 of 2" in captured.out and "REWORK" in captured.err


def test_rework_is_capped_and_unfixed_findings_block_with_the_verdict_visible(tmp_path, monkeypatch, capsys):
    status, seen = rework_session(tmp_path, monkeypatch, ["Still wrong.\nAUDIT_STATUS: FINDINGS"])
    captured = capsys.readouterr()
    assert status == 2
    assert len(seen["implementations"]) == 3 and seen["reviews"] == 3  # initial + 2 rework rounds
    assert "review findings remain after 2 rework round(s)" in captured.out and "Still wrong." in captured.out


def test_findings_from_an_earlier_round_are_labelled_for_acceptance():
    doc = {"rework_rounds": 1, "attempts": [
        {"stage": "review", "agent": "cursor", "state": "REVOKED", "failure": "AUDIT_FINDINGS_OR_UNCONFIRMED", "verdict_excerpt": "bug", "rework_round": 0},
        {"stage": "review", "agent": "cursor", "state": "SUCCEEDED", "failure": None, "verdict_excerpt": "AUDIT_STATUS: CLEAN", "rework_round": 1}]}
    evidence = module.audit_evidence_for_acceptance(doc)
    assert "FINDINGS FROM REWORK ROUND 0" in evidence and "NOT CLEAN" not in evidence
    assert "Cursor review, ACCEPTED AS CLEAN" in evidence


def test_verification_command_is_derived_from_the_project_when_not_given(tmp_path, monkeypatch, capsys):
    # DOG-009 (run-007): a reroute after a partial write demanded --verify before the app, and its test script, existed.
    repo = repository(tmp_path)
    enable_fake_review_pair(tmp_path, monkeypatch)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "test.sh").write_text("#!/usr/bin/env bash\nexit 0\n")

    def execute(doc, stage, agent, model, cwd):
        if stage == "implementation":
            (cwd / "README").write_text("changed\n")
            doc["reconciliation"] = {"needs_validation": True}
        outcome = result(agent, stage)
        outcome.stdout = {"review": "AUDIT_STATUS: CLEAN", "acceptance": "ACCEPTANCE_STATUS: ACCEPTED"}.get(stage, "plan")
        return outcome

    monkeypatch.setattr(module, "execute_assignment", execute)
    assert module.command(arguments(repo, policy="PLAN + EXECUTE + INDEPENDENT AUDIT")) == 0
    out = capsys.readouterr()
    assert "Verification command: bash scripts/test.sh (derived from the project's discovered test command)" in out.out
    assert "using the project's test command: bash scripts/test.sh" in out.err


def test_derived_verification_failure_stops_the_session(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    enable_fake_review_pair(tmp_path, monkeypatch)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "test.sh").write_text("#!/usr/bin/env bash\nexit 1\n")
    monkeypatch.setattr(module, "execute_assignment", scripted_execute())
    assert module.command(arguments(repo, policy="PLAN + EXECUTE + INDEPENDENT AUDIT")) == 2
    assert "Configured validation failed" in capsys.readouterr().err


def disqualified_orchestrator_session(tmp_path, monkeypatch, codex_acceptance):
    """Claude plans (so it is the orchestrator) and is then denied permission to implement (DOG-010, run-007).

    Returns the agents that were asked to accept, in order, and the session exit status.
    """
    repo = repository(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fake/" + name if name in {"claude", "codex", "agent"} else None)
    monkeypatch.setattr(module, "discover_models", lambda agent: [])
    acceptors = []

    def execute(doc, stage, agent, model, cwd):
        denied = agent == "claude_code" and stage == "implementation"
        if stage == "implementation" and not denied:
            (cwd / "README").write_text("changed\n")
        if stage == "acceptance":
            acceptors.append(agent)
        outcome = result(agent, stage, not denied, "EXECUTION_PERMISSION_REQUIRED" if denied else "")
        accepted = "ACCEPTANCE_STATUS: ACCEPTED"
        outcome.stdout = {"review": "AUDIT_STATUS: CLEAN",
                          "acceptance": codex_acceptance if agent == "codex" else accepted}.get(stage, "plan")
        return outcome

    monkeypatch.setattr(module, "execute_assignment", execute)
    status = module.command(arguments(repo, orchestrator="AUTO", policy="PLAN + EXECUTE + INDEPENDENT AUDIT",
                                      verify=["git", "diff", "--check"]))
    return acceptors, status


def test_acceptance_moves_to_another_agent_when_the_orchestrator_is_disqualified(tmp_path, monkeypatch, capsys):
    acceptors, status = disqualified_orchestrator_session(tmp_path, monkeypatch, "ACCEPTANCE_STATUS: ACCEPTED")
    captured = capsys.readouterr()
    assert acceptors == ["codex"] and "claude_code" not in acceptors
    assert "TAKEOVER" in captured.err and "could not accept" in captured.err
    assert status == 0, captured.err + captured.out


def test_a_rejection_by_the_taking_over_agent_is_final_and_not_shopped_to_a_third(tmp_path, monkeypatch, capsys):
    # Run-007: Claude was disqualified, Codex took over and REJECTED, then Cursor accepted.
    acceptors, status = disqualified_orchestrator_session(tmp_path, monkeypatch, "Not convinced.\nACCEPTANCE_STATUS: REJECTED")
    out = capsys.readouterr().out
    assert acceptors == ["codex"], acceptors
    assert status == 2 and "Status: HANDOFF REQUIRED" in out and "Not convinced." in out


def test_orchestrator_that_rejected_acceptance_is_not_replaced_by_another_agent(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    enable_fake_review_pair(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "execute_assignment", scripted_execute(acceptance="Nope.\nACCEPTANCE_STATUS: REJECTED"))
    assert module.command(arguments(repo, policy="PLAN + EXECUTE + INDEPENDENT AUDIT")) == 2
    assert "TAKEOVER" not in capsys.readouterr().err


def test_acceptance_evidence_marks_no_verdict_reviewer_as_provider_fault():
    doc = {"attempts": [
        {"stage": "review", "agent": "cursor", "state": "REVOKED", "failure": "AUDIT_NO_VERDICT", "verdict_excerpt": ""},
        {"stage": "review", "agent": "agy", "state": "SUCCEEDED", "failure": None, "verdict_excerpt": "AUDIT_STATUS: CLEAN"}]}
    evidence = module.audit_evidence_for_acceptance(doc)
    assert "Cursor review, NO VERDICT (provider returned no text" in evidence
    assert "NOT CLEAN" not in evidence
    assert "AGY review, ACCEPTED AS CLEAN" in evidence


def test_acceptance_prompt_includes_independent_audit_verdicts(tmp_path, monkeypatch):
    # DOG-002: acceptance was told to inspect the independent audit but was
    # never given it, so it rejected a clean audit as "missing evidence".
    prompts = []

    class Recorder:
        def execute(self, task, repo, role, prompt_override, **kwargs):
            prompts.append((role, prompt_override))
            return result("codex", role)

    monkeypatch.setattr(module.AgentBackendRegistry, "get_backend", lambda agent: Recorder())
    doc = {"id": "x", "goal": "g", "constraints": [], "execution_budget": {}, "workspace_trust_policy": {"policy": "prepare"},
           "attempts": [
               {"stage": "review", "agent": "cursor", "state": "REVOKED", "failure": "AUDIT_FINDINGS_OR_UNCONFIRMED", "verdict_excerpt": "bug in due()"},
               {"stage": "review", "agent": "agy", "state": "SUCCEEDED", "failure": None, "verdict_excerpt": "all fine\nAUDIT_STATUS: CLEAN"}]}
    module.execute_assignment(doc, "acceptance", "codex", "UNKNOWN", tmp_path)
    module.execute_assignment(doc, "review", "agy", "UNKNOWN", tmp_path)
    acceptance = dict(prompts)["acceptance"]
    assert "Cursor review, NOT CLEAN (AUDIT_FINDINGS_OR_UNCONFIRMED)" in acceptance and "bug in due()" in acceptance
    assert "AGY review, ACCEPTED AS CLEAN" in acceptance and "AUDIT_STATUS: CLEAN" in acceptance
    assert "Independent audit evidence" not in dict(prompts)["review"]


def test_cursor_backend_passes_selected_model_without_generation_probe(tmp_path, monkeypatch):
    from howlplane.control_plane.agent_execution import CursorBackend
    from howlplane.control_plane.task_spec import TaskSpec

    binary = tmp_path / "agent"
    binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + ":" + module.os.environ["PATH"])
    task = TaskSpec(task_id="T", repository=str(tmp_path), objective="Plan")
    outcome = CursorBackend().execute(task, tmp_path, role="planning", model_id="confirmed-model")
    assert outcome.success
    assert "--model\nconfirmed-model" in outcome.stdout
    assert "--mode\nplan" in outcome.stdout


def test_resume_revokes_old_assignment_and_validates_partial_diff(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    enable_fake_codex(tmp_path, monkeypatch)
    args = arguments(repo, policy="PLAN + EXECUTE", verify=["git", "diff", "--check"])
    doc = module.setup(args, repo)
    doc["retain_report"] = True
    doc["stage"] = "implementation"
    doc["attempts"].append({"stage": "implementation", "agent": "codex", "model": "UNKNOWN", "state": "ASSIGNED"})
    doc["lease"]["pid"] = 999999
    root = module.state_root()
    with module.locked(root):
        path = module.path_for(root, doc["id"])
        module.save(path, doc, doc["lease"]["token"])
    (repo / "README").write_text("partial\n")

    def execute(document, role, agent, model, cwd):
        if role == "implementation":
            (cwd / "README").write_text("finished\n")
        outcome = result(agent, role)
        if role == "acceptance":
            outcome.stdout = "ACCEPTANCE_STATUS: ACCEPTED"
        return outcome

    monkeypatch.setattr(module, "execute_assignment", execute)
    args.input = "resume"
    assert module.command(args) == 0
    retained = module.active_sessions(root, repo, include_terminal=True)[0]
    assert retained["attempts"][0]["failure"] == "INTERRUPTED_OR_STALE_LEASE"
    assert retained["reconciliation"]["needs_validation"] is False
    assert "COMPLETE WITH WARNINGS" in capsys.readouterr().out


def test_permission_failure_degrades_only_unattended_capability_and_auto_skips_backend(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fake/" + name)
    monkeypatch.setattr(module, "discover_models", lambda agent: [])
    doc = module.setup(arguments(repo, orchestrator="AUTO"), repo)
    failure = result("claude_code", "planning", False, "execution permission required")

    assert module.ProviderPoolManager.classify_result("claude_code", failure).value == "EXECUTION_PERMISSION_REQUIRED"
    module.record_capability_failure(doc, "claude_code", "EXECUTION_PERMISSION_REQUIRED")

    claude = doc["agents"]["claude_code"]
    assert claude["state"] == "DEGRADED"
    assert claude["capabilities"]["unattended_execution"] is False
    assert claude["capabilities"]["reason"] == "EXECUTION_PERMISSION_REQUIRED"
    assert claude["capabilities"]["scope"] == "session"
    assert claude["installed"] is True
    assert all(agent != "claude_code" for agent, _ in module.candidates(doc, "planning"))
    assert claude["capabilities"].get("review") is None


def test_explicit_override_warns_but_reserved_still_wins(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fake/" + name)
    monkeypatch.setattr(module, "discover_models", lambda agent: [])
    doc = module.setup(arguments(repo, orchestrator="claude_code"), repo)
    module.record_capability_failure(doc, "claude_code", "EXECUTION_PERMISSION_REQUIRED")
    assert module.candidates(doc, "planning")[0][0] == "claude_code"
    assert module.capability_skip_reason(doc, "claude_code", "planning") == "explicit override"

    doc["agents"]["claude_code"]["state"] = "RESERVED"
    assert all(agent != "claude_code" for agent, _ in module.candidates(doc, "planning"))


def test_new_session_resets_permission_evidence(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fake/" + name)
    monkeypatch.setattr(module, "discover_models", lambda agent: [])
    first = module.setup(arguments(repo, orchestrator="AUTO"), repo)
    module.record_capability_failure(first, "claude_code", "EXECUTION_PERMISSION_REQUIRED")
    second = module.setup(arguments(repo, orchestrator="AUTO"), repo)
    assert second["agents"]["claude_code"]["capabilities"]["unattended_execution"] is None
    assert second["agents"]["claude_code"]["state"] == "AVAILABLE"


def test_permission_failure_reroutes_once_and_auto_selects_replacement(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fake/" + name if name in {"claude", "codex"} else None)
    monkeypatch.setattr(module, "discover_models", lambda agent: ["one", "two"] if agent == "claude_code" else [])
    calls = []

    def execute(document, role, agent, model, cwd):
        calls.append((agent, model))
        return result(agent, role, agent != "claude_code", "EXECUTION_PERMISSION_REQUIRED" if agent == "claude_code" else "")

    monkeypatch.setattr(module, "execute_assignment", execute)
    assert module.command(arguments(repo, orchestrator="AUTO")) == 0

    assert calls == [("claude_code", "one"), ("codex", "UNKNOWN")]
    output = capsys.readouterr().err
    assert "Requested orchestrator: AUTO" in output
    assert "Selected orchestrator: pending" in output
    # The denial happened in the read-only planning role, which proves nothing about
    # unattended mutation, so Claude is rerouted without being marked interactive-only (DOG-004).
    assert "marked interactive-only" not in output
    assert "Claude excluded from planning for this session" in output
    assert "REROUTE" in output and "Claude → Codex" in output
    assert "SELECT" in output and "Codex selected as orchestrator" in output
    assert "TAKEOVER" not in output


def test_permission_denial_in_implementation_still_marks_claude_interactive_only(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fake/" + name if name in {"claude", "codex"} else None)
    monkeypatch.setattr(module, "discover_models", lambda agent: ["one"] if agent == "claude_code" else [])

    def execute(document, role, agent, model, cwd):
        denied = agent == "claude_code" and role == "implementation"
        if role == "implementation" and not denied:
            (cwd / "README").write_text("changed\n")
        outcome = result(agent, role, not denied, "EXECUTION_PERMISSION_REQUIRED" if denied else "")
        outcome.stdout = "ACCEPTANCE_STATUS: ACCEPTED" if role == "acceptance" else "plan"
        return outcome

    monkeypatch.setattr(module, "execute_assignment", execute)
    status = module.command(arguments(repo, orchestrator="claude_code", policy="PLAN + EXECUTE", verify=["git", "diff", "--check"]))
    err = capsys.readouterr().err
    assert "CAPABILITY  Claude marked interactive-only for this session" in err, err
    assert status == 0, err


def test_progress_reporter_projects_real_session_state_without_provider_calls():
    clock = [100.0]
    stream = io.StringIO()
    doc = {
        "id": "8f4c" * 8,
        "goal": "Build HowlPlane Factory " + "x" * 200,
        "orchestrator": "codex",
        "strategy": "BALANCED",
        "failover": "AUTO REROUTE",
        "policy": "PLAN + EXECUTE + INDEPENDENT AUDIT",
        "agents": {agent: {"state": "RESERVED" if agent == "claude_code" else "AVAILABLE"} for agent in module.AGENTS},
        "attempts": [],
        "tests": [],
        "reroutes": [],
    }
    progress = module.SessionProgress(doc, stream=stream, heartbeat_interval=30, clock=lambda: clock[0])

    progress.session_started()
    progress.phase("planning", "Building implementation plan")
    progress.assignment("HP-004", "codex", "Factory CLI and queue model")
    assert not progress.heartbeat()
    clock[0] += 30
    assert progress.heartbeat()

    output = stream.getvalue()
    assert "HOWL ORCHESTRATION" in output
    assert "Session: 8f4c" in output
    assert "Requested orchestrator: Codex" in output
    assert "Selected orchestrator: Codex" in output
    assert "Strategy: BALANCED" in output
    assert "Execution: PLAN + EXECUTE + INDEPENDENT AUDIT" in output
    assert "Reserved agents: Claude" in output
    assert "PLAN" in output and "Building implementation plan" in output
    assert "ASSIGN" in output and "HP-004" in output
    assert "WORKING" in output and "current phase: PLAN" in output
    assert len(next(line for line in output.splitlines() if line.startswith("Goal:"))) <= 96


def test_progress_events_redact_private_text_and_describe_failover():
    stream = io.StringIO()
    doc = {
        "id": "a" * 32, "goal": "Safe goal", "orchestrator": "codex", "strategy": "BALANCED",
        "failover": "AUTO REROUTE", "policy": "PLAN + EXECUTE", "attempts": [], "tests": [], "reroutes": [],
        "agents": {agent: {"state": "RESERVED" if agent == "claude_code" else "AVAILABLE"} for agent in module.AGENTS},
    }
    progress = module.SessionProgress(doc, stream=stream, heartbeat_interval=30, clock=lambda: 1)
    progress.worker_complete("HP-004", "cursor", "UI adapter changes")
    progress.capability_downgrade("claude_code")
    progress.route_skip("claude_code")
    progress.limit("codex", "model-a", "SESSION_LIMIT")
    progress.checkpoint("HP-007")
    progress.reroute("HP-007", "codex", "agy", "token=supersecret; private chain-of-thought")
    progress.validation(True, "42 targeted tests passed")
    progress.blocked("ACTION REQUIRED", "approval required", "howlplane orchestrate resume")
    progress.complete("COMPLETE")

    output = stream.getvalue()
    for label in ("COMPLETE", "CAPABILITY", "ROUTE", "LIMIT", "CHECKPOINT", "REROUTE", "VERIFY", "ACTION REQUIRED"):
        assert label in output
    assert "unattended execution unavailable" in output
    assert "TAKEOVER" not in output
    assert "Claude skipped: RESERVED" in output
    assert "supersecret" not in output
    assert "chain-of-thought" not in output


def test_quiet_and_json_modes_do_not_emit_human_progress():
    doc = {
        "id": "b" * 32, "goal": "Goal", "orchestrator": "AUTO", "strategy": "BALANCED",
        "failover": "AUTO REROUTE", "policy": "PLAN ONLY", "attempts": [], "tests": [], "reroutes": [],
        "agents": {agent: {"state": "UNAVAILABLE"} for agent in module.AGENTS},
    }
    stream = io.StringIO()
    progress = module.SessionProgress(doc, stream=stream, enabled=False)
    progress.session_started()
    progress.phase("planning", "Building plan")
    progress.heartbeat(force=True)
    assert stream.getvalue() == ""

    payload = json.dumps(doc)
    assert json.loads(payload)["id"] == "b" * 32


def test_ctrl_c_is_acknowledged_and_session_is_resumable(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    enable_fake_codex(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "execute_assignment", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert module.command(arguments(repo)) == 130
    captured = capsys.readouterr()
    assert "INTERRUPT" in captured.err
    assert "CHECKPOINT" in captured.err
    assert "howlplane orchestrate resume" in captured.err
    assert module.active_sessions(module.state_root(), repo)[0]["status"] == "INTERRUPTED"


def test_resume_reconcile_and_takeover_feedback(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    enable_fake_review_pair(tmp_path, monkeypatch)
    args = arguments(repo, orchestrator="codex")
    doc = module.setup(args, repo)
    doc["retain_report"] = True
    doc["lease"]["pid"] = 999999
    path = module.path_for(module.state_root(), doc["id"])
    with module.locked(module.state_root()):
        module.save(path, doc, doc["lease"]["token"])
    monkeypatch.setattr(module, "execute_assignment", lambda document, role, agent, model, cwd: result(agent, role))

    args.input = "resume"
    args.orchestrator = "claude_code"
    assert module.command(args) == 0
    output = capsys.readouterr().err
    assert "RESUME" in output
    assert "RECONCILE" in output
    assert "TAKEOVER" in output and "Claude" in output


def test_progress_parser_options():
    from howlplane.control_plane.cli import build_parser

    parsed = build_parser().parse_args(["orchestrate", "Goal", "--heartbeat", "25", "--no-progress", "--quiet"])
    assert parsed.heartbeat == 25
    assert parsed.no_progress is True
    assert parsed.quiet is True

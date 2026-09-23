"""Hermetic contracts for orchestration state, routing, and recovery."""

import argparse
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
                  verify=None, retain_report=True, separate=False, json=False)
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


def test_cursor_backend_passes_selected_model_without_generation_probe(tmp_path, monkeypatch):
    from howlplane.control_plane.agent_execution import CursorBackend
    from howlplane.control_plane.task_spec import TaskSpec

    binary = tmp_path / "cursor-agent"
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

"""`howlplane setup` composes existing readiness; it owns no state."""

import json
import subprocess

import pytest

from howlplane.control_plane import agent_readiness, setup_cli
from howlplane.control_plane.cli import main


def _summary(agent, name, installed=True, authenticated=True, capacity="UNKNOWN", unattended=True):
    return {"agent": agent, "name": name, "installed": installed, "authenticated": authenticated,
            "unattended_execution": unattended, "mutation_capable": True,
            "capacity": {"state": capacity, "limits": []}, "live_smoke": {"status": "NOT_RUN"}}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=a@b", "-c", "user.name=n",
                    "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    return root


def _fake(monkeypatch, summaries, trust_state="READY"):
    monkeypatch.setattr(agent_readiness, "evaluate", lambda *a, **k: summaries)
    agents = {s["agent"]: {"effective_state": trust_state, "state": trust_state} for s in summaries}
    monkeypatch.setattr(agent_readiness, "workspace_report",
                        lambda *a, **k: {"workspace": "w", "authorized": True, "agents": agents})


def _run(capsys, repo, *flags):
    code = main(["setup", "--repo", str(repo), *flags])
    return code, capsys.readouterr().out


def test_healthy_repository_points_to_factory_start(repo, monkeypatch, capsys):
    _fake(monkeypatch, [_summary("codex", "Codex"), _summary("claude", "Claude Code")])
    code, out = _run(capsys, repo)
    assert code == 0
    assert "Codex" in out and "ready" in out
    assert "Ready." in out and "howlplane factory start" in out


def test_missing_provider_and_session_limit_are_named(repo, monkeypatch, capsys):
    _fake(monkeypatch, [_summary("codex", "Codex"), _summary("claude", "Claude Code", capacity="SESSION_EXHAUSTED"),
                        _summary("cursor", "Cursor", installed=False)])
    code, out = _run(capsys, repo)
    assert "Claude Code" in out and "session limit" in out
    assert "not installed" in out
    assert code == 0  # one usable worker remains


def test_missing_workspace_trust_reports_without_preparing_when_noninteractive(repo, monkeypatch, capsys):
    _fake(monkeypatch, [_summary("codex", "Codex")], trust_state="TRUST_REQUIRED")
    called = []
    monkeypatch.setattr("howlplane.control_plane.factory.prepare.command", lambda a: called.append(a) or 0)
    code, out = _run(capsys, repo)
    assert "needs preparation" in out
    assert "howlplane factory prepare" in out
    assert called == []  # stdin is not a TTY and --yes was not given


def test_yes_delegates_to_factory_prepare_and_rechecks(repo, monkeypatch, capsys):
    _fake(monkeypatch, [_summary("codex", "Codex")], trust_state="TRUST_REQUIRED")
    calls = []

    def fake_prepare(args):
        calls.append(args)
        _fake(monkeypatch, [_summary("codex", "Codex")], trust_state="READY")
        return 0

    monkeypatch.setattr("howlplane.control_plane.factory.prepare.command", fake_prepare)
    code, out = _run(capsys, repo, "--yes")
    assert len(calls) == 1 and calls[0].yes is True
    assert code == 0 and "howlplane factory start" in out


def test_blocked_factory_exits_nonzero_with_remediation(repo, monkeypatch, capsys):
    _fake(monkeypatch, [_summary("codex", "Codex", authenticated=False)])
    code, out = _run(capsys, repo)
    assert code == 1
    assert "AUTHENTICATION_REQUIRED" in out
    assert "howlplane agents doctor" in out


def test_not_a_git_repository_is_blocked_with_fix(tmp_path, capsys):
    code, out = _run(capsys, tmp_path)
    assert code == 1
    assert "Repository" in out and "git init" in out


def test_json_contract_never_prompts(repo, monkeypatch, capsys):
    _fake(monkeypatch, [_summary("codex", "Codex")], trust_state="TRUST_REQUIRED")
    code, out = _run(capsys, repo, "--json")
    doc = json.loads(out)
    assert doc["schema"] == setup_cli.SETUP_SCHEMA
    assert doc["needs_preparation"] is True and doc["ready"] is False
    assert doc["next_action"]["command"] == "howlplane factory prepare"

"""Mutating Factory commands confirm what happened and say what to do next."""

import json
from types import SimpleNamespace

import pytest

from howlplane.control_plane.cli import main
from tests._factory_test_helpers import make_git_repo, set_xdg_paths

pytestmark = pytest.mark.integration


@pytest.fixture
def repo(tmp_path, monkeypatch):
    path = make_git_repo(tmp_path, readme_text="# Bugs\n")
    monkeypatch.chdir(path)
    set_xdg_paths(monkeypatch, tmp_path)
    return path


def _fake_start(monkeypatch, started=True):
    monkeypatch.setattr("howlplane.control_plane.factory.service.start_process",
                        lambda campaign, authority, objective, **kw: (started, SimpleNamespace(backend="process")))


def test_start_confirms_project_authority_and_next_commands(repo, monkeypatch, capsys):
    _fake_start(monkeypatch)
    assert main(["factory", "start", "--authority", "safe"]) == 0
    out = capsys.readouterr().out
    assert "STARTED" in out and "Authority" in out and "safe" in out
    for command in ("howlplane factory status", "howlplane factory logs --follow", "howlplane factory stop"):
        assert command in out
    assert str(repo) not in out and "process" not in out.lower().replace("process.", "")


def test_start_verbose_shows_backend_and_target(repo, monkeypatch, capsys):
    _fake_start(monkeypatch)
    main(["factory", "start", "--authority", "safe", "--verbose"])
    out = capsys.readouterr().out
    assert "Backend" in out and "Target" in out


def test_start_when_already_running_says_nothing_changed(repo, monkeypatch, capsys):
    _fake_start(monkeypatch, started=False)
    assert main(["factory", "start", "--authority", "safe"]) == 0
    out = capsys.readouterr().out
    assert "already running" in out and "Nothing changed" in out


def test_start_json_is_pure_json(repo, monkeypatch, capsys):
    _fake_start(monkeypatch)
    assert main(["factory", "start", "--authority", "safe", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["started"] is True
    assert "\x1b" not in captured.out and "Checking providers" not in captured.out + captured.err


def test_stop_preserves_state_and_names_resume(tmp_path, capsys):
    state = tmp_path / "state"
    assert main(["factory", "stop", "--state-dir", str(state)]) == 0
    out = capsys.readouterr().out
    assert "STOPPED" in out and "howlplane factory resume" in out


def test_resume_confirms_and_names_next_step(tmp_path, capsys):
    state = tmp_path / "state"
    main(["factory", "stop", "--state-dir", str(state)])
    capsys.readouterr()
    assert main(["factory", "resume", "--state-dir", str(state)]) == 0
    out = capsys.readouterr().out
    assert "RESUMED" in out and "howlplane factory start" in out


def test_resume_when_not_stopped_is_a_canonical_error(tmp_path, capsys):
    state = tmp_path / "state"
    assert main(["factory", "resume", "--state-dir", str(state), "--json"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["code"] == "FACTORY_NOT_STOPPED" and error["command"] == "howlplane factory status"


def test_status_json_is_additive_and_clean(tmp_path, capsys):
    state = tmp_path / "state"
    assert main(["factory", "status", "--state-dir", str(state), "--json"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    data = json.loads(out)
    for key in ("state", "supervisor_id", "provider_wake_conditions", "worker_resource_id", "operator"):
        assert key in data
    assert data["operator"]["schema"] == "howlplane.operator.status/v1"


def test_piped_status_is_plain_text(tmp_path, capsys):
    main(["factory", "status", "--state-dir", str(tmp_path / "state")])
    out = capsys.readouterr().out
    assert "\x1b" not in out and out.isascii() and "Next" in out

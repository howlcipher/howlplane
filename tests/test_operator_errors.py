"""Recognized failures explain what happened, why, and the next real command."""

import json

import pytest

from howlplane.control_plane import cli, human_boundary, locking, workspace_trust
from howlplane.control_plane.cli import build_parser, main
from howlplane.control_plane.factory.campaign import CampaignError
from howlplane.control_plane.presentation.errors import ERROR_SCHEMA, explain

CASES = [
    (cli.TargetRepositoryNotFoundError("no repo"), "NOT_A_GIT_REPOSITORY"),
    (workspace_trust.PreparationRefused("not authorized"), "WORKSPACE_TRUST_REQUIRED"),
    (locking.RepositoryLockedError("locked"), "LOCKED"),
    (human_boundary.ApprovalRequiredError("needs approval"), "OWNER_REQUIRED"),
    (human_boundary.StaleApprovalError("stale"), "APPROVAL_STALE"),
    (RuntimeError("codex: AUTHENTICATION_REQUIRED"), "AUTHENTICATION_REQUIRED"),
    (RuntimeError("claude hit SESSION_LIMIT"), "SESSION_LIMIT"),
    (CampaignError("dirty worktree"), "CAMPAIGN_ERROR"),
]


@pytest.mark.parametrize("exc,code", CASES)
def test_known_failures_map_to_stable_codes_and_keep_the_message(exc, code):
    explained = explain(exc)
    assert explained.code == code and explained.schema == ERROR_SCHEMA
    assert str(exc) in explained.render()
    assert "Why:" in explained.render() and "Next:" in explained.render()


def test_suggested_commands_exist():
    parser = build_parser()
    for exc, _ in CASES:
        command = explain(exc).command
        if command:
            parser.parse_args(command.split()[1:])


def test_unrecognized_errors_are_not_rewritten():
    assert explain(RuntimeError("something odd")) is None


def test_main_prints_explanation_and_json_form(monkeypatch, capsys):
    monkeypatch.setitem(cli.HANDLERS, "doctor", lambda a: (_ for _ in ()).throw(
        workspace_trust.PreparationRefused("not authorized")))
    assert main(["doctor"]) == 1
    err = capsys.readouterr().err
    assert "not authorized" in err and "howlplane factory prepare" in err and "Code: WORKSPACE_TRUST_REQUIRED" in err

    monkeypatch.setitem(cli.HANDLERS, "factory", lambda a: (_ for _ in ()).throw(
        human_boundary.ApprovalRequiredError("needs approval")))
    assert main(["factory", "status", "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "OWNER_REQUIRED"


def test_unrecognized_exception_is_internal_error_with_diagnostic_id(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HOWLPLANE_DIAGNOSTICS_DIR", str(tmp_path))
    monkeypatch.setitem(cli.HANDLERS, "doctor", lambda a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert main(["doctor"]) == 1
    err = capsys.readouterr().err
    assert "Something unexpected failed: boom" in err
    assert "Traceback (most recent" not in err
    assert "Code: INTERNAL_ERROR" in err
    diag = next(line.split(": ", 1)[1] for line in err.splitlines() if line.startswith("Diagnostic ID"))
    assert "RuntimeError: boom" in (tmp_path / f"{diag}.txt").read_text()


def test_debug_flag_prints_traceback(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HOWLPLANE_DIAGNOSTICS_DIR", str(tmp_path))
    monkeypatch.setitem(cli.HANDLERS, "doctor", lambda a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert main(["doctor", "--debug"]) == 1
    assert "Traceback (most recent" in capsys.readouterr().err


def test_internal_error_json_form(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HOWLPLANE_DIAGNOSTICS_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "cmd_factory_status", lambda a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert main(["factory", "status", "--json"]) == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "INTERNAL_ERROR" and payload["diagnostic_id"].startswith("HP-")


def test_diagnostic_traceback_is_redacted(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HOWLPLANE_DIAGNOSTICS_DIR", str(tmp_path))
    monkeypatch.setitem(cli.HANDLERS, "doctor",
                        lambda a: (_ for _ in ()).throw(RuntimeError("bad ghp_abcdefgh12345678")))
    main(["doctor"])
    assert "ghp_abcdefgh" not in capsys.readouterr().err
    assert all("ghp_abcdefgh" not in f.read_text() for f in tmp_path.iterdir())


def test_factory_input_errors_use_canonical_presentation(monkeypatch, capsys):
    monkeypatch.setattr(cli, "cmd_factory_status", lambda a: (_ for _ in ()).throw(ValueError("bad state dir")))
    assert main(["factory", "status", "--json"]) == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "FACTORY_INPUT_ERROR" and payload["command"] == "howlplane factory doctor"

"""`howlplane config` reports effective values with their source."""

import json

from howlplane.control_plane.cli import main


def test_show_json_lists_settings_with_source(monkeypatch, capsys):
    monkeypatch.setenv("OPERATING_MODE", "connected")
    assert main(["config", "show", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "howlplane.config/v1"
    entry = next(e for e in doc["settings"] if e["key"] == "operating_mode")
    assert entry["value"] == "connected" and entry["source"] == "environment"
    assert all({"key", "value", "source", "valid"} <= set(e) for e in doc["settings"])


def test_secrets_are_masked(monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
    main(["config", "show", "--json"])
    out = capsys.readouterr().out
    assert "super-secret-value" not in out
    main(["config", "explain", "gemini_api_key"])
    assert "super-secret-value" not in capsys.readouterr().out


def test_explain_reports_source_default_and_type(monkeypatch, capsys):
    monkeypatch.setenv("OPERATING_MODE", "connected")
    assert main(["config", "explain", "operating_mode"]) == 0
    out = capsys.readouterr().out
    assert "connected" in out and "environment" in out and "local_only" in out


def test_explain_unknown_key_fails_with_hint(capsys):
    assert main(["config", "explain", "operating_mod"]) == 1
    assert "operating_mode" in capsys.readouterr().out


def test_validate_reports_invalid_value_and_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("OPERATING_MODE", "bogus")
    assert main(["config", "validate"]) == 1
    out = capsys.readouterr().out
    assert "INVALID" in out and "operating_mode" in out


def test_validate_ok(monkeypatch, capsys):
    monkeypatch.delenv("OPERATING_MODE", raising=False)
    assert main(["config", "validate", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

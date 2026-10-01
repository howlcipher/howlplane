"""`scripts/check_test_tools.py` explains why a tier cannot run before a long run does."""

import os
import stat

import pytest

from scripts import check_test_tools as ctt

pytestmark = pytest.mark.unit


def _tool(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def isolated_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + "/usr/bin" + os.pathsep + "/bin")
    monkeypatch.delenv("HOWLFRAME_BIN", raising=False)
    return tmp_path


def test_missing_tool_is_reported_as_not_found(isolated_path):
    assert ctt.binary_problem("slopslint", "0.1.0") == "not found"


def test_installed_but_failing_tool_is_not_called_missing(isolated_path):
    _tool(isolated_path, "slopslint", "echo 'unknown option: --smol' >&2; exit 1")
    assert "installed but failed to run: unknown option: --smol" in ctt.binary_problem("slopslint", "0.1.0")


def test_wrong_version_is_reported(isolated_path):
    _tool(isolated_path, "slopslint", "echo 'slopslint 0.2.0'")
    assert "unexpected version" in ctt.binary_problem("slopslint", "0.1.0")
    _tool(isolated_path, "slopslint", "echo 'slopslint 0.1.0'")
    assert ctt.binary_problem("slopslint", "0.1.0") is None


def test_howlframe_honours_howlframe_bin(isolated_path, monkeypatch):
    assert ctt.binary_problem("howlframe") == "not found"
    _tool(isolated_path, "hf", "echo ok")
    monkeypatch.setenv("HOWLFRAME_BIN", str(isolated_path / "hf"))
    assert ctt.binary_problem("howlframe") is None


def test_exit_status_follows_the_requested_tier(isolated_path, capsys):
    _tool(isolated_path, "slopslint", "echo 'slopslint 0.1.0'")
    # slopslint present (fast tier) but go and howlframe absent (full tier)
    assert ctt.main(["--tier", "fast"]) in (0, 1)  # python deps come from the real interpreter
    capsys.readouterr()
    assert ctt.main(["--tier", "full", "--json"]) == 1
    import json
    doc = json.loads(capsys.readouterr().out)
    missing = {c["name"] for c in doc["checks"] if c["status"] != "ok" and c["tier"] == "full"}
    assert {"howlframe"} <= missing and doc["ok"] is False

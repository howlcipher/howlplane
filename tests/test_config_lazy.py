"""An invalid config must reach the CLI's error renderer, not crash the import."""

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration

ENV = {**os.environ, "OPERATING_MODE": "bogus", "PYTHONPATH": os.pathsep.join(
    ["src", "."] + [p for p in [os.environ.get("PYTHONPATH")] if p])}


def _run(*args):
    return subprocess.run([sys.executable, "-m", "howlplane.control_plane.cli", *args],
                          capture_output=True, text=True, env=ENV, timeout=60)


def test_config_validate_names_the_setting():
    result = _run("config", "validate")
    assert result.returncode == 1 and "operating_mode" in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_other_commands_print_one_canonical_error_not_a_traceback():
    result = _run("factory", "status", "--json")
    assert result.returncode == 1 and "Traceback" not in result.stderr
    error = json.loads(result.stderr)
    assert error["code"] == "INVALID_CONFIGURATION" and "operating_mode" in error["why"]
    assert error["command"] == "howlplane config validate"


def test_human_form_has_why_and_next():
    err = _run("doctor").stderr
    assert "Why:" in err and "Next:" in err and "howlplane config validate" in err


def test_config_loader_import_does_not_build_settings():
    code = "import howlplane.control_plane.config_loader as m; print('default_loader' in vars(m))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=ENV)
    assert result.returncode == 0 and result.stdout.strip() == "False"

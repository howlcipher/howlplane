"""Optional-tool gating skips locally and stops hiding anything where tools are required."""

import pytest

from tests import _requirements as req

pytestmark = pytest.mark.unit


def _skip_condition(mark):
    return mark.mark.args[0]


def test_missing_module_skips_locally_but_not_when_tools_are_required(monkeypatch):
    monkeypatch.delenv(req.REQUIRE_ENV, raising=False)
    assert _skip_condition(req.requires_module("no_such_module_xyz")) is True
    monkeypatch.setenv(req.REQUIRE_ENV, "1")
    assert _skip_condition(req.requires_module("no_such_module_xyz")) is False


def test_present_module_is_never_skipped(monkeypatch):
    monkeypatch.delenv(req.REQUIRE_ENV, raising=False)
    assert _skip_condition(req.requires_module("json")) is False


@pytest.mark.parametrize("value,expected", [("", False), ("0", False), ("false", False), ("1", True), ("yes", True)])
def test_require_flag_values(monkeypatch, value, expected):
    monkeypatch.setenv(req.REQUIRE_ENV, value)
    assert req.tools_required() is expected


def test_ci_python_jobs_require_their_tools():
    """CI installs every optional tool, so its Python jobs must turn skips into failures."""
    from pathlib import Path
    import re
    text = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    for job in ("fast-python", "full-python", "nightly-python"):
        body = re.search(rf"^  {job}:\n(.*?)(?=^  [a-z-]+:\n|\Z)", text, re.S | re.M).group(1)
        assert 'HOWLPLANE_REQUIRE_TOOLS: "1"' in body, job

"""#65: the coverage floor is declared in two places and must not drift apart."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
MINIMUM_FLOOR = 70


def _floors(relative: str):
    text = (REPO / relative).read_text(encoding="utf-8")
    return [int(m) for m in re.findall(r"--cov-fail-under=(\d+)", text)]


def test_makefile_and_ci_declare_the_same_floor():
    make, ci = _floors("Makefile"), _floors(".github/workflows/test.yml")
    assert make and ci, "a coverage gate is missing"
    assert set(make) == set(ci) and len(set(make + ci)) == 1


def test_floor_has_not_been_lowered_below_the_ratchet():
    assert min(_floors("Makefile") + _floors(".github/workflows/test.yml")) >= MINIMUM_FLOOR


def test_testing_guide_states_the_current_floor():
    text = (REPO / "documentation" / "TESTING.md").read_text(encoding="utf-8")
    assert f"--cov-fail-under={_floors('Makefile')[0]}" in text

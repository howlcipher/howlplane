#!/usr/bin/env python3
"""
test_campaign_isolation.py

Keeps test campaign state out of the real repository.

`MarathonDogfoodEngine` defaults `campaign_dir` to `.dogfood_runs` resolved
against the *current working directory*, and `run_marathon`/`run_backlog_marathon`
create a campaign directory before they check anything else -- so a test that
omits the argument writes into the checkout it is running in, and even a test
that aborts immediately still leaves a `DOGFOOD-*` directory behind.

That happened: 860 campaign directories accumulated in this repository, 542 of
them holding a `bundle_path` under `/tmp/pytest-of-...`. The pytest node id was
embedded in the recorded path, which is how the two responsible tests were
found. The historical directories are deliberately left in place -- retention
and rotation of real campaign evidence is a separate concern from stopping new
test pollution.
"""

import ast
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent

# The engine attribute is `campaign_base_dir`; the constructor argument that
# sets it is `campaign_dir`.
ENGINE_NAME = "MarathonDogfoodEngine"
REQUIRED_KWARG = "campaign_dir"


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _unisolated_constructions() -> List[Tuple[str, int]]:
    """Returns (relative path, line) for every engine construction missing campaign_dir."""
    offenders: List[Tuple[str, int]] = []
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != ENGINE_NAME:
                continue
            keywords = {kw.arg for kw in node.keywords}
            # `**kwargs` forwarding (arg is None) is opaque to a static scan;
            # treat it as isolated rather than reporting a false positive.
            if REQUIRED_KWARG in keywords or None in keywords:
                continue
            offenders.append((str(path.relative_to(REPO_ROOT)), node.lineno))
    return offenders


@pytest.mark.unit
def test_every_marathon_engine_in_tests_pins_its_campaign_dir():
    offenders = _unisolated_constructions()
    assert not offenders, (
        "These MarathonDogfoodEngine constructions omit campaign_dir and will "
        "write campaign state into the real repository's .dogfood_runs:\n"
        + "\n".join(f"  {path}:{line}" for path, line in offenders)
        + "\nPass campaign_dir=tmp_path / \"campaigns\"."
    )


@pytest.mark.unit
def test_the_scan_actually_detects_a_missing_campaign_dir(tmp_path, monkeypatch):
    """A policy test that cannot fail is not a policy test."""
    unisolated = tmp_path / "test_unisolated_example.py"
    unisolated.write_text(
        "MarathonDogfoodEngine(base_output_dir='out')\n", encoding="utf-8"
    )
    this_module = sys.modules[__name__]
    monkeypatch.setattr(this_module, "TESTS_DIR", tmp_path)
    monkeypatch.setattr(this_module, "REPO_ROOT", tmp_path)

    offenders = _unisolated_constructions()

    assert [line for _, line in offenders] == [1]

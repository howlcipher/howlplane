#!/usr/bin/env python3
"""
test_canonical_operator_ux.py

Operator-facing output names the canonical CLI.

`howl plane <verb>` is how an operator drives HowlPlane. `ai` is the deprecated
compatibility launcher -- `pyproject.toml` maps it to `legacy_main`, and `bin/ai`
says so in its first comment. Telling an operator to run `ai approve TASK-110`
after a task halts for a human decision hands them a command the canonical
install is not required to provide.

This scans what is actually shown to a person: string literals passed to
`print()`, and the messages given to `raise`. It deliberately does not touch
comments, docstrings, or the `command=` metadata recorded in lock and ledger
entries -- those are durable field values in state already on disk, and
rewriting them would make existing records disagree with new ones for no
operator-visible benefit.

The `ai` executable itself is not being removed. Only the recommendations are
modernized.
"""

import ast
from pathlib import Path
import re
import sys
from typing import Iterator, List, Tuple

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
REPO_ROOT = SRC_DIR.parent

LEGACY_RECOMMENDATION = re.compile(
    r"\bai (approve|reject|resume|cancel|unlock|work|status|doctor|verify)\b"
)


def _literal_text(node: ast.AST) -> str:
    """Flattens a string constant or f-string into its literal fragments."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return ""


def _operator_facing_strings(tree: ast.AST) -> Iterator[Tuple[int, str]]:
    """Yields (line, text) for every string printed to or raised at an operator."""
    for node in ast.walk(tree):
        shown: List[ast.AST] = []
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            shown = list(node.args)
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            shown = list(node.exc.args)
        for arg in shown:
            text = _literal_text(arg)
            if text:
                yield node.lineno, text


def _legacy_recommendations() -> List[str]:
    offenders: List[str] = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, text in _operator_facing_strings(tree):
            match = LEGACY_RECOMMENDATION.search(text)
            if match:
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{line}: {match.group(0)!r} in {text.strip()!r}")
    return offenders


@pytest.mark.unit
def test_no_operator_facing_output_recommends_the_deprecated_ai_cli():
    offenders = _legacy_recommendations()
    assert not offenders, (
        "These strings are shown to an operator and recommend the deprecated "
        "`ai` launcher instead of `howl plane`:\n" + "\n".join(f"  {o}" for o in offenders)
    )


@pytest.mark.unit
def test_the_scan_reads_both_print_and_raise_and_would_catch_a_regression(tmp_path, monkeypatch):
    """A policy test that cannot fail is not a policy test."""
    module = tmp_path / "regression_example.py"
    module.write_text(
        'def f(task_id):\n'
        '    print(f"Run: ai approve {task_id}")\n'
        '    raise RuntimeError(f"then ai resume {task_id}")\n'
        '    print("howl plane resume is fine")\n',
        encoding="utf-8",
    )
    this_module = sys.modules[__name__]
    monkeypatch.setattr(this_module, "SRC_DIR", tmp_path)
    monkeypatch.setattr(this_module, "REPO_ROOT", tmp_path)

    offenders = _legacy_recommendations()

    assert len(offenders) == 2
    assert any("'ai approve'" in o for o in offenders)
    assert any("'ai resume'" in o for o in offenders)


@pytest.mark.unit
def test_no_operator_facing_error_names_the_deprecated_launcher():
    """A bare mention is as misleading as a recommendation.

    The launcher's program name is chosen at runtime -- `howlplane` normally,
    `ai` only through the deprecated entry point -- so an error hard-coding
    "'ai' must be run inside a Git repository" names the wrong CLI for almost
    every caller. `legacy_main`'s own deprecation notice is the one place `ai`
    is the correct subject, so it is excluded by name.
    """
    from src.control_plane import launcher

    offenders = []
    for path in sorted(SRC_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, text in _operator_facing_strings(tree):
            if "'ai'" in text and "is deprecated" not in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}: {text.strip()!r}")

    assert not offenders, (
        "These operator-facing strings name the deprecated `ai` launcher:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )
    # The deprecation notice itself must survive.
    assert callable(launcher.legacy_main)


@pytest.mark.unit
def test_the_deprecated_ai_entry_point_still_exists():
    """Modernizing the recommendations must not remove backwards compatibility."""
    from src.control_plane import launcher

    assert callable(launcher.legacy_main)
    assert (REPO_ROOT / "bin" / "ai").is_file()

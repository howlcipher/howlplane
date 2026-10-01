"""The declared Python support contract matches what the code and CI actually use."""

import ast
from pathlib import Path
import re
import tomllib

import pytest

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
WORKFLOWS = REPO / ".github" / "workflows"


def _floor() -> tuple:
    match = re.fullmatch(r">=(\d+)\.(\d+)", PYPROJECT["project"]["requires-python"])
    assert match, "requires-python must be a single '>=MAJOR.MINOR' floor"
    return int(match.group(1)), int(match.group(2))


def _matrix_versions(workflow: str) -> set:
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    versions = set()
    for line in re.findall(r"python-version:\s*\[([^\]]+)\]", text):
        versions.update(tuple(int(p) for p in v.strip().strip("'\"").split(".")) for v in line.split(","))
    return versions


@pytest.mark.contract
def test_floor_is_supported_by_stdlib_tomllib_imports():
    """tomllib is stdlib only from 3.11; a lower floor would be false."""
    importers = [
        path for path in (REPO / "src").rglob("*.py")
        if any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            and (
                [a.name for a in node.names if a.name == "tomllib"]
                if isinstance(node, ast.Import) else node.module == "tomllib"
            )
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]
    assert importers, "expected tomllib importers; update this guard if they were removed"
    assert _floor() >= (3, 11), f"tomllib is imported by {[p.name for p in importers]}"


@pytest.mark.contract
@pytest.mark.parametrize("workflow", ["test.yml"])
def test_ci_matrix_starts_at_the_declared_floor_and_has_no_older_interpreter(workflow):
    versions = _matrix_versions(workflow)
    assert versions, "no python-version matrix found"
    assert min(versions) == _floor()
    assert (3, 14) in versions


@pytest.mark.contract
def test_classifiers_match_the_ci_matrix():
    classified = {
        tuple(int(p) for p in c.rsplit("::", 1)[1].strip().split("."))
        for c in PYPROJECT["project"]["classifiers"]
        if re.search(r":: 3\.\d+$", c)
    }
    assert classified == _matrix_versions("test.yml")


@pytest.mark.contract
def test_no_workflow_pins_an_interpreter_below_the_floor():
    for path in WORKFLOWS.glob("*.yml"):
        for raw in re.findall(r"python-version:\s*['\"]?(\d+\.\d+)", path.read_text(encoding="utf-8")):
            assert tuple(int(p) for p in raw.split(".")) >= _floor(), path.name

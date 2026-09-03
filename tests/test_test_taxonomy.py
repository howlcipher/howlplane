"""Guards on the test tier taxonomy declared in tests/conftest.py.

The taxonomy is a hardcoded allowlist keyed by filename and test name. Nothing
else in the suite notices when an entry stops resolving: a renamed or deleted
module silently drops out of its tier, and a renamed test silently loses its
``slow`` marking and re-enters the fast gate (or, worse, a genuinely expensive
test keeps running there under a stale name). These tests make that rot loud.
"""

import ast
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"


def _load_conftest():
    spec = importlib.util.spec_from_file_location(
        "_taxonomy_conftest", TESTS_DIR / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFTEST = _load_conftest()
ON_DISK = {path.name for path in TESTS_DIR.rglob("test_*.py")}

TIER_SETS = {
    "_ACCEPTANCE_MODULES": CONFTEST._ACCEPTANCE_MODULES,
    "_INTEGRATION_MODULES": CONFTEST._INTEGRATION_MODULES,
    "_SLOW_MODULES": CONFTEST._SLOW_MODULES,
}


@pytest.mark.parametrize("set_name", sorted(TIER_SETS))
def test_every_classified_module_exists_on_disk(set_name):
    """A renamed or deleted module must not linger as a dead allowlist entry."""
    missing = sorted(TIER_SETS[set_name] - ON_DISK)
    assert not missing, (
        f"{set_name} names modules that no longer exist: {missing}. "
        "Update the taxonomy in tests/conftest.py when a test module moves."
    )


def test_primary_tiers_are_disjoint():
    """Every test gets exactly one primary tier, so the sets cannot overlap."""
    overlap = sorted(
        CONFTEST._ACCEPTANCE_MODULES & CONFTEST._INTEGRATION_MODULES
    )
    assert not overlap, (
        f"Modules claimed by two primary tiers: {overlap}. Acceptance wins in "
        "pytest_collection_modifyitems, so the integration entry is dead."
    )


def _module_level_test_names(module_name):
    """Test function names defined in a module, including inside classes."""
    path = next(TESTS_DIR.rglob(module_name), None)
    if path is None:
        return None
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def test_every_slow_test_id_resolves_to_a_real_test():
    """A ``slow`` entry that no longer names a real test silently stops working.

    This is the failure that matters: the expensive test quietly rejoins
    `make test-fast` under its new name, and nobody notices until the fast gate
    is slow again.
    """
    unresolved = []
    for entry in sorted(CONFTEST._SLOW_TESTS):
        module_name, _, test_name = entry.partition("::")
        assert test_name, f"{entry!r} must be of the form 'test_module.py::test_name'"
        names = _module_level_test_names(module_name)
        if names is None:
            unresolved.append(f"{entry} (module missing)")
        elif test_name not in names:
            unresolved.append(f"{entry} (test missing)")
    assert not unresolved, (
        f"_SLOW_TESTS entries no longer resolve: {unresolved}. "
        "Re-profile with `pytest --durations=0` and update tests/conftest.py."
    )


def test_slow_test_ids_do_not_duplicate_slow_modules():
    """Per-test entries in an already-slow module are redundant and misleading."""
    redundant = sorted(
        entry
        for entry in CONFTEST._SLOW_TESTS
        if entry.split("::")[0] in CONFTEST._SLOW_MODULES
    )
    assert not redundant, (
        f"These tests are already covered by _SLOW_MODULES: {redundant}. "
        "Keep one source of truth per test."
    )

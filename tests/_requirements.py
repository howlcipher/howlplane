"""Skip tests whose optional Python dependency is genuinely absent.

A missing optional extra is an environment fact, not a product defect, so
locally it skips with an explicit reason. CI sets ``HOWLPLANE_REQUIRE_TOOLS=1``
and installs everything, so there a missing dependency stops being a skip and the
test runs (and fails), which means a broken CI image can never hide behind skips.

Usage::

    @requires_module("chromadb")

External toolchain binaries that the project verifies fail-closed (slopslint,
howlframe) are deliberately NOT skipped here; see ``scripts/check_test_tools.py``.
"""

import importlib
import importlib.util
import os

import pytest

REQUIRE_ENV = "HOWLPLANE_REQUIRE_TOOLS"


def tools_required() -> bool:
    return os.environ.get(REQUIRE_ENV, "") not in ("", "0", "false", "no")


def _skip_unless(available: bool, reason: str):
    return pytest.mark.skipif(not available and not tools_required(), reason=reason)


def module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def requires_module(*names: str):
    """Skip unless every named optional Python module can be imported."""
    missing = [n for n in names if not module_available(n)]
    return _skip_unless(not missing, f"optional dependency not installed: {', '.join(missing)} "
                                     f"(install the extras in documentation/TESTING.md)")


def import_or_skip(name: str):
    """Module-level import that skips the module locally but fails where tools are required."""
    if tools_required():
        return importlib.import_module(name)
    return pytest.importorskip(name, reason=f"optional dependency not installed: {name}")

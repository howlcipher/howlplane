"""Skip tests whose optional dependency or external tool is genuinely absent.

A missing optional dependency is an environment fact, not a product defect, so
locally it skips with an explicit reason. CI sets ``HOWLPLANE_REQUIRE_TOOLS=1``
and installs everything, so there a missing dependency stops being a skip and the
test runs (and fails), which means a broken CI image can never hide behind skips.

Usage::

    @requires_module("chromadb")
    @requires_binary("slopslint", version_substring="0.1.0")
"""

import importlib
import importlib.util
import os
import shutil
import subprocess
from typing import Optional

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


def binary_status(name: str, version_args=("--version",), version_substring: Optional[str] = None) -> Optional[str]:
    """None when the binary runs (and reports the version), else why it cannot be used."""
    path = shutil.which(name)
    if path is None:
        return f"external tool not on PATH: {name}"
    try:
        result = subprocess.run([path, *version_args], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"external tool {name} could not run: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[:1]
        return f"external tool {name} is installed but failed to run: {detail[0] if detail else result.returncode}"
    if version_substring and version_substring not in (result.stdout + result.stderr):
        return f"external tool {name} is not the expected version {version_substring}"
    return None


def requires_binary(name: str, version_args=("--version",), version_substring: Optional[str] = None):
    """Skip unless the external tool is on PATH, runs, and (optionally) has the expected version."""
    problem = binary_status(name, version_args, version_substring)
    return _skip_unless(problem is None, problem or "")

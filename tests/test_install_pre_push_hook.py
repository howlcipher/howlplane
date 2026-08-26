# -*- coding: utf-8 -*-
"""Tests for the install_pre_push_hook script.

This test ensures that the script creates a literal "pre-push" hook file with the expected content
and that the script source no longer contains the obfuscated chr() chain.
"""

import os
import sys
import shutil
import subprocess
import pathlib

import pytest

from src.control_plane.git_env import GIT_REPOSITORY_SELECTION_ENV_VARS

# Repository root is two directories up from this test file
repo_root = pathlib.Path(__file__).resolve().parents[1]

SCRIPT_SRC = repo_root / "scripts" / "install_pre_push_hook.py"


def _copy_script_to_tmp(tmp_path: pathlib.Path) -> pathlib.Path:
    """Copy the real install_pre_push_hook.py into a temporary repo structure.

    The script determines its location relative to its own __file__, so we recreate the
    minimal directory layout expected by the script: <tmp>/scripts/install_pre_push_hook.py
    and <tmp>/.git/hooks/ where the hook will be written.
    """
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    dest = scripts_dir / "install_pre_push_hook.py"
    shutil.copyfile(SCRIPT_SRC, dest)
    return dest


def test_install_pre_push_hook_creates_literal_filename(tmp_path: pathlib.Path):
    """Run the install_pre_push_hook script in an isolated temp repo.

    The script should create a file named ``pre-push`` in ``.git/hooks`` that is
    executable and contains the expected hook body.
    """
    # Create a fake .git/hooks directory
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Copy the script into the temporary layout
    script_path = _copy_script_to_tmp(tmp_path)

    # Execute the script using the current Python interpreter
    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )

    # The script should exit cleanly
    assert result.returncode == 0, f"Script failed: {result.stderr}"

    # Verify the hook file exists with the literal name "pre-push"
    hook_file = hooks_dir / "pre-push"
    assert hook_file.is_file(), "Hook file was not created"

    # Verify the hook is executable
    assert os.access(hook_file, os.X_OK), "Hook file is not executable"

    # Verify the hook content includes the expected make command line
    hook_content = hook_file.read_text(encoding="utf-8")
    assert "make test lint build docs" in hook_content

    # Regression guard: ensure the source now contains the literal string "pre-push"
    source_text = SCRIPT_SRC.read_text(encoding="utf-8")
    assert '"pre-push"' in source_text, "Literal hook name missing in source"
    # Ensure the obfuscation pattern is gone
    assert "chr(" not in source_text, "Obfuscated chr() chain still present"


def test_generated_hook_drops_inherited_git_repository_selection(tmp_path: pathlib.Path):
    """The generated hook must scrub the variables Git exports to hooks.

    Regression guard: the installer previously emitted a hook with no `unset`,
    so re-running it silently reverted a hand-patched local hook. The suite is
    independently safe (git_env sanitizes every call), but the hook must not
    reintroduce a contaminated environment for anything else it invokes.
    """
    (tmp_path / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
    script_path = _copy_script_to_tmp(tmp_path)

    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Script failed: {result.stderr}"

    hook_content = (tmp_path / ".git" / "hooks" / "pre-push").read_text(encoding="utf-8")
    unset_lines = [ln for ln in hook_content.splitlines() if ln.startswith("unset ")]
    assert unset_lines, "Generated hook does not unset any Git variables"
    scrubbed = " ".join(unset_lines)
    for name in GIT_REPOSITORY_SELECTION_ENV_VARS:
        assert name in scrubbed, f"Generated hook does not unset {name}"
    # The unset must precede the suite it protects.
    assert hook_content.index("unset ") < hook_content.index("make test")

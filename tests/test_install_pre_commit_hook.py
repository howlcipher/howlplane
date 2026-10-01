# -*- coding: utf-8 -*-
"""Tests for the install_pre_commit_hook script.

Ensures the installed hook rejects .env commits and regenerates the skills
manifest/index/`.claude/skills/` symlinks whenever a skill or command-skill
file is part of the commit.
"""

import os
import shutil
import subprocess
import sys
import pathlib

repo_root = pathlib.Path(__file__).resolve().parents[1]

SCRIPT_SRC = repo_root / "scripts" / "install_pre_commit_hook.py"


def _copy_script_to_tmp(tmp_path: pathlib.Path) -> pathlib.Path:
    dest = tmp_path / "scripts" / "install_pre_commit_hook.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SCRIPT_SRC, dest)
    return dest


def test_install_pre_commit_hook_creates_literal_filename(tmp_path: pathlib.Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    script_path = _copy_script_to_tmp(tmp_path)

    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Script failed: {result.stderr}"

    hook_file = hooks_dir / "pre-commit"
    assert hook_file.is_file(), "Hook file was not created"
    assert os.access(hook_file, os.X_OK), "Hook file is not executable"

    hook_content = hook_file.read_text(encoding="utf-8")
    assert ".env" in hook_content
    assert "generate_skills_manifest.py" in hook_content
    # Regenerates on either domain skill or command-skill changes.
    assert r"^\.agents/(skills|skill_commands)/" in hook_content
    # Stages all three regenerated artifacts, not just the manifest/index.
    assert "git add AGENTS.md .agents/skills.json .claude/skills" in hook_content

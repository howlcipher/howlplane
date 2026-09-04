#!/usr/bin/env python3
"""
test_marathon_cli.py

The `marathon` subcommand as an operator actually reaches it.

tests/test_backlog_marathon.py drives `run_backlog_marathon` directly, so it
stayed green while the command was uninvokable: nothing exercised the path from
argv through the canonical launcher into `cmd_marathon`. These tests close that
gap for the exact invocation the unattended dogfood run uses.
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.control_plane import launcher
from src.control_plane.git_env import run_git_in_repo

MARATHON_ARGV = [
    "marathon",
    "--authority-profile", "howlframe-overnight",
    "--repo-slug", "howlcipher/howlframe",
    "--max-tasks", "5",
    "--max-runtime-hours", "4",
]

RANKED_BACKLOG = """# Improvements

## Ranked Backlog (best ROI first)

| # | Improvement | Status | Score (V×D÷E) | Claude model | ROI rationale |
| --- | --- | --- | --- | --- | --- |
| 1 | [Make the widget faster](#1-make-the-widget-faster) | Pending | 8.0 (8×1÷1) | Sonnet 5 | worth doing |
| 2 | [Tidy the docs](#2-tidy-the-docs) | Pending ⚠️ below floor | 0.2 (1×1÷5) | Haiku | needs confirmation |

## Details

### 1. Make the widget faster

The widget is slow under load.

### 2. Tidy the docs

Cosmetic.
"""


@pytest.fixture
def backlog_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    run_git_in_repo(repo, ["init", "-q", "."])
    run_git_in_repo(repo, ["config", "user.email", "fixture@example.test"])
    run_git_in_repo(repo, ["config", "user.name", "fixture"])
    (repo / "improvements.md").write_text(RANKED_BACKLOG, encoding="utf-8")
    run_git_in_repo(repo, ["add", "-A"])
    run_git_in_repo(repo, ["commit", "-q", "-m", "backlog fixture"])
    return repo


@pytest.mark.integration
def test_marathon_help_renders_marathon_options_not_the_command_list(capsys):
    with pytest.raises(SystemExit) as exit_info:
        launcher.main(["marathon", "--help"])
    assert exit_info.value.code == 0

    out = capsys.readouterr().out
    assert "--authority-profile" in out
    assert "--roi-floor" in out
    assert "--max-runtime-hours" in out
    # The failure mode this guards against is falling through to top-level help.
    assert "Command to execute" not in out


@pytest.mark.integration
def test_marathon_reaches_cmd_marathon_with_every_flag(monkeypatch, backlog_repo):
    """The dispatch gap dropped the whole invocation, not just its flags."""
    seen = {}

    def spy(args):
        seen["namespace"] = args
        return 0

    monkeypatch.setitem(launcher.ACTIONS, "marathon", spy)

    code = launcher.main(MARATHON_ARGV + ["--target-repo", str(backlog_repo), "--dry-run"])

    assert code == 0
    namespace = seen["namespace"]
    assert namespace.subcommand == "marathon"
    assert namespace.authority_profile == "howlframe-overnight"
    assert namespace.target_repo == str(backlog_repo)
    assert namespace.repo_slug == "howlcipher/howlframe"
    assert namespace.max_tasks == 5
    assert namespace.max_runtime_hours == 4.0
    assert namespace.dry_run is True


@pytest.mark.integration
def test_marathon_dry_run_selects_from_a_clean_fixture_backlog(capsys, backlog_repo):
    """End to end through the real cmd_marathon: no provider, no writes."""
    code = launcher.main(MARATHON_ARGV + ["--target-repo", str(backlog_repo), "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert "MARATHON DRY RUN" in out
    assert "HOWLFRAM-IMP-1" in out
    # Below the ROI floor, so reported as ineligible rather than silently dropped.
    assert "STATUS_NOT_ELIGIBLE" in out
    assert "No provider was invoked and nothing was written." in out


@pytest.mark.integration
def test_marathon_is_invokable_through_the_real_executable(backlog_repo):
    """Guards the launcher entry point itself, not just an in-process call."""
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "src.control_plane.launcher", "marathon", "--help"],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "--authority-profile" in result.stdout


@pytest.mark.unit
def test_authority_show_accepts_every_canonical_profile(capsys):
    """`howlframe-overnight` is a valid marathon profile; it must be inspectable."""
    from src.control_plane.authority_profile import CANONICAL_PROFILES

    for profile_id in sorted(CANONICAL_PROFILES):
        assert launcher.main(["authority", "show", profile_id]) == 0
        assert profile_id in capsys.readouterr().out


@pytest.mark.unit
def test_authority_show_choices_track_the_registry():
    """A second hard-coded list is what made howlframe-overnight uninspectable."""
    from src.control_plane.authority_profile import CANONICAL_PROFILES

    def subparser_choices(parser):
        return next(
            action.choices
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )

    authority = subparser_choices(launcher.build_parser())["authority"]
    show = subparser_choices(authority)["show"]
    profile_arg = next(a for a in show._actions if a.dest == "profile_id")

    assert set(profile_arg.choices) == set(CANONICAL_PROFILES)
    assert "howlframe-overnight" in profile_arg.choices


@pytest.mark.unit
def test_authority_inspection_never_writes_or_invokes_a_provider(monkeypatch, tmp_path):
    """Inspection is read-only; it is what an operator does before granting."""
    from src.control_plane.synthesis.provider_pool import ProviderPoolManager

    def explode(*args, **kwargs):
        raise AssertionError("authority show must not construct a provider pool")

    monkeypatch.setattr(ProviderPoolManager, "from_config", staticmethod(explode))
    monkeypatch.chdir(tmp_path)

    assert launcher.main(["authority", "show", "howlframe-overnight"]) == 0
    assert list(tmp_path.iterdir()) == []

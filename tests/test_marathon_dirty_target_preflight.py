#!/usr/bin/env python3
"""
test_marathon_dirty_target_preflight.py

A new marathon refuses to start on a working tree it does not own.

A marathon implements directly in the target checkout -- the orchestrator edits
files in place, and `capture_baseline` then separates what the task changed from
what was already there. That is right for attribution and wrong to *start* on
top of: an unattended run beginning on unresolved work would interleave its own
changes with a person's, commit the result, and open a pull request nobody can
cleanly separate.

The live example is HowlFrame, whose four modified files are owned by
HOWLFRAM-BUG-52 while that task sits AWAITING_HUMAN.

The refusal is fail-closed and inert: nothing is cleaned, stashed, reset, or
adopted, because deciding who owns unexplained work is a human's call.
"""

from pathlib import Path
from typing import List, Optional

import pytest

from src.control_plane.git_baseline import describe_working_tree
from src.control_plane.git_env import run_git_in_repo
from src.control_plane.synthesis.marathon import (
    DIRTY_TARGET_STOP_REASON,
    MarathonDogfoodEngine,
)
from src.control_plane.synthesis.provider_pool import ProviderPoolManager

RANKED_BACKLOG = """# Improvements

## Ranked Backlog (best ROI first)

| # | Improvement | Status | Score (V×D÷E) | Claude model | ROI rationale |
| --- | --- | --- | --- | --- | --- |
| 1 | [Make the widget faster](#1-make-the-widget-faster) | Pending | 8.0 (8×1÷1) | Sonnet 5 | worth doing |

## Details

### 1. Make the widget faster

The widget is slow under load.
"""


def _git(repo: Path, *args: str):
    result = run_git_in_repo(repo, list(args))
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    """A clean git repository carrying a valid ranked backlog."""
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "fixture@example.test")
    _git(repo, "config", "user.name", "fixture")
    (repo / "improvements.md").write_text(RANKED_BACKLOG, encoding="utf-8")
    (repo / "widget.py").write_text("def widget():\n    return 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\nbuild/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


class ProviderSpy:
    """Counts every attempt to reach a provider or touch git."""

    def __init__(self):
        self.orchestrator_calls: List[str] = []
        self.git_executor_calls: List[str] = []

    def orchestrator_factory(self, config):
        self.orchestrator_calls.append(getattr(config, "task_id", "unknown"))
        raise AssertionError("a refused marathon must not construct an orchestrator")

    def git_executor_factory(self, envelope, merges_so_far):
        self.git_executor_calls.append("git_executor")
        raise AssertionError("a refused marathon must not construct a git executor")


@pytest.fixture
def spy() -> ProviderSpy:
    return ProviderSpy()


def build_engine(target: Path, campaign_root: Path, spy: ProviderSpy) -> MarathonDogfoodEngine:
    return MarathonDogfoodEngine(
        provider_pool=ProviderPoolManager(),
        campaign_dir=campaign_root,
        base_output_dir=campaign_root / "output",
        target_repo=target,
        repo_slug="howlcipher/fixture",
        git_executor_factory=spy.git_executor_factory,
        orchestrator_factory=spy.orchestrator_factory,
    )


def run(target: Path, campaign_root: Path, spy: ProviderSpy, resume: Optional[str] = None):
    return build_engine(target, campaign_root, spy).run_backlog_marathon(
        authority_profile_id="howlframe-overnight",
        max_tasks=1,
        max_runtime_hours=1.0,
        resume_campaign_id=resume,
    )


# --------------------------------------------------------------------------
# What counts as dirty
# --------------------------------------------------------------------------

@pytest.mark.integration
def test_a_clean_tracked_repository_is_permitted_past_the_gate(target_repo, tmp_path, spy):
    """The gate must not be a blanket refusal: clean work proceeds."""
    assert describe_working_tree(target_repo).is_clean

    # Getting past the gate means reaching the git executor, which the spy
    # refuses to build -- proving the preflight was not what stopped the run.
    with pytest.raises(AssertionError, match="git executor"):
        run(target_repo, tmp_path / "campaigns", spy)

    assert spy.git_executor_calls == ["git_executor"]


@pytest.mark.integration
def test_modified_tracked_source_is_denied(target_repo, tmp_path, spy):
    (target_repo / "widget.py").write_text("def widget():\n    return 2\n", encoding="utf-8")

    report = run(target_repo, tmp_path / "campaigns", spy)

    assert report["refused"] is True
    assert report["stop_reason"] == DIRTY_TARGET_STOP_REASON
    assert "widget.py" in report["dirty_files"]


@pytest.mark.integration
def test_staged_changes_are_denied(target_repo, tmp_path, spy):
    (target_repo / "staged.py").write_text("x = 1\n", encoding="utf-8")
    _git(target_repo, "add", "staged.py")

    report = run(target_repo, tmp_path / "campaigns", spy)

    assert report["refused"] is True
    assert "staged.py" in report["dirty_files"]


@pytest.mark.integration
def test_untracked_non_ignored_file_is_denied(target_repo, tmp_path, spy):
    (target_repo / "someone_elses_note.md").write_text("half-finished\n", encoding="utf-8")

    report = run(target_repo, tmp_path / "campaigns", spy)

    assert report["refused"] is True
    assert "someone_elses_note.md" in report["dirty_files"]


@pytest.mark.integration
def test_a_deleted_tracked_file_is_denied(target_repo, tmp_path, spy):
    (target_repo / "widget.py").unlink()

    report = run(target_repo, tmp_path / "campaigns", spy)

    assert report["refused"] is True
    assert "widget.py" in report["dirty_files"]


@pytest.mark.integration
def test_ignored_runtime_metadata_is_permitted(target_repo, tmp_path, spy):
    """Git's own ignore rules decide this; a second list would drift from them."""
    (target_repo / "runtime.log").write_text("noise\n", encoding="utf-8")
    (target_repo / "build").mkdir()
    (target_repo / "build" / "artifact.bin").write_bytes(b"\x00")

    assert describe_working_tree(target_repo).is_clean

    with pytest.raises(AssertionError, match="git executor"):
        run(target_repo, tmp_path / "campaigns", spy)


@pytest.mark.integration
def test_control_plane_task_runs_state_is_permitted(target_repo, tmp_path, spy):
    """`.task_runs/` is the control plane's own bookkeeping, not a person's work.

    HowlFrame's checkout carries exactly this, untracked and not gitignored.
    """
    (target_repo / ".task_runs" / "SOME-TASK").mkdir(parents=True)
    (target_repo / ".task_runs" / "SOME-TASK" / "task.yaml").write_text("task_id: SOME-TASK\n", encoding="utf-8")

    assert describe_working_tree(target_repo).is_clean

    with pytest.raises(AssertionError, match="git executor"):
        run(target_repo, tmp_path / "campaigns", spy)


@pytest.mark.integration
def test_a_campaign_directory_inside_the_target_is_permitted(target_repo, tmp_path, spy):
    """The engine's own campaign state is not a change to refuse over."""
    campaign_root = target_repo / ".dogfood_runs"
    campaign_root.mkdir()
    (campaign_root / "DOGFOOD-EARLIER").mkdir()
    (campaign_root / "DOGFOOD-EARLIER" / "campaign_state.json").write_text("{}", encoding="utf-8")

    with pytest.raises(AssertionError, match="git executor"):
        run(target_repo, campaign_root, spy)


# --------------------------------------------------------------------------
# What a refusal must not do
# --------------------------------------------------------------------------

@pytest.mark.integration
def test_a_refusal_invokes_no_provider_and_builds_no_git_executor(target_repo, tmp_path, spy):
    (target_repo / "widget.py").write_text("dirty\n", encoding="utf-8")

    report = run(target_repo, tmp_path / "campaigns", spy)

    assert report["refused"] is True
    assert spy.orchestrator_calls == []
    assert spy.git_executor_calls == []


@pytest.mark.integration
def test_a_refusal_creates_no_branch_and_no_commit(target_repo, tmp_path, spy):
    (target_repo / "widget.py").write_text("dirty\n", encoding="utf-8")
    branches_before = _git(target_repo, "branch", "--list").stdout
    head_before = _git(target_repo, "rev-parse", "HEAD").stdout.strip()

    run(target_repo, tmp_path / "campaigns", spy)

    assert _git(target_repo, "branch", "--list").stdout == branches_before
    assert _git(target_repo, "rev-parse", "HEAD").stdout.strip() == head_before


@pytest.mark.integration
def test_a_refusal_does_not_touch_the_dirty_work_it_found(target_repo, tmp_path, spy):
    """Nothing is cleaned, stashed, reset, or adopted."""
    (target_repo / "widget.py").write_text("someone's unfinished edit\n", encoding="utf-8")
    (target_repo / "untracked_note.md").write_text("keep me\n", encoding="utf-8")
    status_before = _git(target_repo, "status", "--porcelain").stdout
    stash_before = _git(target_repo, "stash", "list").stdout

    run(target_repo, tmp_path / "campaigns", spy)

    assert (target_repo / "widget.py").read_text() == "someone's unfinished edit\n"
    assert (target_repo / "untracked_note.md").read_text() == "keep me\n"
    assert _git(target_repo, "status", "--porcelain").stdout == status_before
    assert _git(target_repo, "stash", "list").stdout == stash_before


@pytest.mark.integration
def test_a_refusal_writes_no_campaign_state(target_repo, tmp_path, spy):
    """A refusal should leave nothing behind to clean up."""
    (target_repo / "widget.py").write_text("dirty\n", encoding="utf-8")
    campaign_root = tmp_path / "campaigns"

    report = run(target_repo, campaign_root, spy)

    assert report["refused"] is True
    assert not campaign_root.exists()
    assert not Path(report["state_dir"]).exists()


@pytest.mark.integration
def test_the_refusal_explains_itself(target_repo, tmp_path, spy):
    (target_repo / "widget.py").write_text("dirty\n", encoding="utf-8")

    report = run(target_repo, tmp_path / "campaigns", spy)
    reason = report["refusal_reason"]

    assert "Refusing to start a new marathon" in reason
    assert str(target_repo) in reason
    assert "widget.py" in report["dirty_summary"]
    # Says what it did NOT do, so an operator is not left wondering.
    assert "cleaned" in reason and "stashed" in reason and "reset" in reason
    # Says how to proceed.
    assert "--resume" in reason


# --------------------------------------------------------------------------
# Resume must keep working
# --------------------------------------------------------------------------

@pytest.mark.integration
def test_resume_of_an_existing_campaign_is_not_blocked_by_a_dirty_worktree(target_repo, tmp_path, spy):
    """A resumed campaign's durable state legitimately owns unfinished work.

    Refusing here would make an interrupted task impossible to finish, which is
    a worse failure than the one the preflight prevents.
    """
    campaign_root = tmp_path / "campaigns"
    state_dir = campaign_root / "DOGFOOD-EXISTING"
    state_dir.mkdir(parents=True)
    (state_dir / "campaign_state.json").write_text(
        '{"campaign_id": "DOGFOOD-EXISTING", "schema": "howlplane.campaign_state/v1"}',
        encoding="utf-8",
    )
    (target_repo / "widget.py").write_text("work in progress from the interrupted run\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="git executor"):
        run(target_repo, campaign_root, spy, resume="DOGFOOD-EXISTING")


@pytest.mark.integration
def test_resume_naming_a_campaign_that_does_not_exist_is_still_a_new_campaign(target_repo, tmp_path, spy):
    """--resume with no state on disk starts fresh, so the gate must apply."""
    (target_repo / "widget.py").write_text("dirty\n", encoding="utf-8")

    report = run(target_repo, tmp_path / "campaigns", spy, resume="DOGFOOD-NEVER-EXISTED")

    assert report["refused"] is True
    assert report["stop_reason"] == DIRTY_TARGET_STOP_REASON


# --------------------------------------------------------------------------
# The operator-facing surface
# --------------------------------------------------------------------------

@pytest.mark.integration
def test_the_cli_reports_a_refusal_on_stderr_with_a_non_zero_exit(target_repo, tmp_path, monkeypatch, capsys):
    """An unattended wrapper must not read a refusal as a run that found no work."""
    from src.control_plane import launcher

    (target_repo / "widget.py").write_text("dirty\n", encoding="utf-8")

    # The refusal returns before a provider pool is ever asked for work; this
    # only keeps the test off the host's real provider configuration.
    monkeypatch.setattr(
        ProviderPoolManager, "from_config", staticmethod(lambda *a, **k: ProviderPoolManager())
    )

    code = launcher.main([
        "marathon",
        "--authority-profile", "howlframe-overnight",
        "--target-repo", str(target_repo),
        "--repo-slug", "howlcipher/fixture",
        "--max-tasks", "1",
        "--max-runtime-hours", "1",
        "--campaign-dir", str(tmp_path / "campaigns"),
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert "MARATHON REFUSED" in captured.err
    assert "widget.py" in captured.err
    assert captured.out == ""


@pytest.mark.integration
def test_dry_run_still_works_against_a_dirty_target(target_repo, tmp_path, capsys):
    """--dry-run reads a backlog and writes nothing, so the gate must not block it.

    This is what a readiness audit runs against a real, dirty checkout.
    """
    from src.control_plane import launcher

    (target_repo / "widget.py").write_text("dirty\n", encoding="utf-8")

    code = launcher.main([
        "marathon",
        "--authority-profile", "howlframe-overnight",
        "--target-repo", str(target_repo),
        "--repo-slug", "howlcipher/fixture",
        "--max-tasks", "1",
        "--max-runtime-hours", "1",
        "--dry-run",
    ])

    assert code == 0
    assert "MARATHON DRY RUN" in capsys.readouterr().out

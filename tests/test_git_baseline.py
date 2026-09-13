"""
test_git_baseline.py

Unit tests for Git baseline capture and task-scoped repository delta isolation.
"""

import os
from pathlib import Path
import subprocess
from typing import Optional

from src.control_plane.git_baseline import (
    GitBaseline,
    _parse_porcelain_lines,
    capture_baseline,
    capture_delta,
    is_baseline_restored,
    restore_repository_to_baseline,
)
from tests._git_test_helpers import init_git_repo


def _init_git_repo(path: Path, files: Optional[dict] = None) -> Path:
    """Helper to initialize a real git repository for testing."""
    default_files = {
        "README.md": "# Test Repo\n",
        "existing.py": "def old_fn():\n    return 1\n",
    }
    return init_git_repo(path, files=files if files is not None else default_files)


def test_capture_baseline_clean_repo(tmp_path):
    repo = _init_git_repo(tmp_path / "repo_clean")
    baseline = capture_baseline(repo)

    assert baseline.repo_root == str(repo.resolve())
    assert len(baseline.initial_commit_sha) >= 7
    assert baseline.pre_existing_modified == []
    assert baseline.pre_existing_untracked == []

    # Serialization
    d = baseline.to_dict()
    assert d["repo_root"] == str(repo.resolve())
    restored = GitBaseline.from_dict(d)
    assert restored.initial_commit_sha == baseline.initial_commit_sha


def test_capture_baseline_dirty_repo(tmp_path):
    repo = _init_git_repo(tmp_path / "repo_dirty")
    # Dirty state before task starts
    (repo / "existing.py").write_text("def old_fn():\n    return 2 # modified\n", encoding="utf-8")
    (repo / "pre_existing_untracked.txt").write_text("scratch", encoding="utf-8")

    baseline = capture_baseline(repo)
    assert "existing.py" in baseline.pre_existing_modified
    assert "pre_existing_untracked.txt" in baseline.pre_existing_untracked


def test_capture_delta_new_file_and_modified(tmp_path):
    repo = _init_git_repo(tmp_path / "repo_delta")
    baseline = capture_baseline(repo)

    # Agent acts: creates a new file and modifies existing.py
    (repo / "new_feature.py").write_text("def new_feature():\n    return True\n", encoding="utf-8")
    (repo / "existing.py").write_text("def old_fn():\n    return 99\n", encoding="utf-8")

    delta = capture_delta(repo, baseline)
    assert delta.is_empty is False
    assert "new_feature.py" in delta.files_added
    assert "existing.py" in delta.files_modified
    assert delta.files_deleted == []
    assert delta.insertions > 0
    assert "diff --git a/new_feature.py" in delta.diff_content
    assert "def new_feature" in delta.diff_content
    assert "def old_fn" in delta.diff_content


def test_capture_delta_isolates_pre_existing_dirty_files(tmp_path):
    repo = _init_git_repo(tmp_path / "repo_isolation")
    # Pre-existing dirty files
    (repo / "unrelated_dirty.txt").write_text("unrelated scratch\n", encoding="utf-8")
    (repo / "existing.py").write_text("def old_fn():\n    return 'dirty'\n", encoding="utf-8")

    baseline = capture_baseline(repo)
    assert "unrelated_dirty.txt" in baseline.pre_existing_untracked
    assert "existing.py" in baseline.pre_existing_modified

    # Agent only creates task_feature.py without touching pre-existing dirty files
    (repo / "task_feature.py").write_text("def task(): pass\n", encoding="utf-8")

    delta = capture_delta(repo, baseline)
    assert "task_feature.py" in delta.files_added
    # Pre-existing files should NOT be attributed to the task as newly added/modified
    assert "unrelated_dirty.txt" not in delta.files_added
    assert "unrelated_dirty.txt" in delta.pre_existing_excluded
    assert "existing.py" in delta.pre_existing_excluded


def test_capture_delta_file_deletion(tmp_path):
    repo = _init_git_repo(tmp_path / "repo_del")
    baseline = capture_baseline(repo)

    # Agent deletes existing.py
    (repo / "existing.py").unlink()

    delta = capture_delta(repo, baseline)
    assert "existing.py" in delta.files_deleted
    assert delta.is_empty is False


def test_captured_delta_patch_is_replayable_by_git_apply(tmp_path):
    """HOWLFRAM-SLOPFIX-05: preserved evidence has to survive `git apply`.

    Every attempt patch in the evidence store was written straight from
    diff_content, which was assembled from stripped hunks and so ended without
    a trailing newline. git rejected all of them as corrupt at the final line,
    which meant a preserved candidate could never be replayed, reviewed, or
    verified -- only read.
    """
    repo = _init_git_repo(tmp_path / "repo_replay")
    baseline = capture_baseline(repo)

    (repo / "existing.py").write_text("def old_fn():\n    return 2\n", encoding="utf-8")
    (repo / "added.py").write_text("VALUE = 1\n", encoding="utf-8")

    delta = capture_delta(repo, baseline)
    assert not delta.is_empty
    assert delta.diff_content.endswith("\n")
    assert not delta.diff_content.endswith("\n\n")

    patch = tmp_path / "candidate.patch"
    patch.write_text(delta.diff_content, encoding="utf-8")

    # The patch describes exactly the working tree we are standing in, so it
    # must reverse-apply cleanly against it...
    assert subprocess.run(
        ["git", "apply", "--check", "--reverse", str(patch)],
        cwd=repo, capture_output=True, text=True,
    ).returncode == 0

    # ...and forward-apply cleanly once the tree is back at baseline, which is
    # what governing a captured candidate actually requires.
    subprocess.run(["git", "apply", "--reverse", str(patch)], cwd=repo, check=True)
    applied = subprocess.run(
        ["git", "apply", str(patch)],
        cwd=repo, capture_output=True, text=True,
    )
    assert applied.returncode == 0, applied.stderr
    assert (repo / "existing.py").read_text(encoding="utf-8") == "def old_fn():\n    return 2\n"
    assert (repo / "added.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_empty_delta_stays_empty_not_a_bare_newline(tmp_path):
    """Normalizing the patch must not turn 'no changes' into a one-byte file."""
    repo = _init_git_repo(tmp_path / "repo_empty")
    delta = capture_delta(repo, capture_baseline(repo))

    assert delta.is_empty
    assert delta.diff_content == ""


def test_parse_porcelain_records_both_sides_of_a_staged_rename():
    """A staged rename must report the destination added AND the source deleted.

    "R  old -> new" carries no D and no A, so it used to fall through to
    `modified` carrying only the destination. The source file's disappearance
    was dropped, and anything rebuilding a tree from the delta kept both paths.
    """
    untracked, modified, deleted, added = _parse_porcelain_lines(
        "R  docs/archive/old.go -> docs/archive/old.go.txt\n"
    )
    assert added == {"docs/archive/old.go.txt"}
    assert deleted == {"docs/archive/old.go"}
    assert modified == set()
    assert untracked == set()


def test_parse_porcelain_rename_with_later_edit_still_deletes_the_source():
    """"RM" is a staged rename whose destination was edited afterwards."""
    _, modified, deleted, added = _parse_porcelain_lines("RM old.py -> new.py\n")
    assert added == {"new.py"}
    assert deleted == {"old.py"}
    assert modified == set()


def test_parse_porcelain_copy_does_not_delete_its_source():
    """A copy names a source too, but leaves it in place."""
    _, _, deleted, added = _parse_porcelain_lines("C  template.py -> derived.py\n")
    assert added == {"derived.py"}
    assert deleted == set()


def test_parse_porcelain_rename_of_control_plane_paths_is_filtered():
    """Control plane metadata stays out of the delta on both sides of a rename."""
    _, _, deleted, added = _parse_porcelain_lines(
        "R  .task_runs/T/a.go -> .task_runs/T/b.go\n"
        "R  .task_runs/T/c.go -> product.go\n"
        "R  product_old.go -> .task_runs/T/d.go\n"
    )
    assert added == {"product.go"}
    assert deleted == {"product_old.go"}


def test_capture_delta_reports_a_staged_rename_as_added_plus_deleted(tmp_path):
    repo = _init_git_repo(tmp_path / "repo_rename")
    baseline = capture_baseline(repo)

    subprocess.run(["git", "mv", "existing.py", "renamed.py"], cwd=repo, check=True)

    delta = capture_delta(repo, baseline)
    assert "renamed.py" in delta.files_added
    assert "existing.py" in delta.files_deleted
    assert delta.is_empty is False


def test_capture_delta_still_handles_an_unstaged_rename(tmp_path):
    """Unstaged renames arrive as ' D old' + '?? new' and already worked."""
    repo = _init_git_repo(tmp_path / "repo_rename_unstaged")
    baseline = capture_baseline(repo)

    (repo / "moved.py").write_text((repo / "existing.py").read_text())
    (repo / "existing.py").unlink()

    delta = capture_delta(repo, baseline)
    assert "moved.py" in delta.files_added
    assert "existing.py" in delta.files_deleted


def test_baseline_records_existing_directories(tmp_path):
    repo = init_git_repo(
        tmp_path / "repo_dirs",
        files={"src/existing.py": "x\n", "docs/readme.md": "# r\n"},
    )
    baseline = capture_baseline(repo)
    assert "src" in baseline.pre_existing_directories
    assert "docs" in baseline.pre_existing_directories


def test_restore_baseline_removes_task_created_directories(tmp_path):
    """Rollback must delete empty directories created by the attempt."""
    repo = _init_git_repo(tmp_path / "repo_created_dirs", files={"src/app.py": "x\n"})
    baseline = capture_baseline(repo)

    (repo / "internal/hfirstore").mkdir(parents=True)
    (repo / "internal/hfirstore/schema.go").write_text("package hfirstore\n", encoding="utf-8")
    (repo / "internal/hfirstore/loader.go").write_text("package hfirstore\n", encoding="utf-8")

    delta = capture_delta(repo, baseline)
    # git status reports the whole untracked directory tree as a single entry.
    assert "internal/" in delta.files_added

    ok, reason = restore_repository_to_baseline(repo, baseline, delta)
    assert ok, reason
    assert not (repo / "internal/hfirstore").exists()
    assert (repo / "src").exists()
    assert capture_delta(repo, baseline).is_empty


def test_restore_baseline_removes_task_created_symlinks(tmp_path):
    """Symlinks added by the attempt must be removed, not followed."""
    repo = _init_git_repo(tmp_path / "repo_symlinks", files={"src/app.py": "x\n"})
    baseline = capture_baseline(repo)

    (repo / "link.py").symlink_to("src/app.py")
    delta = capture_delta(repo, baseline)
    assert "link.py" in delta.files_added

    ok, reason = restore_repository_to_baseline(repo, baseline, delta)
    assert ok, reason
    assert not (repo / "link.py").exists()
    assert capture_delta(repo, baseline).is_empty


def test_restore_baseline_preserves_pre_existing_untracked_directories(tmp_path):
    """Pre-existing untracked directories and their contents must survive."""
    repo = _init_git_repo(tmp_path / "repo_keep_dirs", files={"src/app.py": "x\n"})
    scratch = repo / "scratch"
    scratch.mkdir()
    (scratch / "note.txt").write_text("keep me\n", encoding="utf-8")
    baseline = capture_baseline(repo)
    assert "scratch" in baseline.pre_existing_directories
    # git status --porcelain reports an untracked directory as a single entry
    # ending in '/', not the individual files inside it.
    assert "scratch/" in baseline.pre_existing_untracked

    (repo / "src/new.py").write_text("y\n", encoding="utf-8")
    delta = capture_delta(repo, baseline)

    ok, reason = restore_repository_to_baseline(repo, baseline, delta)
    assert ok, reason
    assert (scratch / "note.txt").exists()
    assert not (repo / "src/new.py").exists()


def test_restore_baseline_preserves_pre_existing_modified_files(tmp_path):
    """Pre-existing dirty files keep their current content when the attempt did not touch them."""
    repo = _init_git_repo(tmp_path / "repo_keep_dirty", files={"src/app.py": "original\n"})
    (repo / "src/app.py").write_text("changed by user\n", encoding="utf-8")
    baseline = capture_baseline(repo)

    (repo / "src/new.py").write_text("task\n", encoding="utf-8")
    delta = capture_delta(repo, baseline)

    ok, reason = restore_repository_to_baseline(repo, baseline, delta)
    assert ok, reason
    assert (repo / "src/app.py").read_text(encoding="utf-8") == "changed by user\n"
    assert not (repo / "src/new.py").exists()


def test_restore_baseline_task_files_beneath_pre_existing_directories(tmp_path):
    """A new file under an existing directory is removed; the existing parent remains."""
    repo = _init_git_repo(tmp_path / "repo_nested", files={"internal/base.py": "x\n"})
    baseline = capture_baseline(repo)

    (repo / "internal/hfirstore").mkdir()
    (repo / "internal/hfirstore/data.go").write_text("package data\n", encoding="utf-8")
    delta = capture_delta(repo, baseline)

    ok, reason = restore_repository_to_baseline(repo, baseline, delta)
    assert ok, reason
    assert (repo / "internal").exists()
    assert (repo / "internal/base.py").exists()
    assert not (repo / "internal/hfirstore").exists()
    assert capture_delta(repo, baseline).is_empty


def test_restore_baseline_empty_task_created_parents_removed(tmp_path):
    """Rollback removes intermediate directories that only existed because of task files."""
    repo = _init_git_repo(tmp_path / "repo_empty_parents", files={"src/app.py": "x\n"})
    baseline = capture_baseline(repo)

    (repo / "a/b/c").mkdir(parents=True)
    (repo / "a/b/c/file.go").write_text("package c\n", encoding="utf-8")
    delta = capture_delta(repo, baseline)

    ok, reason = restore_repository_to_baseline(repo, baseline, delta)
    assert ok, reason
    assert not (repo / "a").exists()


def test_restore_baseline_path_traversal_symlink_fails_closed(tmp_path):
    """A task-created symlink pointing outside the repo must not escape the rollback boundary."""
    repo = _init_git_repo(tmp_path / "repo_escape", files={"src/app.py": "x\n"})
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("outside secret\n", encoding="utf-8")
    baseline = capture_baseline(repo)

    (repo / "evil").symlink_to(outside)
    delta = capture_delta(repo, baseline)
    assert "evil" in delta.files_added

    ok, reason = restore_repository_to_baseline(repo, baseline, delta)
    assert ok, reason
    assert not (repo / "evil").exists()
    assert outside.exists()
    assert outside.read_text(encoding="utf-8") == "outside secret\n"


def test_restore_baseline_idempotent_after_partial_cleanup(tmp_path):
    """Interrupted rollback: a second restore call still reaches baseline."""
    repo = _init_git_repo(tmp_path / "repo_idem", files={"src/app.py": "x\n"})
    baseline = capture_baseline(repo)

    (repo / "task1.py").write_text("a\n", encoding="utf-8")
    (repo / "task2.py").write_text("b\n", encoding="utf-8")
    delta = capture_delta(repo, baseline)

    # Simulate an interruption that removed one task file but left the other.
    (repo / "task1.py").unlink()

    ok, reason = restore_repository_to_baseline(repo, baseline, delta)
    assert ok, reason
    assert not (repo / "task1.py").exists()
    assert not (repo / "task2.py").exists()
    assert capture_delta(repo, baseline).is_empty

    # A second call is a no-op and still succeeds.
    ok2, reason2 = restore_repository_to_baseline(repo, baseline)
    assert ok2, reason2
    assert capture_delta(repo, baseline).is_empty


def test_is_baseline_restored_true_after_clean_restore(tmp_path):
    """A correctly restored repository passes the invariant check."""
    repo = _init_git_repo(tmp_path / "repo_restored", files={"src/app.py": "original\n"})
    (repo / "user.log").write_text("start\n", encoding="utf-8")
    baseline = capture_baseline(repo)

    # Attempt changes a pre-existing untracked file and adds a task file.
    (repo / "user.log").write_text("end\n", encoding="utf-8")
    (repo / "task_feature.py").write_text("task\n", encoding="utf-8")
    delta = capture_delta(repo, baseline)

    ok, reason = restore_repository_to_baseline(repo, baseline, delta)
    assert ok, reason

    restored, reason2 = is_baseline_restored(repo, baseline)
    assert restored, reason2


def test_is_baseline_restored_false_with_residual_task_delta(tmp_path):
    """A leftover task-created file is detected as not restored."""
    repo = _init_git_repo(tmp_path / "repo_residual")
    baseline = capture_baseline(repo)

    (repo / "leftover.py").write_text("task\n", encoding="utf-8")
    restored, reason = is_baseline_restored(repo, baseline)
    assert not restored
    assert "task-attributable delta remains" in reason


def test_is_baseline_restored_false_when_pre_existing_snapshot_changed(tmp_path):
    """A pre-existing file that does not match its retained snapshot fails."""
    repo = _init_git_repo(tmp_path / "repo_snapshot")
    (repo / "user.log").write_text("start\n", encoding="utf-8")
    baseline = capture_baseline(repo)

    # Modify the file without restoring it (simulating a failed rollback).
    (repo / "user.log").write_text("end\n", encoding="utf-8")
    restored, reason = is_baseline_restored(repo, baseline)
    assert not restored
    assert "does not match baseline snapshot" in reason


def test_is_baseline_restored_ignores_legitimate_control_plane_artifacts(tmp_path):
    """Control-plane state created during an attempt is excluded from verification."""
    repo = _init_git_repo(tmp_path / "repo_ctrl", files={"src/app.py": "x\n"})
    baseline = capture_baseline(repo)

    log = repo / ".task_runs" / "WI-1" / "log.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("provider log\n", encoding="utf-8")
    restored, reason = is_baseline_restored(repo, baseline)
    assert restored, reason

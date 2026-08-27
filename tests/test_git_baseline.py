"""
test_git_baseline.py

Unit tests for Git baseline capture and task-scoped repository delta isolation.
"""

from pathlib import Path
import subprocess

from src.control_plane.git_baseline import (
    GitBaseline,
    capture_baseline,
    capture_delta,
)
from tests._git_test_helpers import init_git_repo


def _init_git_repo(path: Path) -> Path:
    """Helper to initialize a real git repository for testing."""
    return init_git_repo(
        path,
        files={
            "README.md": "# Test Repo\n",
            "existing.py": "def old_fn():\n    return 1\n",
        },
    )


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

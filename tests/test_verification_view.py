"""
test_verification_view.py

Verifies that deterministic verification runs against a sanitized view of the
repository (baseline commit + task-attributable delta) rather than the live
checkout (HOWLFRAM-SLOPFIX-07S).

The canary showed a provider's source clone under `.task_runs/` moving
slopslint's `go_production` count from 291 to 1421 with product code untouched,
and the target repository may not be asked to carry ignore rules for HowlPlane
internals. These tests pin the property that makes that impossible: control
plane evidence never enters the tree the gates measure.
"""

from pathlib import Path
import subprocess

import pytest

from src.control_plane.atomic_io import safe_load_json
from src.control_plane.git_baseline import capture_baseline, capture_delta
from src.control_plane.git_env import run_git_in_repo
from src.control_plane.verification_view import (
    VerificationViewError,
    build_verification_view,
    destroy_verification_view,
    resolve_external_scratch_root,
    verification_view,
)
from tests.test_provider_failover import (
    _FakeBackendResolver,
    _edit_feature_to_true,
    _init_test_repo,
    _run_failover_task,
)

TASK_ID = "VIEW-TEST-01"


def _commit_all(repo: Path, message: str) -> str:
    run_git_in_repo(repo, ["add", "-A"])
    run_git_in_repo(repo, ["commit", "-q", "-m", message])
    return run_git_in_repo(repo, ["rev-parse", "HEAD"]).stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    """A minimal git repo with one tracked product file."""
    repo = tmp_path / "target_repo"
    repo.mkdir()
    run_git_in_repo(repo, ["init", "-q", "-b", "main"])
    run_git_in_repo(repo, ["config", "user.email", "test@example.com"])
    run_git_in_repo(repo, ["config", "user.name", "Test"])
    (repo / "product.go").write_text("package main\n\nfunc Product() int { return 1 }\n")
    _commit_all(repo, "initial")
    return repo


def _plant_scratch_clone(repo: Path) -> Path:
    """Reproduces the SLOPFIX-07S contamination: a source clone under .task_runs."""
    scratch = repo / ".task_runs" / TASK_ID / "provider_scratch" / "02-codex" / "clone-isolation"
    scratch.mkdir(parents=True)
    (scratch / "go.mod").write_text("module clone\n\ngo 1.21\n")
    (scratch / "product.go").write_text("package main\n\nfunc Product() int { return 1 }\n")
    return scratch


def _built(repo: Path, baseline, delta, tmp_path: Path):
    """Context-managed view over an explicit baseline/delta pair."""
    return verification_view(
        target_repo=repo, baseline=baseline, delta=delta,
        task_id=TASK_ID, scratch_root=tmp_path / "scratch",
    )


def _view_for(repo: Path, scratch_root: Path, **kwargs):
    baseline = capture_baseline(repo)
    delta = capture_delta(repo, baseline)
    return build_verification_view(
        target_repo=repo,
        baseline=baseline,
        delta=delta,
        task_id=TASK_ID,
        scratch_root=scratch_root,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. The regression the canary demonstrated: 291 -> 1421
# ---------------------------------------------------------------------------


def test_untracked_control_plane_evidence_never_enters_the_view(tmp_path: Path):
    """A provider source clone under .task_runs must not reach the gates."""
    repo = _make_repo(tmp_path)
    _plant_scratch_clone(repo)

    view = _view_for(repo, tmp_path / "scratch")
    try:
        # The contamination is still on disk in the real repository...
        assert (repo / ".task_runs" / TASK_ID).is_dir()
        # ...and entirely absent from the tree the gates measure.
        assert not (view.path / ".task_runs").exists()
        go_files = sorted(p.relative_to(view.path).as_posix() for p in view.path.rglob("*.go"))
        assert go_files == ["product.go"]
    finally:
        destroy_verification_view(view)


def test_view_holds_baseline_content_at_the_baseline_commit(tmp_path: Path):
    repo = _make_repo(tmp_path)
    baseline = capture_baseline(repo)

    view = _view_for(repo, tmp_path / "scratch")
    try:
        assert view.baseline_sha == baseline.initial_commit_sha
        assert (view.path / "product.go").read_text() == (repo / "product.go").read_text()
        head = run_git_in_repo(view.path, ["rev-parse", "HEAD"]).stdout.strip()
        assert head == baseline.initial_commit_sha
    finally:
        destroy_verification_view(view)


# ---------------------------------------------------------------------------
# 2. The view is baseline + task-attributable delta, exactly
# ---------------------------------------------------------------------------


def test_task_delta_is_materialized_byte_for_byte(tmp_path: Path):
    """Modified, added, and deleted product files all land correctly."""
    repo = _make_repo(tmp_path)
    (repo / "doomed.txt").write_text("remove me\n")
    _commit_all(repo, "add doomed")

    baseline = capture_baseline(repo)
    (repo / "product.go").write_text("package main\n\nfunc Product() int { return 42 }\n")
    (repo / "added.go").write_text("package main\n\nfunc Added() {}\n")
    (repo / "doomed.txt").unlink()
    delta = capture_delta(repo, baseline)

    with _built(repo, baseline, delta, tmp_path) as view:
        assert (view.path / "product.go").read_bytes() == (repo / "product.go").read_bytes()
        assert (view.path / "added.go").read_bytes() == (repo / "added.go").read_bytes()
        assert not (view.path / "doomed.txt").exists()
        assert "doomed.txt" in view.files_removed
        assert set(view.files_materialized) == {"product.go", "added.go"}


def test_binary_addition_survives_the_view(tmp_path: Path):
    """The recorded patch is text-only; the view must not lose binary additions."""
    repo = _make_repo(tmp_path)
    baseline = capture_baseline(repo)
    payload = bytes(range(256)) * 4
    (repo / "asset.bin").write_bytes(payload)
    delta = capture_delta(repo, baseline)

    with _built(repo, baseline, delta, tmp_path) as view:
        assert (view.path / "asset.bin").read_bytes() == payload


def test_empty_delta_yields_a_clean_baseline_view(tmp_path: Path):
    repo = _make_repo(tmp_path)
    _plant_scratch_clone(repo)
    baseline = capture_baseline(repo)
    delta = capture_delta(repo, baseline)

    with _built(repo, baseline, delta, tmp_path) as view:
        assert view.files_materialized == []
        assert not (view.path / ".task_runs").exists()


# ---------------------------------------------------------------------------
# 3. Containment
# ---------------------------------------------------------------------------


class _StubBaseline:
    def __init__(self, sha: str):
        self.initial_commit_sha = sha


class _StubDelta:
    def __init__(self, added=None, modified=None, deleted=None, diff_content=""):
        self.files_added = added or []
        self.files_modified = modified or []
        self.files_deleted = deleted or []
        self.diff_content = diff_content


@pytest.mark.parametrize(
    "hostile_path",
    [
        ".task_runs/TASK/provider_scratch/clone/product.go",
        ".git/hooks/pre-commit",
        "../escaped.go",
        "/etc/passwd",
        ".howlchangeops/state.json",
    ],
)
def test_control_plane_and_escaping_paths_are_refused(tmp_path: Path, hostile_path: str):
    """A delta may not carry control plane metadata or reach outside the repo."""
    repo = _make_repo(tmp_path)
    sha = run_git_in_repo(repo, ["rev-parse", "HEAD"]).stdout.strip()
    (tmp_path / "escaped.go").write_text("package main\n")

    view = build_verification_view(
        target_repo=repo,
        baseline=_StubBaseline(sha),
        delta=_StubDelta(added=[hostile_path]),
        task_id=TASK_ID,
        scratch_root=tmp_path / "scratch",
    )
    try:
        assert hostile_path in view.files_refused
        assert hostile_path not in view.files_materialized
        assert not (tmp_path / "escaped.go" ).with_name("escaped_copied.go").exists()
    finally:
        destroy_verification_view(view)


def test_scratch_root_inside_the_target_repo_is_rejected(tmp_path: Path):
    """A view must never be built inside the tree it is meant to sanitize."""
    repo = _make_repo(tmp_path)
    resolved = resolve_external_scratch_root(repo, repo / ".task_runs" / "views")
    assert not resolved.is_relative_to(repo.resolve())

    view = _view_for(repo, repo / ".task_runs" / "views")
    try:
        assert not view.path.is_relative_to(repo.resolve())
    finally:
        destroy_verification_view(view)


def test_missing_baseline_sha_fails_closed(tmp_path: Path):
    repo = _make_repo(tmp_path)
    with pytest.raises(VerificationViewError):
        build_verification_view(
            target_repo=repo,
            baseline=_StubBaseline(""),
            delta=_StubDelta(),
            task_id=TASK_ID,
            scratch_root=tmp_path / "scratch",
        )


def test_unknown_baseline_commit_fails_closed(tmp_path: Path):
    repo = _make_repo(tmp_path)
    with pytest.raises(VerificationViewError):
        build_verification_view(
            target_repo=repo,
            baseline=_StubBaseline("0" * 40),
            delta=_StubDelta(),
            task_id=TASK_ID,
            scratch_root=tmp_path / "scratch",
        )


# ---------------------------------------------------------------------------
# 4. Lifecycle: teardown, crash recovery, provenance
# ---------------------------------------------------------------------------


def test_teardown_removes_the_worktree_and_prunes(tmp_path: Path):
    repo = _make_repo(tmp_path)
    with verification_view(
        target_repo=repo,
        baseline=capture_baseline(repo),
        delta=_StubDelta(),
        task_id=TASK_ID,
        scratch_root=tmp_path / "scratch",
    ) as view:
        view_path = view.path
        assert view_path.is_dir()
        listed = run_git_in_repo(repo, ["worktree", "list"]).stdout
        assert str(view_path) in listed

    assert not view_path.exists()
    assert view.cleanup_status == "removed"
    listed = run_git_in_repo(repo, ["worktree", "list"]).stdout
    assert str(view_path) not in listed


def test_a_stale_view_from_a_crashed_run_is_rebuilt_not_reused(tmp_path: Path):
    """A crash leaves a view behind; the next run must not verify against it."""
    repo = _make_repo(tmp_path)
    scratch_root = tmp_path / "scratch"

    crashed = _view_for(repo, scratch_root)
    (crashed.path / "stale_evidence.go").write_text("package main\n// left by a crash\n")
    stale_path = crashed.path
    # Deliberately no teardown: simulate the process dying mid-verification.

    fresh = _view_for(repo, scratch_root)
    try:
        assert fresh.path == stale_path
        assert not (fresh.path / "stale_evidence.go").exists()
    finally:
        destroy_verification_view(fresh)


def test_cleanup_status_is_recorded_truthfully(tmp_path: Path):
    repo = _make_repo(tmp_path)
    view = _view_for(repo, tmp_path / "scratch")
    assert view.to_dict()["cleanup_status"] == "active"
    destroy_verification_view(view)
    record = view.to_dict()
    assert record["cleanup_status"] == "removed"
    assert record["baseline_sha"] == view.baseline_sha
    assert record["schema"] == "howlplane.verification_view/v1"


def test_dependency_directories_are_linked_not_copied(tmp_path: Path):
    """Interpreters and dependency trees are too large to copy and are linked."""
    repo = _make_repo(tmp_path)
    (repo / "venv" / "bin").mkdir(parents=True)
    (repo / "venv" / "bin" / "python").write_text("#!/bin/sh\n")

    view = _view_for(repo, tmp_path / "scratch")
    try:
        assert "venv" in view.linked_paths
        assert (view.path / "venv").is_symlink()
        assert (view.path / "venv").resolve() == (repo / "venv").resolve()
    finally:
        destroy_verification_view(view)


# ---------------------------------------------------------------------------
# 5. Orchestrator wiring
# ---------------------------------------------------------------------------


def _run_with_isolation(repo: Path, **overrides):
    resolver = _FakeBackendResolver({
        "resource_a": {"success": True, "side_effect": _edit_feature_to_true},
    })
    return _run_failover_task(repo, resolver, max_attempts=1, **overrides)


def test_orchestrator_verifies_in_the_view_and_records_evidence(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "target_repo")
    res = _run_with_isolation(repo)
    assert res.final_state == "complete"

    record = safe_load_json(Path(res.run_dir) / "verification_view.json")
    assert record["schema"] == "howlplane.verification_view/v1"
    assert record["cleanup_status"] == "removed"
    assert not Path(record["view_path"]).exists()
    assert not Path(record["view_path"]).is_relative_to(repo.resolve())
    # The evidence tree itself stays where the audit expects it.
    assert (Path(res.run_dir) / "verification_result.json").is_file()


def test_isolation_can_be_disabled_for_the_previous_in_place_behaviour(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "target_repo")
    res = _run_with_isolation(repo, verification_isolation=False)
    assert res.final_state == "complete"
    assert not (Path(res.run_dir) / "verification_view.json").exists()


def test_no_worktree_is_leaked_after_a_governed_run(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "target_repo")
    res = _run_with_isolation(repo)
    assert res.final_state == "complete"
    listed = run_git_in_repo(repo, ["worktree", "list"]).stdout.strip().splitlines()
    assert len(listed) == 1

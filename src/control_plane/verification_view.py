#!/usr/bin/env python3
"""
verification_view.py

Sanitized verification views for deterministic gates.

Deterministic verification used to execute directly in the live target
checkout, so anything sitting untracked beside the product -- provider
scratch, task evidence, build caches -- was inside the blast radius of every
gate that walks the filesystem. HOWLFRAM-SLOPFIX-07S demonstrated the
consequence: a provider left an isolated source clone under
`.task_runs/<task>/provider_scratch/`, and `slopslint check --classify
--enforce` went from 291 to 1421 `go_production` clones with the product code
untouched.

Externalising new scratch (PR #57) stops the control plane from *adding* that
debris, but evidence already written into a target repository keeps poisoning
later runs, and a target repository must not be asked to carry ignore rules for
HowlPlane internals. So the gates move instead of the evidence: they run
against a disposable worktree built from the task's baseline commit plus the
task-attributable delta, and nothing else. What the control plane never put in
the view cannot change a verification result.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
from typing import Any, Dict, Iterator, List, Optional, Sequence, Union

from src.control_plane.git_baseline import is_internal_control_plane_path
from src.control_plane.git_env import run_git_in_repo

VERIFICATION_VIEW_SCHEMA_VERSION = "howlplane.verification_view/v1"

# Untracked directories a gate legitimately needs (interpreters, dependency
# trees) that are too large to copy and are already excluded by both repos'
# hygiene scopes. Linked, never copied.
DEFAULT_LINKED_DEPENDENCY_DIRS = ("venv", ".venv", "node_modules")


class VerificationViewError(RuntimeError):
    """Raised when a sanitized verification view cannot be constructed."""


def resolve_external_scratch_root(
    target_repo: Union[str, Path],
    configured_root: Optional[Union[str, Path]] = None,
) -> Path:
    """
    Resolves the control-plane-owned scratch root, which must live outside the
    target repository.

    Order: explicit configuration, then $HOWLPLANE_SCRATCH_ROOT, then
    $XDG_CACHE_HOME/howlplane/scratch (falling back to ~/.cache). A root that
    resolves inside the target repository is rejected and replaced with the
    cache default, because the whole point is to keep control-plane state out
    of the tree the gates measure.
    """
    def _cache_default() -> Path:
        xdg = os.environ.get("XDG_CACHE_HOME")
        cache_base = Path(xdg) if xdg else (Path.home() / ".cache")
        return (cache_base / "howlplane" / "scratch").resolve()

    if configured_root is not None:
        base = Path(configured_root).resolve()
    elif os.environ.get("HOWLPLANE_SCRATCH_ROOT"):
        base = Path(os.environ["HOWLPLANE_SCRATCH_ROOT"]).resolve()
    else:
        base = _cache_default()

    repo_res = Path(target_repo).resolve()
    if base == repo_res or base.is_relative_to(repo_res):
        base = _cache_default()
    return base


@dataclass
class VerificationView:
    """A disposable checkout the deterministic gates run against."""

    path: Path
    repo_root: Path
    task_id: str
    baseline_sha: str
    files_materialized: List[str] = field(default_factory=list)
    files_removed: List[str] = field(default_factory=list)
    files_refused: List[str] = field(default_factory=list)
    linked_paths: List[str] = field(default_factory=list)
    delta_patch_sha256: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cleanup_status: str = "active"
    cleanup_error: Optional[str] = None
    schema: str = VERIFICATION_VIEW_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "view_path": str(self.path),
            "repo_root": str(self.repo_root),
            "baseline_sha": self.baseline_sha,
            "created_at": self.created_at,
            "files_materialized": self.files_materialized,
            "files_removed": self.files_removed,
            "files_refused": self.files_refused,
            "linked_paths": self.linked_paths,
            "delta_patch_sha256": self.delta_patch_sha256,
            "cleanup_status": self.cleanup_status,
            "cleanup_error": self.cleanup_error,
        }


def _view_dir_for(scratch_root: Path, repo_root: Path, task_id: str) -> Path:
    repo_slug = repo_root.name or "repo"
    return scratch_root / repo_slug / task_id / "verification_view"


def _is_safe_relative_path(repo_root: Path, view_root: Path, rel_path: str) -> bool:
    """
    Rejects anything that is not a plain repository-relative path landing inside
    both the repository and the view: absolute paths, traversal, and control
    plane metadata (`.task_runs`, `.git`, `.howlchangeops`).
    """
    if not rel_path or Path(rel_path).is_absolute():
        return False
    if is_internal_control_plane_path(rel_path):
        return False
    try:
        src_res = (repo_root / rel_path).resolve()
        dst_res = (view_root / rel_path).resolve()
    except OSError:
        return False
    if not (src_res == repo_root or src_res.is_relative_to(repo_root)):
        return False
    if not (dst_res == view_root or dst_res.is_relative_to(view_root)):
        return False
    return True


def _link_dependency_dirs(
    repo_root: Path, view_root: Path, names: Sequence[str]
) -> List[str]:
    """Symlinks untracked dependency/interpreter dirs into the view."""
    linked: List[str] = []
    for name in names:
        source = repo_root / name
        if not source.is_dir():
            continue
        target = view_root / name
        if target.exists() or target.is_symlink():
            continue
        try:
            target.symlink_to(source.resolve(), target_is_directory=True)
            linked.append(name)
        except OSError:
            continue
    return linked


def build_verification_view(
    target_repo: Union[str, Path],
    baseline: Any,
    delta: Any,
    task_id: str,
    scratch_root: Optional[Union[str, Path]] = None,
    linked_dependency_dirs: Sequence[str] = DEFAULT_LINKED_DEPENDENCY_DIRS,
) -> VerificationView:
    """
    Builds `baseline commit + task-attributable delta` as a disposable git
    worktree outside the target repository.

    The delta is materialized by byte-copy rather than by replaying
    `delta.diff_content`. The recorded patch is a text diff, so a binary file an
    agent added has no faithful representation in it (see
    `git_baseline._generate_untracked_diff`), and a patch that fails to apply
    would silently verify the wrong tree. The patch digest is still recorded, so
    the view stays tied to the evidence that explains it.

    Raises VerificationViewError if the view cannot be constructed. Callers must
    fail closed: falling back to the live checkout would reinstate exactly the
    contamination this exists to prevent.
    """
    repo_root = Path(target_repo).resolve()
    baseline_sha = getattr(baseline, "initial_commit_sha", "") or ""
    if not baseline_sha:
        raise VerificationViewError("Baseline has no initial_commit_sha to build a view from.")

    base = resolve_external_scratch_root(repo_root, scratch_root)
    view_root = _view_dir_for(base, repo_root, task_id)

    # A leftover view from a crashed run must not be verified against.
    if view_root.exists():
        _remove_worktree(repo_root, view_root)
    view_root.parent.mkdir(parents=True, exist_ok=True)

    add_proc = run_git_in_repo(
        repo_root, ["worktree", "add", "--detach", str(view_root), baseline_sha], timeout=180
    )
    if add_proc.returncode != 0 or not view_root.is_dir():
        raise VerificationViewError(
            f"Could not create verification worktree at {view_root}: "
            f"{(add_proc.stderr or add_proc.stdout or '').strip()}"
        )

    view = VerificationView(
        path=view_root,
        repo_root=repo_root,
        task_id=task_id,
        baseline_sha=baseline_sha,
    )

    diff_content = getattr(delta, "diff_content", "") or ""
    if diff_content:
        view.delta_patch_sha256 = hashlib.sha256(diff_content.encode("utf-8")).hexdigest()

    added = list(getattr(delta, "files_added", []) or [])
    modified = list(getattr(delta, "files_modified", []) or [])
    deleted = list(getattr(delta, "files_deleted", []) or [])

    try:
        for rel_path in sorted(set(added) | set(modified)):
            if not _is_safe_relative_path(repo_root, view_root, rel_path):
                view.files_refused.append(rel_path)
                continue
            source = repo_root / rel_path
            # A symlink in the delta is copied as a link, never followed out of
            # the repository into whatever it points at.
            if not source.is_symlink() and not source.is_file():
                view.files_refused.append(rel_path)
                continue
            destination = view_root / rel_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink() or destination.exists():
                destination.unlink()
            shutil.copy2(source, destination, follow_symlinks=False)
            view.files_materialized.append(rel_path)

        for rel_path in sorted(set(deleted)):
            if not _is_safe_relative_path(repo_root, view_root, rel_path):
                view.files_refused.append(rel_path)
                continue
            destination = view_root / rel_path
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
                view.files_removed.append(rel_path)

        view.linked_paths = _link_dependency_dirs(repo_root, view_root, linked_dependency_dirs)
    except OSError as exc:
        _remove_worktree(repo_root, view_root)
        raise VerificationViewError(
            f"Could not materialize task delta into verification view: {exc}"
        ) from exc

    return view


def _remove_worktree(repo_root: Path, view_root: Path) -> Optional[str]:
    """Removes a worktree and prunes its administrative record."""
    proc = run_git_in_repo(
        repo_root, ["worktree", "remove", "--force", str(view_root)], timeout=120
    )
    error: Optional[str] = None
    if proc.returncode != 0:
        error = (proc.stderr or proc.stdout or "").strip() or "git worktree remove failed"
        if view_root.exists():
            shutil.rmtree(view_root, ignore_errors=True)
    run_git_in_repo(repo_root, ["worktree", "prune"], timeout=60)
    if view_root.exists():
        return error or f"Verification view still present at {view_root}"
    return None


def destroy_verification_view(view: VerificationView) -> VerificationView:
    """Tears the view down and records a truthful cleanup status."""
    if view.cleanup_status == "removed":
        return view
    error = _remove_worktree(view.repo_root, view.path)
    if error:
        view.cleanup_status = "orphaned"
        view.cleanup_error = error
    else:
        view.cleanup_status = "removed"
        view.cleanup_error = None
    return view


@contextmanager
def verification_view(
    target_repo: Union[str, Path],
    baseline: Any,
    delta: Any,
    task_id: str,
    scratch_root: Optional[Union[str, Path]] = None,
    linked_dependency_dirs: Sequence[str] = DEFAULT_LINKED_DEPENDENCY_DIRS,
) -> Iterator[VerificationView]:
    """Yields a sanitized verification view and always tears it down."""
    view = build_verification_view(
        target_repo=target_repo,
        baseline=baseline,
        delta=delta,
        task_id=task_id,
        scratch_root=scratch_root,
        linked_dependency_dirs=linked_dependency_dirs,
    )
    try:
        yield view
    finally:
        destroy_verification_view(view)

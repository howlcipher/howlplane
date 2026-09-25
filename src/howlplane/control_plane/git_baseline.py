#!/usr/bin/env python3
"""
git_baseline.py

Captures pre-implementation Git repository baselines and computes
task-attributable repository deltas, distinguishing pre-existing modifications
from changes produced during agent implementation.
"""

import base64
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from howlplane.control_plane.git_env import run_git_in_repo
from howlplane.control_plane.task_spec import DataClassSerializationMixin

GIT_BASELINE_SCHEMA_VERSION = "howlplane.git_baseline/v1"
GIT_DELTA_SCHEMA_VERSION = "howlplane.git_delta/v1"


@dataclass
class GitBaseline(DataClassSerializationMixin):
    """Snapshot of repository state before agent implementation begins."""

    repo_root: str
    initial_commit_sha: str
    status_porcelain: str = ""
    pre_existing_modified: List[str] = field(default_factory=list)
    pre_existing_untracked: List[str] = field(default_factory=list)
    # Directories that existed before the attempt. Task-created directories that
    # are left empty after rollback must be removed, because git does not track
    # directories and an empty tree can still pollute a later baseline/delta view.
    pre_existing_directories: List[str] = field(default_factory=list)
    # Byte snapshots of pre-existing modified/untracked files so a failed
    # implementation attempt can be rolled back without losing user work.
    # Only files below the configured size limit are retained.
    pre_existing_snapshots: Dict[str, bytes] = field(default_factory=dict, repr=False)
    snapshot_size_limit_bytes: int = 1024 * 1024
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema: str = GIT_BASELINE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["pre_existing_snapshots"] = {
            k: base64.b64encode(v).decode("ascii")
            for k, v in self.pre_existing_snapshots.items()
        }
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GitBaseline":
        d = dict(data)
        raw_snapshots = d.pop("pre_existing_snapshots", {}) or {}
        snapshots: Dict[str, bytes] = {}
        for k, v in raw_snapshots.items():
            if isinstance(v, str):
                snapshots[k] = base64.b64decode(v)
            elif isinstance(v, bytes):
                snapshots[k] = v
        valid_fields = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(pre_existing_snapshots=snapshots, **filtered)


@dataclass
class RepositoryDelta(DataClassSerializationMixin):
    """Actual repository changes attributable to the task execution."""

    files_added: List[str] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    files_deleted: List[str] = field(default_factory=list)
    diff_content: str = ""
    insertions: int = 0
    deletions: int = 0
    is_empty: bool = True
    pre_existing_excluded: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema: str = GIT_DELTA_SCHEMA_VERSION

    def to_event_metadata(self) -> Dict[str, Any]:
        return {
            "files_added": self.files_added,
            "files_modified": self.files_modified,
            "files_deleted": self.files_deleted,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "files_changed": len(self.files_modified) + len(self.files_added),
        }


def _run_git_cmd(repo_root: Union[str, Path], args: List[str]) -> subprocess.CompletedProcess:
    """Executes a git command deterministically without shell=True.

    An inherited GIT_DIR overrides `git -C`, so the environment is sanitized
    before every invocation (see git_env.GIT_REPOSITORY_SELECTION_ENV_VARS).
    """
    return run_git_in_repo(repo_root, args)


def is_internal_control_plane_path(path: str) -> bool:
    """Returns True if the path is internal control plane metadata (e.g. .task_runs, .git)."""
    norm = path.strip().replace("\\", "/")
    if norm.endswith("/"):
        norm = norm[:-1]
    return norm in (".task_runs", ".git", ".howlchangeops") or norm.startswith((".task_runs/", ".git/", ".howlchangeops/"))


def _parse_porcelain_lines(status_text: str) -> Tuple[Set[str], Set[str], Set[str], Set[str]]:
    """Extracts (untracked, modified, deleted, added) file sets from git status --porcelain."""
    untracked, modified, deleted, added = set(), set(), set(), set()
    for line in status_text.splitlines():
        if not line.strip():
            continue
        code, path_part = line[:2], line[3:].strip()
        source_path: Optional[str] = None
        if " -> " in path_part:
            source_part, dest_part = path_part.split(" -> ", 1)
            source_path = source_part.strip()
            f_path = dest_part.strip()
        else:
            f_path = path_part

        # A staged rename ("R  old -> new") carries no D and no A, so it used to
        # fall through to `modified` carrying only the destination -- the source
        # file's disappearance was dropped entirely. A verification view built
        # from that delta then held both the old and the new path, which is the
        # contamination the view exists to prevent: the archive rename that took
        # howlframe's go_production from 291 to 84 would have measured 291.
        # A copy ("C") names a source too, but leaves it in place, so only the
        # destination is new.
        index_code = code[0]
        if index_code in ("R", "C") and source_path:
            if not is_internal_control_plane_path(f_path):
                added.add(f_path)
            if index_code == "R" and not is_internal_control_plane_path(source_path):
                deleted.add(source_path)
            continue

        if is_internal_control_plane_path(f_path):
            continue
        if code.startswith("??"):
            untracked.add(f_path)
        elif "D" in code:
            deleted.add(f_path)
        elif "A" in code:
            added.add(f_path)
        else:
            modified.add(f_path)
    return untracked, modified, deleted, added


@dataclass(frozen=True)
class WorkingTreeStatus:
    """Whether a repository's working tree carries changes someone else owns.

    `capture_baseline` records dirt so a task can be held responsible only for
    what it added; this answers the different question of whether it is safe to
    *begin* at all. Both read the same porcelain, so they agree on what counts.

    `git status --porcelain` is deliberately invoked without `--ignored`, so
    ignored runtime metadata is invisible here by git's own default rather than
    by a list this module would have to maintain. Control-plane state
    (`.task_runs`, `.howlchangeops`) is excluded by
    `is_internal_control_plane_path`, since that is the control plane's own
    bookkeeping and not a person's unfinished work.
    """

    repo_root: str
    is_clean: bool
    modified: List[str] = field(default_factory=list)
    added: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    untracked: List[str] = field(default_factory=list)

    def all_paths(self) -> List[str]:
        return sorted(set(self.modified) | set(self.added) | set(self.deleted) | set(self.untracked))

    def describe(self) -> str:
        """Human-readable summary naming what is dirty and how."""
        groups = (
            ("modified", self.modified),
            ("staged", self.added),
            ("deleted", self.deleted),
            ("untracked", self.untracked),
        )
        return "; ".join(
            f"{label}: {', '.join(sorted(paths))}" for label, paths in groups if paths
        ) or "clean"


def describe_working_tree(
    repo_dir: Union[str, Path],
    extra_internal_prefixes: Sequence[str] = (),
) -> WorkingTreeStatus:
    """Reports the working-tree state of `repo_dir` for a fail-closed preflight.

    `extra_internal_prefixes` names additional control-plane-owned paths to
    ignore -- a campaign directory configured to live inside the target repo is
    the engine's own state, not a change it should refuse to start over.
    """
    root = Path(repo_dir).resolve()
    status_out = _run_git_cmd(root, ["status", "--porcelain"]).stdout or ""
    untracked, modified, deleted, added = _parse_porcelain_lines(status_out)

    if extra_internal_prefixes:
        def owned_by_control_plane(path: str) -> bool:
            norm = path.strip().replace("\\", "/").rstrip("/")
            return any(
                norm == prefix.rstrip("/") or norm.startswith(prefix.rstrip("/") + "/")
                for prefix in extra_internal_prefixes
            )

        def prune(paths: Iterable[str]) -> Set[str]:
            return {p for p in paths if not owned_by_control_plane(p)}

        untracked, modified, deleted, added = (
            prune(untracked), prune(modified), prune(deleted), prune(added),
        )

    return WorkingTreeStatus(
        repo_root=str(root),
        is_clean=not (untracked or modified or deleted or added),
        modified=sorted(modified),
        added=sorted(added),
        deleted=sorted(deleted),
        untracked=sorted(untracked),
    )


def _snapshot_file(path: Path, size_limit_bytes: int) -> Optional[bytes]:
    """Returns file contents if the file exists and is within the size limit."""
    try:
        if path.is_file() and path.stat().st_size <= size_limit_bytes:
            return path.read_bytes()
    except OSError:
        pass
    return None


# Paths the control plane owns and which must never be rolled back or counted
# as user-created repository state.
_INTERNAL_PREFIXES = (".git", ".task_runs", ".howlchangeops")


def _is_internal_directory_path(rel_dir: str) -> bool:
    """True for directories that belong to the control plane, not the project."""
    norm = rel_dir.strip().replace("\\", "/").rstrip("/")
    if norm == ".":
        return False
    if norm in _INTERNAL_PREFIXES:
        return True
    return any(norm.startswith(prefix + "/") for prefix in _INTERNAL_PREFIXES)


def _collect_existing_directories(repo_root: Path) -> Set[str]:
    """All project directories that exist before an attempt, excluding control-plane state."""
    existing: Set[str] = set()
    root_str = str(repo_root)
    for dirpath, dirnames, _ in os.walk(root_str, topdown=True, followlinks=False):
        rel = Path(dirpath).relative_to(repo_root).as_posix() if dirpath != root_str else "."
        if _is_internal_directory_path(rel):
            dirnames[:] = []
            continue
        if rel != ".":
            existing.add(rel)
    return existing


def _is_within_repo(repo_root: Path, candidate: Path) -> bool:
    """Returns True if candidate resolves to a path inside repo_root."""
    try:
        return str(candidate.resolve()).startswith(str(repo_root.resolve()))
    except OSError:
        return False


def _safe_path_within_repo(repo_root: Path, rel_path: str) -> Optional[Path]:
    """Return an absolute Path for rel_path that is guaranteed to live inside repo_root.

    Resolves the parent directory (so symlinks in parents are not followed) and
    then joins the final name, so a symlink target pointing outside the repo
    cannot escape the repository boundary.
    """
    rel_path = rel_path.strip().replace("\\", "/")
    if is_internal_control_plane_path(rel_path):
        return None
    if not rel_path or rel_path == ".":
        return repo_root.resolve()
    parts = rel_path.split("/")
    if ".." in parts or any(p.startswith("/") for p in parts):
        return None
    parent_rel = "/".join(parts[:-1]) if len(parts) > 1 else "."
    try:
        parent_abs = (repo_root / parent_rel).resolve()
    except OSError:
        return None
    try:
        repo_root_resolved = repo_root.resolve()
    except OSError:
        return None
    if repo_root_resolved not in parent_abs.parents and parent_abs != repo_root_resolved:
        return None
    candidate = parent_abs / parts[-1]
    # Final check: the containing directory must be inside the repo. We do not
    # resolve the candidate itself because it may be a symlink we are about to
    # remove, but we must never allow a name that climbs above the root.
    if repo_root_resolved not in candidate.parents and candidate != repo_root_resolved:
        return None
    return candidate


def capture_baseline(repo_dir: Union[str, Path], snapshot_size_limit_bytes: int = 1024 * 1024) -> GitBaseline:
    """Captures the initial state of the repository prior to task execution."""
    root = Path(repo_dir).resolve()
    sha_proc = _run_git_cmd(root, ["rev-parse", "HEAD"])
    sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else "HEAD_UNKNOWN"

    status_out = _run_git_cmd(root, ["status", "--porcelain"]).stdout or ""
    untracked, modified, _, _ = _parse_porcelain_lines(status_out)

    snapshots: Dict[str, bytes] = {}
    for rel_path in modified | untracked:
        abs_path = root / rel_path
        snap = _snapshot_file(abs_path, snapshot_size_limit_bytes)
        if snap is not None:
            snapshots[rel_path] = snap

    existing_directories = _collect_existing_directories(root)

    return GitBaseline(
        repo_root=str(root),
        initial_commit_sha=sha,
        status_porcelain=status_out,
        pre_existing_modified=sorted(list(modified)),
        pre_existing_untracked=sorted(list(untracked)),
        pre_existing_directories=sorted(existing_directories),
        pre_existing_snapshots=snapshots,
        snapshot_size_limit_bytes=snapshot_size_limit_bytes,
    )


def is_baseline_restored(
    repo_dir: Union[str, Path],
    baseline: GitBaseline,
) -> Tuple[bool, Optional[str]]:
    """Verify the repository actually reflects the captured baseline.

    Checks HEAD has not drifted, no task-attributable delta remains, and every
    pre-existing file whose snapshot was retained still matches that snapshot.
    Does not require metadata equality for legitimate pre-existing state that
    changed during the attempt (e.g. log files), only that the repository is back
    to a state attributable entirely to the baseline.
    """
    root = Path(repo_dir).resolve()

    head_proc = _run_git_cmd(root, ["rev-parse", "HEAD"])
    if head_proc.returncode != 0:
        return False, "cannot read HEAD"
    current_head = head_proc.stdout.strip()
    if current_head != baseline.initial_commit_sha:
        return False, f"HEAD drifted: expected {baseline.initial_commit_sha}, got {current_head}"

    delta = capture_delta(root, baseline)
    if not delta.is_empty:
        residual = sorted(
            set(delta.files_added + delta.files_modified + delta.files_deleted)
        )
        return False, f"task-attributable delta remains: {residual}"

    snapshots = baseline.pre_existing_snapshots or {}
    for rel_path, expected_bytes in snapshots.items():
        abs_path = _safe_path_within_repo(root, rel_path)
        if abs_path is None:
            return False, f"pre-existing path {rel_path} escaped repo boundary"
        try:
            if not abs_path.is_file() or abs_path.read_bytes() != expected_bytes:
                return False, f"pre-existing file {rel_path} does not match baseline snapshot"
        except OSError as exc:
            return False, f"pre-existing file {rel_path} cannot be verified: {exc}"

    return True, None


def _generate_untracked_diff(repo_root: Path, rel_path: str) -> str:
    """Generates synthetic unified diff for newly created untracked file."""
    f = repo_root / rel_path
    if not f.is_file():
        return ""
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    hdr = [f"diff --git a/{rel_path} b/{rel_path}", "new file mode 100644", "--- /dev/null", f"+++ b/{rel_path}", f"@@ -0,0 +1,{len(lines)} @@"]
    return "\n".join(hdr + [f"+{l}" for l in lines]) + "\n"


def capture_delta(repo_dir: Union[str, Path], baseline: GitBaseline) -> RepositoryDelta:
    """Computes task-attributable repository delta, isolating pre-existing dirt."""
    root = Path(repo_dir).resolve()
    status_out = _run_git_cmd(root, ["status", "--porcelain"]).stdout or ""
    cur_untracked, cur_modified, cur_deleted, cur_added = _parse_porcelain_lines(status_out)

    pre_untracked = set(baseline.pre_existing_untracked)
    pre_modified = set(baseline.pre_existing_modified)

    task_added = sorted(list((cur_untracked - pre_untracked) | (cur_added - pre_modified)))
    task_deleted = sorted(list(cur_deleted - pre_modified))
    task_modified = sorted(list(cur_modified - pre_modified))
    excluded = sorted(list((cur_untracked & pre_untracked) | (cur_modified & pre_modified)))

    diffs: List[str] = []
    tracked_changed = sorted(list(set(task_modified + task_deleted + [f for f in task_added if f in cur_added])))
    if tracked_changed:
        d_proc = _run_git_cmd(root, ["diff", "HEAD", "--"] + tracked_changed)
        if d_proc.returncode == 0 and d_proc.stdout.strip():
            diffs.append(d_proc.stdout.strip())

    for nf in task_added:
        if nf in cur_untracked:
            ch = _generate_untracked_diff(root, nf)
            if ch.strip():
                diffs.append(ch.strip())

    full_diff = "\n\n".join(diffs) if diffs else ""
    ins = sum(1 for l in full_diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    dels = sum(1 for l in full_diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    # Each hunk was stripped before joining, so the assembled patch ends without
    # a newline and `git apply` rejects the whole file as corrupt at its last
    # line. Preserved evidence has to be replayable -- a candidate that cannot
    # be applied cannot be reviewed or verified (HOWLFRAM-SLOPFIX-05). Counts
    # are taken above so this stays purely a serialization fix.
    if full_diff:
        full_diff += "\n"

    return RepositoryDelta(
        files_added=task_added,
        files_modified=task_modified,
        files_deleted=task_deleted,
        diff_content=full_diff,
        insertions=ins,
        deletions=dels,
        is_empty=not bool(task_added or task_modified or task_deleted or full_diff.strip()),
        pre_existing_excluded=excluded,
    )


def _restore_pre_existing_files(
    root: Path,
    baseline: GitBaseline,
    delta: RepositoryDelta,
) -> None:
    """Put back any pre-existing user files the attempt touched, using snapshots or git."""
    snapshots = baseline.pre_existing_snapshots or {}
    pre_modified = set(baseline.pre_existing_modified)
    pre_untracked = set(baseline.pre_existing_untracked)

    touched_pre_existing: Set[str] = set()
    for rel_path in pre_modified | pre_untracked:
        abs_path = _safe_path_within_repo(root, rel_path)
        if abs_path is None:
            continue
        snap = snapshots.get(rel_path)
        if not abs_path.is_file():
            if snap is not None:
                touched_pre_existing.add(rel_path)
            continue
        if snap is None:
            continue
        try:
            if abs_path.read_bytes() != snap:
                touched_pre_existing.add(rel_path)
        except OSError:
            pass

    for rel_path in touched_pre_existing:
        abs_path = _safe_path_within_repo(root, rel_path)
        if abs_path is None:
            continue
        snap = snapshots[rel_path]
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(snap)

    for rel_path in set(delta.files_modified + delta.files_deleted):
        if rel_path in pre_modified:
            continue
        abs_path = _safe_path_within_repo(root, rel_path)
        if abs_path is None:
            continue
        _run_git_cmd(root, ["checkout", "--", rel_path])


def _remove_task_added_paths(root: Path, delta: RepositoryDelta, baseline: GitBaseline) -> Set[str]:
    """Delete files, symlinks and task-created directory contents the attempt added.

    Git status reports entire untracked directory trees as a single path ending in
    '/'. Such a path is never deleted wholesale; instead, every file/symlink
    inside it that was not pre-existing is removed, and the now-empty directory
    cleanup pass removes the shell.
    """
    pre_untracked = set(baseline.pre_existing_untracked or [])
    pre_existing_dirs = set(baseline.pre_existing_directories or [])
    touched_dirs: Set[str] = set()

    for rel_path in delta.files_added:
        if rel_path in pre_untracked:
            continue
        abs_path = _safe_path_within_repo(root, rel_path)
        if abs_path is None:
            continue

        parent_rel = str(Path(rel_path).parent).replace("\\", "/")
        if parent_rel and parent_rel != ".":
            touched_dirs.add(parent_rel)

        if abs_path.is_dir() and not abs_path.is_symlink():
            # Remove every file/symlink inside this untracked directory that was
            # not pre-existing. Empty directory shells are removed afterwards.
            for entry in sorted(abs_path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if not (entry.is_file() or entry.is_symlink()):
                    continue
                entry_rel = entry.relative_to(root).as_posix()
                if entry_rel in pre_untracked:
                    continue
                safe_entry = _safe_path_within_repo(root, entry_rel)
                if safe_entry is None:
                    continue
                try:
                    safe_entry.unlink()
                except OSError:
                    pass
        else:
            # Remove regular files and symlinks (including broken symlinks and
            # symlinks to directories) without following the link target.
            try:
                if abs_path.is_file() or abs_path.is_symlink():
                    abs_path.unlink()
            except OSError:
                pass

    # Also remove any now-empty directory that was created by the attempt. The
    # full-repo scan is bounded to project directories and protects pre-existing
    # directories.
    _remove_empty_task_created_dirs(root, baseline, touched_dirs)

    return touched_dirs


def _remove_empty_task_created_dirs(
    root: Path,
    baseline: GitBaseline,
    touched_dirs: Set[str],
) -> None:
    """Remove directories created by the attempt once they are empty.

    A directory is preserved if it existed in the baseline, if it is part of
    control-plane state, or if it still contains files. Removal stops at the
    repository root.
    """
    protected: Set[str] = set(baseline.pre_existing_directories or [])
    candidates: List[str] = []

    root_str = str(root)
    for dirpath, dirnames, filenames in os.walk(root_str, topdown=True, followlinks=False):
        rel = Path(dirpath).relative_to(root).as_posix() if dirpath != root_str else "."
        if _is_internal_directory_path(rel):
            dirnames[:] = []
            continue
        if rel == ".":
            # Always recurse from the repository root, but do not add the root itself.
            dirnames[:] = [d for d in dirnames if not _is_internal_directory_path(d)]
            continue
        if rel in protected:
            continue
        # Prune known control-plane sub-trees.
        dirnames[:] = [d for d in dirnames if not _is_internal_directory_path(
            (Path(rel) / d).as_posix()
        )]
        candidates.append(rel)

    # Remove deepest directories first so parents can become empty in turn.
    for rel in reversed(candidates):
        p = root / rel
        try:
            if p.exists() and p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass


def restore_repository_to_baseline(
    repo_dir: Union[str, Path],
    baseline: GitBaseline,
    attempt_delta: Optional[RepositoryDelta] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Restores the repository to the pre-attempt baseline, removing only changes
    attributable to the failed attempt while preserving pre-existing user work.

    Removes task-created files, symlinks and now-empty directory trees, restores
    pre-existing modified/untracked files from byte snapshots, and reverts tracked
    files the attempt modified or deleted. The operation is idempotent: a
    second call against a clean baseline is a no-op.

    Returns (success, reason).
    """
    root = Path(repo_dir).resolve()

    head_proc = _run_git_cmd(root, ["rev-parse", "HEAD"])
    if head_proc.returncode != 0 or head_proc.stdout.strip() != baseline.initial_commit_sha:
        return False, (
            f"HEAD drifted from baseline: expected {baseline.initial_commit_sha}, "
            f"got {head_proc.stdout.strip() if head_proc.returncode == 0 else 'unknown'}"
        )

    delta = attempt_delta or capture_delta(root, baseline)

    _restore_pre_existing_files(root, baseline, delta)
    _remove_task_added_paths(root, delta, baseline)

    remaining = capture_delta(root, baseline)
    if not remaining.is_empty:
        pre_modified = set(baseline.pre_existing_modified)
        pre_untracked = set(baseline.pre_existing_untracked)
        remaining_files = set(
            remaining.files_added + remaining.files_modified + remaining.files_deleted
        )
        residual = remaining_files - pre_modified - pre_untracked
        if residual:
            return False, f"Rollback verification failed: residual task files {sorted(residual)}"

    return True, None

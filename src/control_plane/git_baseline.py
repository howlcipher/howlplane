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
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from src.control_plane.git_env import run_git_in_repo
from src.control_plane.task_spec import DataClassSerializationMixin

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
        f_path = path_part.split(" -> ")[1].strip() if " -> " in path_part else path_part
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


def _snapshot_file(path: Path, size_limit_bytes: int) -> Optional[bytes]:
    """Returns file contents if the file exists and is within the size limit."""
    try:
        if path.is_file() and path.stat().st_size <= size_limit_bytes:
            return path.read_bytes()
    except OSError:
        pass
    return None


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

    return GitBaseline(
        repo_root=str(root),
        initial_commit_sha=sha,
        status_porcelain=status_out,
        pre_existing_modified=sorted(list(modified)),
        pre_existing_untracked=sorted(list(untracked)),
        pre_existing_snapshots=snapshots,
        snapshot_size_limit_bytes=snapshot_size_limit_bytes,
    )


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


def _is_within_repo(repo_root: Path, candidate: Path) -> bool:
    """Returns True if candidate resolves to a path inside repo_root."""
    try:
        return str(candidate.resolve()).startswith(str(repo_root.resolve()))
    except OSError:
        return False


def restore_repository_to_baseline(
    repo_dir: Union[str, Path],
    baseline: GitBaseline,
    attempt_delta: Optional[RepositoryDelta] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Restores the repository to the pre-attempt baseline, removing only changes
    attributable to the failed attempt while preserving pre-existing user work.
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
    snapshots = baseline.pre_existing_snapshots or {}
    pre_modified = set(baseline.pre_existing_modified)
    pre_untracked = set(baseline.pre_existing_untracked)

    # Identify pre-existing files the attempt may have touched.
    touched_pre_existing: Set[str] = set()
    for rel_path in pre_modified | pre_untracked:
        abs_path = root / rel_path
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

    # Restore pre-existing files from snapshots first.
    for rel_path in touched_pre_existing:
        abs_path = root / rel_path
        snap = snapshots[rel_path]
        if not _is_within_repo(root, abs_path):
            continue
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(snap)

    # Restore tracked files the attempt modified or deleted.
    for rel_path in set(delta.files_modified + delta.files_deleted):
        if rel_path in pre_modified:
            continue
        abs_path = root / rel_path
        if not _is_within_repo(root, abs_path):
            continue
        _run_git_cmd(root, ["checkout", "--", rel_path])

    # Remove files the attempt added.
    for rel_path in delta.files_added:
        if rel_path in pre_untracked:
            continue
        abs_path = root / rel_path
        if not _is_within_repo(root, abs_path):
            continue
        try:
            if abs_path.is_file():
                abs_path.unlink()
        except OSError:
            pass

    # Verify rollback left only pre-existing changes.
    remaining = capture_delta(root, baseline)
    if not remaining.is_empty:
        remaining_files = set(
            remaining.files_added + remaining.files_modified + remaining.files_deleted
        )
        residual = remaining_files - pre_modified - pre_untracked
        if residual:
            return False, f"Rollback verification failed: residual task files {sorted(residual)}"

    return True, None

#!/usr/bin/env python3
"""
git_baseline.py

Captures pre-implementation Git repository baselines and computes
task-attributable repository deltas, distinguishing pre-existing modifications
from changes produced during agent implementation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Set, Tuple, Union

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
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema: str = GIT_BASELINE_SCHEMA_VERSION


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
    """Executes a git command deterministically without shell=True."""
    return subprocess.run(
        ["git", "-C", str(repo_root)] + args,
        capture_output=True,
        text=True,
        check=False,
    )


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


def capture_baseline(repo_dir: Union[str, Path]) -> GitBaseline:
    """Captures the initial state of the repository prior to task execution."""
    root = Path(repo_dir).resolve()
    sha_proc = _run_git_cmd(root, ["rev-parse", "HEAD"])
    sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else "HEAD_UNKNOWN"

    status_out = _run_git_cmd(root, ["status", "--porcelain"]).stdout or ""
    untracked, modified, _, _ = _parse_porcelain_lines(status_out)

    return GitBaseline(
        repo_root=str(root),
        initial_commit_sha=sha,
        status_porcelain=status_out,
        pre_existing_modified=sorted(list(modified)),
        pre_existing_untracked=sorted(list(untracked)),
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

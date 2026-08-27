#!/usr/bin/env python3
"""
locking.py

Lightweight filesystem repository and task lifecycle locks for HowlPlane.
Prevents concurrent mutation workflows from destroying git attribution or racing reviews,
and protects task lifecycle operations against concurrent invocation.
Provides safe stale lock detection without requiring Redis, daemons, or databases.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import errno
import os
from pathlib import Path
import socket
import time
from typing import Any, Dict, Optional, Tuple, Type, Union

from src.control_plane.atomic_io import safe_load_json
from src.control_plane.task_spec import DataClassSerializationMixin

LOCK_SCHEMA_VERSION = "howlplane.lock/v1"
LOCK_RECLAMATION_SCHEMA_VERSION = "howlplane.lock_reclamation/v1"


class LockError(RuntimeError):
    """Base exception for locking errors."""
    pass


class RepositoryLockedError(LockError):
    """Raised when repository mutation is blocked because another active task holds the repository lock."""
    pass


class TaskLockedError(LockError):
    """Raised when task lifecycle mutation is blocked because another operation holds the task lock."""
    pass


@dataclass
class LockMetadata(DataClassSerializationMixin):
    """Structured metadata stored inside a lock file."""

    task_id: str
    pid: int
    hostname: str
    lock_type: str  # "repository_mutation" | "task_run"
    operation: str = "work"
    command: str = "ai"
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    process_create_time: float = 0.0
    schema: str = LOCK_SCHEMA_VERSION


def get_process_create_time(pid: int) -> float:
    """Extracts process creation timestamp on Linux /proc or falls back to current time."""
    try:
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.is_file():
            return stat_path.stat().st_mtime
    except Exception:
        pass
    return time.time()


class LockOwnerState(str, Enum):
    """What can actually be established about a lock owner from this machine.

    The middle state is the one that matters. Previously anything not provably
    dead was reported as alive, which let a lock written on another host block
    the documented recovery path forever while claiming to be "held by active
    process" -- a statement about a PID whose liveness had never been checked
    (HOWLFRAM-SLOPFIX-05).
    """

    ACTIVE = "ACTIVE"        # owner is running: fail closed, never steal
    STALE = "STALE"          # owner is provably gone: safe to reclaim
    AMBIGUOUS = "AMBIGUOUS"  # cannot be established here: requires a human


def classify_lock_owner(
    pid: int,
    hostname: str,
    expected_create_time: Optional[float] = None,
) -> Tuple[LockOwnerState, str]:
    """Classifies a lock owner as ACTIVE, STALE, or AMBIGUOUS, with a reason."""
    current_host = socket.gethostname()
    if hostname != current_host:
        return (
            LockOwnerState.AMBIGUOUS,
            f"Lock was written on host '{hostname}' (this host is "
            f"'{current_host}'); liveness of PID {pid} cannot be established "
            f"from here",
        )

    if pid <= 0:
        return LockOwnerState.STALE, "Invalid PID <= 0"

    try:
        os.kill(pid, 0)
    except OSError as err:
        if err.errno == errno.ESRCH:
            return (
                LockOwnerState.STALE,
                f"Process PID {pid} is no longer running (ESRCH)",
            )
        if err.errno == errno.EPERM:
            return (
                LockOwnerState.AMBIGUOUS,
                f"PID {pid} exists but runs under another user account, so it "
                f"cannot be confirmed as this task's owner",
            )
        return (
            LockOwnerState.AMBIGUOUS,
            f"Process check returned unexpected OSError {err}",
        )

    if expected_create_time and expected_create_time > 0:
        actual_create_time = get_process_create_time(pid)
        if abs(actual_create_time - expected_create_time) > 10.0:
            return (
                LockOwnerState.STALE,
                f"PID {pid} was recycled by operating system (mismatched start time)",
            )

    return LockOwnerState.ACTIVE, f"Process PID {pid} is actively running"


def is_process_alive(
    pid: int,
    hostname: str,
    expected_create_time: Optional[float] = None,
) -> Tuple[bool, str]:
    """Reports whether a lock owner must be treated as running.

    Kept as the single liveness predicate for callers that only need a boolean,
    and built on classify_lock_owner so there is one authority. AMBIGUOUS counts
    as alive here: automatic paths must never assume an unverifiable owner is
    gone. Reclaiming one is a deliberate human act -- see reclaim_lock.
    """
    state, reason = classify_lock_owner(pid, hostname, expected_create_time)
    return state is not LockOwnerState.STALE, reason


@dataclass
class LockReclamation(DataClassSerializationMixin):
    """Record of a lock reclaimed by explicit human action."""

    task_id: str
    lock_path: str
    owner_state: str
    reason: str
    owner_pid: int
    owner_hostname: str
    owner_command: str
    owner_started_at: str
    reclaimed_by_pid: int
    reclaimed_by_hostname: str
    reclaimed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    schema: str = LOCK_RECLAMATION_SCHEMA_VERSION


def reclaim_lock(lock_path: Union[str, Path]) -> LockReclamation:
    """Reclaims a lock whose owner is gone or unverifiable, for a human caller.

    Fails closed on an ACTIVE owner: a running process is never displaced, no
    matter who asks. STALE and AMBIGUOUS owners can be reclaimed, because the
    first is provably gone and the second is precisely the case that had no
    recovery path at all -- `ai resume` recommended resuming a run whose lock it
    could never take (HOWLFRAM-SLOPFIX-05). Reclaiming an AMBIGUOUS lock stays a
    deliberate act by a person, never something an automatic path does.
    """
    path = Path(lock_path)
    if not path.exists():
        raise LockError(f"No lock file at '{path}'.")
    try:
        existing = LockMetadata.from_dict(safe_load_json(path))
    except Exception as err:
        raise LockError(f"Lock file at '{path}' is unreadable: {err}") from err

    state, reason = classify_lock_owner(
        existing.pid, existing.hostname, existing.process_create_time
    )
    if state is LockOwnerState.ACTIVE:
        raise LockError(
            f"Refusing to reclaim: lock for task '{existing.task_id}' is held by "
            f"active process PID {existing.pid} ({existing.command}). {reason}. "
            f"Stop that process first, or use `ai cancel {existing.task_id}`."
        )

    record = LockReclamation(
        task_id=existing.task_id,
        lock_path=str(path),
        owner_state=state.value,
        reason=reason,
        owner_pid=existing.pid,
        owner_hostname=existing.hostname,
        owner_command=existing.command,
        owner_started_at=existing.started_at,
        reclaimed_by_pid=os.getpid(),
        reclaimed_by_hostname=socket.gethostname(),
    )
    path.unlink()
    return record


def get_repo_lock_path(repo_root: Union[str, Path]) -> Path:
    """Determines the canonical lock path for repository mutations."""
    root = Path(repo_root).resolve()
    git_dir = root / ".git"
    if git_dir.is_dir():
        return git_dir / "howlplane.lock"
    task_runs = root / ".task_runs"
    task_runs.mkdir(parents=True, exist_ok=True)
    return task_runs / ".repo.lock"


def get_task_lock_path(repo_root: Union[str, Path], task_id: str) -> Path:
    """Determines the canonical lock path for a specific task lifecycle run."""
    root = Path(repo_root).resolve()
    task_dir = root / ".task_runs" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir / ".task.lock"


class _BaseFileLock:
    """Base mutual-exclusion file lock with stale process reclamation."""

    lock_type = "generic_lock"
    error_cls: Type[LockError] = LockError

    def __init__(
        self,
        repo_root: Union[str, Path],
        task_id: str,
        lock_path: Path,
        command: str,
        operation: str,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.task_id = task_id
        self.lock_path = lock_path
        self.command = command
        self.operation = operation
        self._acquired = False

    def acquire(self) -> bool:
        my_pid = os.getpid()
        my_host = socket.gethostname()
        my_ctime = get_process_create_time(my_pid)

        if self.lock_path.exists():
            try:
                data = safe_load_json(self.lock_path)
                existing = LockMetadata.from_dict(data)
            except Exception:
                existing = None

            if existing:
                if existing.pid == my_pid and existing.hostname == my_host:
                    if self._acquired:
                        return True
                    raise self.error_cls(
                        f"{self.lock_type.replace('_', ' ').title()} lock already held on task '{existing.task_id}'."
                    )

                state, reason = classify_lock_owner(
                    existing.pid, existing.hostname, existing.process_create_time
                )
                label = self.lock_type.replace("_", " ").title()
                if state is LockOwnerState.ACTIVE:
                    raise self.error_cls(
                        f"{label} lock held by active process "
                        f"PID {existing.pid} ({existing.command}) for task '{existing.task_id}' "
                        f"started at {existing.started_at}. Concurrent operation blocked."
                    )
                if state is LockOwnerState.AMBIGUOUS:
                    # Never silently steal a lock we cannot prove is dead, and
                    # never claim its owner is active when we never checked.
                    raise self.error_cls(
                        f"{label} lock ownership is INDETERMINATE for task "
                        f"'{existing.task_id}': PID {existing.pid} ({existing.command}) "
                        f"on host '{existing.hostname}', started at {existing.started_at}. "
                        f"{reason}. If that run is definitely gone, reclaim the lock "
                        f"explicitly with `ai unlock {existing.task_id}`."
                    )
                # Provably gone: reclaim automatically, as before.
                try:
                    self.lock_path.unlink()
                except Exception:
                    pass
            else:
                try:
                    self.lock_path.unlink()
                except Exception:
                    pass

        meta = LockMetadata(
            task_id=self.task_id,
            pid=my_pid,
            hostname=my_host,
            lock_type=self.lock_type,
            operation=self.operation,
            command=self.command,
            process_create_time=my_ctime,
        )

        content = meta.to_json()
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                str(self.lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            self._acquired = True
            return True
        except FileExistsError:
            raise self.error_cls(
                f"Contention on '{self.lock_path}'. Another operation acquired the lock concurrently."
            )

    def release(self) -> None:
        if not self._acquired:
            return
        my_pid = os.getpid()
        my_host = socket.gethostname()
        if self.lock_path.exists():
            try:
                data = safe_load_json(self.lock_path)
                existing = LockMetadata.from_dict(data)
                if existing.pid == my_pid and existing.hostname == my_host:
                    self.lock_path.unlink()
            except Exception:
                try:
                    self.lock_path.unlink()
                except Exception:
                    pass
        self._acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class RepoLock(_BaseFileLock):
    """
    Mutual-exclusion lock for repository mutations.
    Ensures only one mutating task modifies a working tree at a time.
    """

    lock_type = "repository_mutation"
    error_cls = RepositoryLockedError

    def __init__(
        self,
        repo_root: Union[str, Path],
        task_id: str,
        command: str = "ai work --execute",
        operation: str = "repository_mutation",
    ):
        path = get_repo_lock_path(repo_root)
        super().__init__(repo_root, task_id, path, command, operation)


class TaskLock(_BaseFileLock):
    """
    Mutual-exclusion lock for a single task's lifecycle operations
    (approve, reject, resume, cancel, bounded execution).
    """

    lock_type = "task_run"
    error_cls = TaskLockedError

    def __init__(
        self,
        repo_root: Union[str, Path],
        task_id: str,
        operation: str = "resume",
        command: str = "ai resume",
    ):
        path = get_task_lock_path(repo_root, task_id)
        super().__init__(repo_root, task_id, path, command, operation)


class LocalInferenceBusyError(LockError):
    """Raised when a second local model inference is attempted while one is already running."""
    pass


def get_local_inference_lock_path(repo_root: Union[str, Path]) -> Path:
    """Canonical machine-wide lock path enforcing a single concurrent local inference."""
    root = Path(repo_root).resolve()
    task_runs = root / ".task_runs"
    task_runs.mkdir(parents=True, exist_ok=True)
    return task_runs / ".local_inference.lock"


class LocalInferenceLock(_BaseFileLock):
    """
    Machine-wide mutual-exclusion lock enforcing exactly one concurrent local
    (Ollama) model inference at a time, regardless of how many HowlPlane
    processes are running (#58 Phase 7). HowlPlane's own lock is authoritative
    and does not rely solely on Ollama's own internal queuing behavior.
    """

    lock_type = "local_inference"
    error_cls = LocalInferenceBusyError

    def __init__(
        self,
        repo_root: Union[str, Path],
        task_id: str,
        command: str = "ollama_local",
    ):
        path = get_local_inference_lock_path(repo_root)
        super().__init__(repo_root, task_id, path, command, "local_inference")

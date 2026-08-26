#!/usr/bin/env python3
"""
progress.py
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import os
from pathlib import Path
import sys
import threading
import time
from typing import Iterator, Optional, TextIO, Union

from src.control_plane.atomic_io import atomic_write_json
from src.control_plane.task_spec import DataClassSerializationMixin

PROGRESS_SCHEMA_VERSION = "howlplane.task_progress/v1"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0


class TaskPhase(str, Enum):
    PREPARING = "PREPARING"
    ROUTING = "ROUTING"
    IMPLEMENTING = "IMPLEMENTING"
    REVIEWING = "REVIEWING"
    REMEDIATING = "REMEDIATING"
    VERIFYING = "VERIFYING"
    AWAITING_AUTHORIZATION = "AWAITING_AUTHORIZATION"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskProgressState(str, Enum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    AWAITING_AUTHORIZATION = "AWAITING_AUTHORIZATION"


def _now_utc_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_elapsed(seconds: Union[int, float]) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def format_last_heartbeat(
    updated_at_iso: Optional[str],
    now_ts: Optional[float] = None,
) -> str:
    if not updated_at_iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(updated_at_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        curr = (
            now_ts
            if now_ts is not None
            else datetime.now(timezone.utc).timestamp()
        )
        sec = max(0, int(curr - dt.timestamp()))
        if sec < 60:
            return f"{sec}s ago"
        elif sec < 3600:
            return f"{sec // 60}m ago"
        return f"{sec // 3600}h ago"
    except Exception:
        return "unknown"


@dataclass
class TaskProgressRecord(DataClassSerializationMixin):
    task_id: str
    phase: str = TaskPhase.PREPARING.value
    resource_id: Optional[str] = None
    role: Optional[str] = None
    started_at: str = field(default_factory=_now_utc_str)
    phase_started_at: str = field(default_factory=_now_utc_str)
    updated_at: str = field(default_factory=_now_utc_str)
    elapsed_seconds: int = 0
    phase_elapsed_seconds: int = 0
    cycle: int = 0
    state: str = TaskProgressState.RUNNING.value
    details: Optional[str] = None
    pid: Optional[int] = None
    schema: str = PROGRESS_SCHEMA_VERSION


class TaskProgressTracker:
    def __init__(
        self,
        task_id: str,
        run_dir: Optional[Union[str, Path]] = None,
        stream: Optional[TextIO] = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        enabled: bool = True,
    ):
        self.task_id = task_id
        self.run_dir = Path(run_dir).resolve() if run_dir else None
        self.stream = stream if stream is not None else sys.stderr
        self.heartbeat_interval = max(0.0, float(heartbeat_interval))
        self.enabled = enabled

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ticker: Optional[threading.Thread] = None

        t = time.time()
        self._t_start = t
        self._t_phase = t

        stamp = _now_utc_str()
        self._record = TaskProgressRecord(
            task_id=task_id,
            started_at=stamp,
            phase_started_at=stamp,
            updated_at=stamp,
            pid=os.getpid(),
        )

    @property
    def _heartbeat_thread(self) -> Optional[threading.Thread]:
        return self._ticker

    @property
    def record(self) -> TaskProgressRecord:
        with self._lock:
            return TaskProgressRecord.from_dict(self._record.to_dict())

    def start(
        self,
        task_id: Optional[str] = None,
        run_dir: Optional[Union[str, Path]] = None,
        initial_phase: str = TaskPhase.PREPARING.value,
    ) -> None:
        with self._lock:
            if task_id:
                self.task_id = task_id
                self._record.task_id = task_id
            if run_dir:
                self.run_dir = Path(run_dir).resolve()

            self._t_start = time.time()
            self._t_phase = self._t_start
            stamp = _now_utc_str()
            self._record.started_at = stamp
            self._record.phase = initial_phase
            self._record.state = TaskProgressState.RUNNING.value
            self._record.pid = os.getpid()
            self._record.phase_started_at = stamp
            self._record.updated_at = stamp
            self._record.elapsed_seconds = 0
            self._record.phase_elapsed_seconds = 0
            self._flush_json()
            self._write_stream(f"[HowlPlane] Task {self.task_id} started")

    def transition(
        self, phase: Union[str, TaskPhase],
        resource_id: Optional[str] = None, role: Optional[str] = None,
        details: Optional[str] = None, cycle: int = 0,
    ) -> None:
        self._stop_ticker()
        p_name = phase.value if isinstance(phase, TaskPhase) else str(phase)
        with self._lock:
            now_t = time.time()
            self._t_phase = now_t
            stamp = _now_utc_str()

            self._record.phase = p_name
            self._record.resource_id = resource_id
            self._record.role = role
            self._record.details = details
            self._record.cycle = cycle
            self._record.phase_started_at = stamp
            self._record.updated_at = stamp
            self._record.elapsed_seconds = int(now_t - self._t_start)
            self._record.phase_elapsed_seconds = 0

            self._flush_json()

            parts = [f"[HowlPlane] {p_name}"]
            if resource_id:
                parts.append(resource_id)
            if details:
                parts.append(details)
            self._write_stream(" | ".join(parts))

    @contextmanager
    def operation(
        self, phase: Union[str, TaskPhase],
        resource_id: Optional[str] = None, role: Optional[str] = None,
        details: Optional[str] = None, cycle: int = 0,
        completion_message: Optional[str] = None,
        suppress_completion: bool = False,
    ) -> Iterator[None]:
        self.transition(phase=phase, resource_id=resource_id, role=role, details=details, cycle=cycle)
        self._start_ticker()
        t0 = time.time()
        try:
            yield
        finally:
            self._stop_ticker()
            dur = time.time() - t0
            p_str = phase.value if isinstance(phase, TaskPhase) else str(phase)
            with self._lock:
                now_t = time.time()
                self._record.elapsed_seconds = int(now_t - self._t_start)
                self._record.phase_elapsed_seconds = int(now_t - self._t_phase)
                self._record.updated_at = _now_utc_str()
                self._flush_json()

            if not suppress_completion:
                if completion_message:
                    self._write_stream(completion_message)
                elif p_str == TaskPhase.IMPLEMENTING.value and resource_id:
                    self._write_stream(
                        f"[HowlPlane] IMPLEMENTATION COMPLETE | {resource_id} | "
                        f"elapsed {format_elapsed(dur)}"
                    )

    def emit_failover(
        self,
        source_resource_id: str,
        target_resource_id: str,
        failure_class: Optional[str] = None,
    ) -> None:
        """Emits a concise provider failover event to the progress stream."""
        parts = [
            "[HowlPlane] FAILOVER",
            f"{source_resource_id} -> {target_resource_id}",
        ]
        if failure_class:
            parts.append(failure_class)
        self._write_stream(" | ".join(parts))

    def emit_implementation_failed(
        self,
        resource_id: str,
        reason: Optional[str] = None,
    ) -> None:
        """Emits explicit implementation-failure terminology for a failed attempt."""
        parts = ["[HowlPlane] IMPLEMENTATION FAILED", resource_id]
        if reason:
            parts.append(reason)
        self._write_stream(" | ".join(parts))

    def record_terminal(
        self,
        state: Union[str, TaskProgressState],
        phase: Optional[Union[str, TaskPhase]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        self._stop_ticker()
        s_val = state.value if isinstance(state, TaskProgressState) else str(state)
        p_val = (
            phase.value
            if isinstance(phase, TaskPhase)
            else str(phase)
            if phase is not None
            else {"COMPLETE": "COMPLETE", "FAILED": "FAILED", "CANCELLED": "CANCELLED", "AWAITING_AUTHORIZATION": "AWAITING_AUTHORIZATION"}.get(s_val, self._record.phase)
        )
        with self._lock:
            now_t = time.time()
            tot = int(now_t - self._t_start)
            ph = int(now_t - self._t_phase)

            self._record.state = s_val
            self._record.phase = p_val
            self._record.updated_at = _now_utc_str()
            self._record.elapsed_seconds = tot
            self._record.phase_elapsed_seconds = ph
            if error_message:
                self._record.details = error_message

            self._flush_json()
            self._write_stream(f"[HowlPlane] {s_val} | elapsed {format_elapsed(tot)}")

    def close(self) -> None:
        self._stop_ticker()

    def _start_ticker(self) -> None:
        if not self.enabled or self.heartbeat_interval <= 0.0:
            return
        self._stop_event.clear()
        self._ticker = threading.Thread(
            target=self._run_ticker,
            name=f"hp-beat-{self.task_id}",
            daemon=True,
        )
        self._ticker.start()

    def _stop_ticker(self) -> None:
        self._stop_event.set()
        t_ref = self._ticker
        if t_ref is not None and t_ref.is_alive() and threading.current_thread() != t_ref:
            t_ref.join(timeout=1.0)
        self._ticker = None

    def _run_ticker(self) -> None:
        while not self._stop_event.wait(timeout=self.heartbeat_interval):
            if self._stop_event.is_set():
                break
            with self._lock:
                now_t = time.time()
                tot_s = int(now_t - self._t_start)
                ph_s = int(now_t - self._t_phase)
                self._record.elapsed_seconds = tot_s
                self._record.phase_elapsed_seconds = ph_s
                self._record.updated_at = _now_utc_str()
                self._flush_json()

                p_str = self._record.phase
                r_str = self._record.resource_id or self._record.details or ""
                dur_str = format_elapsed(ph_s)

                tag = f"{p_str} | {r_str}" if r_str else p_str
                self._write_stream(f"[HowlPlane] {tag} | elapsed {dur_str} | still working")

    def _write_stream(self, text: str) -> None:
        if self.enabled and self.stream:
            try:
                self.stream.write(f"{text}\n")
                self.stream.flush()
            except Exception:
                pass

    def _flush_json(self) -> None:
        if not (self.enabled and self.run_dir):
            return
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self.run_dir / "progress.json", self._record.to_dict())
        except Exception:
            pass


@contextmanager
def track_operation(
    tracker: Optional[TaskProgressTracker],
    phase: Union[TaskPhase, str] = TaskPhase.IMPLEMENTING,
    resource_id: Optional[str] = None,
    role: Optional[str] = None,
    cycle: Optional[int] = None,
    details: Optional[str] = None,
    suppress_completion: bool = False,
) -> Iterator[None]:
    """Scoped helper that enters tracker operation when tracker is provided."""
    if tracker is not None:
        with tracker.operation(
            phase=phase,
            resource_id=resource_id,
            role=role,
            cycle=cycle,
            details=details,
            suppress_completion=suppress_completion,
        ):
            yield
    else:
        yield

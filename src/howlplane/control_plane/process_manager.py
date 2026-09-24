#!/usr/bin/env python3
"""
process_manager.py

Process tracking, liveness verification, and graceful cancellation for HowlPlane.
Tracks in-flight child agent processes across lifecycles to enable safe detection,
inspection, and cancellation without blindly killing arbitrary PIDs or destroying worktrees.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import socket
import time
from typing import Any, Dict, Optional, Tuple, Union

from howlplane.control_plane.atomic_io import atomic_write_json, safe_load_json
from howlplane.control_plane.locking import is_process_alive, get_process_create_time
from howlplane.control_plane.task_spec import DataClassSerializationMixin

PROCESS_RECORD_SCHEMA_VERSION = "howlplane.process_record/v1"


@dataclass
class ProcessRecord(DataClassSerializationMixin):
    """Structured record of an active or completed child process."""

    task_id: str
    pid: int
    hostname: str
    backend: str
    command: str
    process_create_time: float
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = "running"  # "running", "completed", "terminated", "interrupted"
    schema: str = PROCESS_RECORD_SCHEMA_VERSION


class ProcessTracker:
    """Manages active process registration and cancellation."""

    @classmethod
    def register_process(
        cls,
        run_dir: Union[str, Path],
        task_id: str,
        pid: int,
        backend: str,
        command: str,
    ) -> ProcessRecord:
        """Records active child process metadata in run_dir."""
        r_dir = Path(run_dir).resolve()
        my_host = socket.gethostname()
        my_ctime = get_process_create_time(pid)

        record = ProcessRecord(
            task_id=task_id,
            pid=pid,
            hostname=my_host,
            backend=backend,
            command=command,
            process_create_time=my_ctime,
            status="running",
        )

        atomic_write_json(r_dir / "process.json", record.to_dict())
        return record

    @classmethod
    def unregister_process(
        cls,
        run_dir: Union[str, Path],
        pid: Optional[int] = None,
    ) -> None:
        """Removes process.json when process finishes cleanly."""
        proc_file = Path(run_dir).resolve() / "process.json"
        if proc_file.exists():
            try:
                proc_file.unlink()
            except Exception:
                pass

    @classmethod
    def get_active_process(
        cls,
        run_dir: Union[str, Path],
    ) -> Optional[ProcessRecord]:
        """Loads and verifies liveness of active process from process.json."""
        proc_file = Path(run_dir).resolve() / "process.json"
        if not proc_file.is_file():
            return None

        try:
            data = safe_load_json(proc_file)
            record = ProcessRecord.from_dict(data)
        except Exception:
            return None

        alive, _ = is_process_alive(
            record.pid, record.hostname, record.process_create_time
        )
        if not alive:
            record.status = "interrupted"
        return record

    @classmethod
    def terminate_task_process(
        cls,
        run_dir: Union[str, Path],
        timeout_seconds: float = 3.0,
    ) -> Tuple[bool, str]:
        """
        Gracefully terminates an active task process (SIGTERM -> wait -> SIGKILL).
        Returns (terminated, description).
        """
        proc = cls.get_active_process(run_dir)
        if not proc:
            return False, "No active running process registered for this task"

        alive, reason = is_process_alive(proc.pid, proc.hostname, proc.process_create_time)
        if not alive:
            cls.unregister_process(run_dir)
            return False, f"Registered process PID {proc.pid} was already dead: {reason}"

        # 1. Request graceful termination first via SIGTERM
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except OSError as e:
            cls.unregister_process(run_dir)
            return False, f"Failed to send SIGTERM to PID {proc.pid}: {e}"

        # 2. Wait up to timeout_seconds for graceful exit
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            time.sleep(0.1)
            still_alive, _ = is_process_alive(proc.pid, proc.hostname, proc.process_create_time)
            if not still_alive:
                cls.unregister_process(run_dir)
                return True, f"Process PID {proc.pid} ({proc.backend}) gracefully terminated."

        # 3. Escalate to SIGKILL if still running
        try:
            os.kill(proc.pid, signal.SIGKILL)
            time.sleep(0.1)
        except OSError:
            pass

        cls.unregister_process(run_dir)
        return True, f"Process PID {proc.pid} ({proc.backend}) forcefully terminated after timeout."

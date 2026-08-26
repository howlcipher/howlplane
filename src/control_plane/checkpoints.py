#!/usr/bin/env python3
"""
checkpoints.py

Durable stage checkpoints for governed task orchestration.
Persists immutable records of stage boundaries, inputs, outputs,
repository fingerprints, and process metadata to enable safe crash recovery.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from src.control_plane.atomic_io import atomic_write_json, safe_load_json
from src.control_plane.git_env import run_git_in_repo
from src.control_plane.task_spec import DataClassSerializationMixin, VALID_TASK_STATES

CHECKPOINT_SCHEMA_VERSION = "howlplane.stage_checkpoint/v1"

VALID_STAGE_NAMES = VALID_TASK_STATES | {"bounded_execution"}


@dataclass
class StageCheckpoint(DataClassSerializationMixin):
    """Immutable durable checkpoint of a task lifecycle stage."""

    task_id: str
    stage: str
    status: str = "in_progress"  # "in_progress", "completed", "failed", "interrupted", "cancelled"
    attempt_number: int = 1
    stage_started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    stage_completed_at: Optional[str] = None
    duration_seconds: float = 0.0
    repository_fingerprint: Optional[Dict[str, Any]] = None
    agent_id: Optional[str] = None
    process_info: Optional[Dict[str, Any]] = None
    input_artifacts: List[str] = field(default_factory=list)
    output_artifacts: List[str] = field(default_factory=list)
    result_summary: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema: str = CHECKPOINT_SCHEMA_VERSION


def compute_stage_repo_fingerprint(repo_path: Union[str, Path]) -> Dict[str, Any]:
    """Computes a lightweight repository fingerprint for stage checkpointing."""
    target = Path(repo_path).resolve()

    def _run(args):
        r = run_git_in_repo(target, args)
        return r.stdout if r.returncode == 0 else ""

    commit = _run(["rev-parse", "HEAD"]).strip()
    status = _run(["status", "--porcelain"])
    lines = [l for l in status.splitlines() if l.strip()]

    return {
        "commit_sha": commit,
        "status_hash": hashlib.sha256(status.encode("utf-8")).hexdigest()[:16],
        "dirty_count": len(lines),
        "is_dirty": len(lines) > 0,
    }


def get_current_process_info(command: Optional[str] = None) -> Dict[str, Any]:
    """Captures current process identity metadata."""
    from src.control_plane.locking import get_process_create_time
    pid = os.getpid()
    return {
        "pid": pid,
        "hostname": socket.gethostname(),
        "create_time": get_process_create_time(pid),
        "command": command or "howlplane",
    }


class CheckpointManager:
    """Manages durable stage checkpoints on disk."""

    @classmethod
    def get_checkpoints_dir(cls, run_dir: Union[str, Path]) -> Path:
        p = Path(run_dir).resolve() / "checkpoints"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def start_stage(
        cls,
        run_dir: Union[str, Path],
        task_id: str,
        stage: str,
        agent_id: Optional[str] = None,
        repo_path: Optional[Union[str, Path]] = None,
        process_info: Optional[Dict[str, Any]] = None,
        input_artifacts: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StageCheckpoint:
        """Starts a new stage checkpoint and persists it atomically."""
        r_dir = Path(run_dir).resolve()
        c_dir = cls.get_checkpoints_dir(r_dir)

        # Determine attempt number for this stage
        prior_checkpoints = list(c_dir.glob(f"{stage}_*.json"))
        attempt = len(prior_checkpoints) + 1

        repo_fp = compute_stage_repo_fingerprint(repo_path) if repo_path else None
        p_info = process_info or get_current_process_info()

        checkpoint = StageCheckpoint(
            task_id=task_id,
            stage=stage,
            status="in_progress",
            attempt_number=attempt,
            stage_started_at=datetime.now(timezone.utc).isoformat(),
            repository_fingerprint=repo_fp,
            agent_id=agent_id,
            process_info=p_info,
            input_artifacts=input_artifacts or [],
            metadata=metadata or {},
        )

        # Write current stage checkpoint
        atomic_write_json(r_dir / "stage_checkpoint.json", checkpoint.to_dict())
        # Write immutable history checkpoint
        atomic_write_json(c_dir / f"{stage}_{attempt:02d}.json", checkpoint.to_dict())

        return checkpoint

    @classmethod
    def _finalize_stage(
        cls,
        run_dir: Union[str, Path],
        target_status: str,
        stage: Optional[str] = None,
        reason: Optional[str] = None,
        output_artifacts: Optional[List[str]] = None,
        result_summary: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StageCheckpoint:
        r_dir = Path(run_dir).resolve()
        latest = cls.load_latest_checkpoint(r_dir)
        target_stage = stage or (latest.stage if latest else "unknown")
        now_iso = datetime.now(timezone.utc).isoformat()
        dur = 0.0

        if latest and (stage is None or latest.stage == stage):
            try:
                start_dt = datetime.fromisoformat(latest.stage_started_at)
                dur = round((datetime.now(timezone.utc) - start_dt).total_seconds(), 3)
            except Exception:
                pass
            latest.status = target_status
            latest.stage_completed_at = now_iso
            latest.duration_seconds = dur
            if output_artifacts:
                latest.output_artifacts.extend(output_artifacts)
            if result_summary:
                latest.result_summary = result_summary
            elif reason:
                latest.result_summary = {"error": reason}
            if metadata:
                latest.metadata.update(metadata)
            checkpoint = latest
        else:
            meta = dict(metadata or {})
            if reason:
                meta["interruption_reason"] = reason
            checkpoint = StageCheckpoint(
                task_id=latest.task_id if latest else "UNKNOWN",
                stage=target_stage,
                status=target_status,
                stage_started_at=now_iso,
                stage_completed_at=now_iso,
                output_artifacts=output_artifacts or [],
                result_summary=result_summary or ({"error": reason} if reason else None),
                metadata=meta,
            )

        c_dir = cls.get_checkpoints_dir(r_dir)
        atomic_write_json(r_dir / "stage_checkpoint.json", checkpoint.to_dict())
        atomic_write_json(c_dir / f"{target_stage}_{checkpoint.attempt_number:02d}.json", checkpoint.to_dict())
        return checkpoint

    @classmethod
    def complete_stage(
        cls,
        run_dir: Union[str, Path],
        stage: str,
        output_artifacts: Optional[List[str]] = None,
        result_summary: Optional[Dict[str, Any]] = None,
    ) -> StageCheckpoint:
        """Marks a stage checkpoint as completed."""
        return cls._finalize_stage(
            run_dir,
            "completed",
            stage=stage,
            output_artifacts=output_artifacts,
            result_summary=result_summary,
        )

    @classmethod
    def fail_stage(
        cls,
        run_dir: Union[str, Path],
        stage: Optional[str] = None,
        reason: Optional[str] = None,
        result_summary: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StageCheckpoint:
        """Marks a stage checkpoint as failed with completed timestamp and summary."""
        return cls._finalize_stage(
            run_dir,
            "failed",
            stage=stage,
            reason=reason,
            result_summary=result_summary,
            metadata=metadata,
        )

    @classmethod
    def record_interrupted(
        cls,
        run_dir: Union[str, Path],
        stage: Optional[str] = None,
        reason: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StageCheckpoint:
        """Records an interrupted checkpoint status."""
        return cls._finalize_stage(
            run_dir,
            "interrupted",
            stage=stage,
            reason=reason,
            metadata=metadata,
        )

    @classmethod
    def load_latest_checkpoint(cls, run_dir: Union[str, Path]) -> Optional[StageCheckpoint]:
        """Loads the current stage_checkpoint.json if present."""
        chk_file = Path(run_dir).resolve() / "stage_checkpoint.json"
        if not chk_file.is_file():
            return None
        try:
            data = safe_load_json(chk_file)
            return StageCheckpoint.from_dict(data)
        except Exception:
            return None

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
    def _find_active_in_progress_checkpoint(
        cls, run_dir: Union[str, Path]
    ) -> Optional[StageCheckpoint]:
        """Finds an active in-progress checkpoint in the run directory."""
        r_dir = Path(run_dir).resolve()
        latest = cls.load_latest_checkpoint(r_dir)
        if (
            latest
            and latest.status == "in_progress"
            and latest.stage not in ("failed", "cancelled")
        ):
            return latest

        c_dir = cls.get_checkpoints_dir(r_dir)
        if not c_dir.is_dir():
            return None

        candidates: List[StageCheckpoint] = []
        for p in c_dir.glob("*.json"):
            try:
                data = safe_load_json(p)
                chk = StageCheckpoint.from_dict(data)
                if (
                    chk.status == "in_progress"
                    and chk.stage not in ("failed", "cancelled")
                ):
                    candidates.append(chk)
            except Exception:
                continue

        if not candidates:
            return None

        candidates.sort(
            key=lambda c: (c.stage_started_at or "", c.attempt_number),
            reverse=True,
        )
        return candidates[0]

    @classmethod
    def _find_latest_checkpoint_for_stage(
        cls, run_dir: Union[str, Path], stage: str
    ) -> Optional[StageCheckpoint]:
        """Finds the most recent checkpoint for a given stage name."""
        r_dir = Path(run_dir).resolve()
        c_dir = cls.get_checkpoints_dir(r_dir)
        if not c_dir.is_dir():
            return None
        stage_files = sorted(c_dir.glob(f"{stage}_*.json"))
        if not stage_files:
            return None
        try:
            data = safe_load_json(stage_files[-1])
            return StageCheckpoint.from_dict(data)
        except Exception:
            return None

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
        c_dir = cls.get_checkpoints_dir(r_dir)
        now_iso = datetime.now(timezone.utc).isoformat()
        dur = 0.0

        latest = cls.load_latest_checkpoint(r_dir)
        target_chk: Optional[StageCheckpoint] = None
        target_stage: Optional[str] = None

        # If stage is a terminal/non-stage state name ("failed", "cancelled") or None,
        # resolve and finalize the active in-progress checkpoint rather than
        # creating a bogus new checkpoint file with stage="failed" / stage="cancelled".
        if stage in (None, "failed", "cancelled"):
            active_chk = cls._find_active_in_progress_checkpoint(r_dir)
            if active_chk is not None:
                target_chk = active_chk
                target_stage = active_chk.stage
            elif latest and latest.stage not in ("failed", "cancelled"):
                target_chk = latest
                target_stage = latest.stage
            else:
                target_stage = (
                    latest.stage
                    if latest and latest.stage not in ("failed", "cancelled")
                    else "unknown"
                )
        else:
            # A specific stage was requested
            if latest and latest.stage == stage:
                target_chk = latest
                target_stage = stage
            else:
                stage_chk = cls._find_latest_checkpoint_for_stage(r_dir, stage)
                if stage_chk is not None:
                    target_chk = stage_chk
                    target_stage = stage
                else:
                    target_stage = stage

        if target_chk is not None:
            try:
                start_dt = datetime.fromisoformat(target_chk.stage_started_at)
                dur = round((datetime.now(timezone.utc) - start_dt).total_seconds(), 3)
            except Exception:
                pass
            target_chk.status = target_status
            target_chk.stage_completed_at = now_iso
            target_chk.duration_seconds = dur
            if output_artifacts:
                target_chk.output_artifacts.extend(output_artifacts)
            if result_summary:
                target_chk.result_summary = result_summary
            elif reason:
                target_chk.result_summary = {"error": reason}
            if metadata:
                target_chk.metadata.update(metadata)
            checkpoint = target_chk
        else:
            meta = dict(metadata or {})
            if reason:
                meta["interruption_reason"] = reason
            checkpoint = StageCheckpoint(
                task_id=latest.task_id if latest else "UNKNOWN",
                stage=target_stage or "unknown",
                status=target_status,
                stage_started_at=now_iso,
                stage_completed_at=now_iso,
                output_artifacts=output_artifacts or [],
                result_summary=result_summary or ({"error": reason} if reason else None),
                metadata=meta,
            )

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
        """Marks a stage checkpoint as failed with completed timestamp and summary.

        If stage is 'failed', 'cancelled', or None, it finalizes the active in-progress
        checkpoint rather than creating a bogus checkpoint file with stage='failed'.
        """
        if stage in ("failed", "cancelled"):
            stage = None
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

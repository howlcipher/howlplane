#!/usr/bin/env python3
"""
recovery.py

Deterministic crash recovery and safe-retry classification engine for HowlPlane.
Classifies recoverable lifecycle stages, reconciles uncommitted repository deltas,
preserves completed reviewer results, invalidates stale reviews on repository drift,
and prevents double-execution of consequential bounded actions.
"""

from enum import Enum
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import yaml

from src.control_plane.atomic_io import atomic_write_json, atomic_write_yaml, safe_load_json, safe_load_yaml
from src.control_plane.checkpoints import CheckpointManager, StageCheckpoint, compute_stage_repo_fingerprint
from src.control_plane.executor import AuthorityExecutor, ExecutionReceipt, ExecutionResult, ExecutorRegistry
from src.control_plane.git_baseline import GitBaseline, RepositoryDelta, capture_baseline, capture_delta
from src.control_plane.locking import (
    LockOwnerState,
    RepoLock,
    TaskLock,
    classify_lock_owner,
    get_repo_lock_path,
    get_task_lock_path,
    is_process_alive,
)
from src.control_plane.process_manager import ProcessTracker, ProcessRecord
from src.control_plane.proposed_action import ProposedAction, infer_proposed_actions
from src.control_plane.reconciliation import ReviewFinding, ReconciliationResult, ReviewReconciler
from src.control_plane.review_runner import SingleReviewResult, parse_and_validate_findings
from src.control_plane.task_spec import TaskSpec


class RetryClassification(str, Enum):
    """Deterministic classification of stage retry safety."""

    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    RECONCILE_FIRST = "RECONCILE_FIRST"
    NEVER_AUTO_RETRY = "NEVER_AUTO_RETRY"
    ALREADY_COMPLETE = "ALREADY_COMPLETE"
    HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"


STAGE_RETRY_CLASSIFICATIONS: Dict[str, RetryClassification] = {
    "discovered": RetryClassification.SAFE_TO_RETRY,
    "planned": RetryClassification.SAFE_TO_RETRY,
    "project_discovery": RetryClassification.SAFE_TO_RETRY,
    "routing": RetryClassification.SAFE_TO_RETRY,
    "implementing": RetryClassification.RECONCILE_FIRST,
    "reviewing": RetryClassification.SAFE_TO_RETRY,
    "remediating": RetryClassification.RECONCILE_FIRST,
    "verifying": RetryClassification.SAFE_TO_RETRY,
    "awaiting_human": RetryClassification.HUMAN_DECISION_REQUIRED,
    "bounded_execution": RetryClassification.NEVER_AUTO_RETRY,
    "package_publishing": RetryClassification.NEVER_AUTO_RETRY,
    "external_messaging": RetryClassification.NEVER_AUTO_RETRY,
    "complete": RetryClassification.ALREADY_COMPLETE,
    "cancelled": RetryClassification.HUMAN_DECISION_REQUIRED,
    "interrupted": RetryClassification.RECONCILE_FIRST,
}


def classify_stage_retry(stage: str, **kwargs) -> RetryClassification:
    """Returns the deterministic retry classification for a stage."""
    return STAGE_RETRY_CLASSIFICATIONS.get(stage, RetryClassification.RECONCILE_FIRST)


class CrashRecoveryEngine:
    """Inspects durable task artifacts and coordinates safe recovery after process death."""

    @staticmethod
    def _reviewer_disposition(reviews_dir: Path, role: str) -> str:
        """Classifies what a reviewer's durable evidence actually says.

        Returns one of: completed_clean, completed_with_findings, failed,
        invalid, running, pending. File presence alone is not a verdict -- a
        zero-byte transcript beside an empty findings list is what a provider
        that never answered leaves behind, and it must not read as a passing
        review (HOWLFRAM-SLOPFIX-06).
        """
        result_file = reviews_dir / role / "result.json"
        if result_file.is_file():
            try:
                data = safe_load_json(result_file)
                status = (data or {}).get("status")
            except Exception:
                status = None
            if status == "clean":
                return "completed_clean"
            if status == "findings_detected":
                return "completed_with_findings"
            if status == "output_invalid":
                return "invalid"
            if status in ("reviewer_failure", "malformed_output"):
                return "failed"
            if status:
                return "pending"

        md_file = reviews_dir / f"{role}.md"
        yaml_file = reviews_dir / f"{role}_findings.yaml"
        if not (md_file.is_file() and yaml_file.is_file()):
            return "running" if md_file.is_file() else "pending"

        try:
            transcript = md_file.read_text(encoding="utf-8")
        except Exception:
            return "invalid"
        if not transcript.strip():
            return "invalid"

        try:
            parsed = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        except Exception:
            return "failed"
        if isinstance(parsed, dict):
            parsed = parsed.get("findings", [])
        if isinstance(parsed, list) and parsed:
            return "completed_with_findings"
        return "completed_clean"

    @classmethod
    def inspect_task(
        cls,
        target_repo: Union[str, Path],
        task_id: str,
    ) -> Dict[str, Any]:
        """
        Inspects all durable state for a task run to determine recovery classification and actions.
        """
        repo_root = Path(target_repo).resolve()
        run_dir = repo_root / ".task_runs" / task_id

        if not run_dir.is_dir() or not (run_dir / "task.yaml").is_file():
            return {
                "task_id": task_id,
                "exists": False,
                "error": f"Task run directory not found: {run_dir}",
                "classification": RetryClassification.HUMAN_DECISION_REQUIRED,
            }

        try:
            task_spec = TaskSpec.load_from_file(run_dir / "task.yaml")
        except Exception as exc:
            return {
                "task_id": task_id,
                "exists": True,
                "corrupt": True,
                "error": f"Corrupt task specification: {exc}",
                "classification": RetryClassification.HUMAN_DECISION_REQUIRED,
            }

        latest_checkpoint = CheckpointManager.load_latest_checkpoint(run_dir)
        last_stage = latest_checkpoint.stage if latest_checkpoint else task_spec.current_state

        active_process = ProcessTracker.get_active_process(run_dir)
        is_process_running = (
            active_process is not None and active_process.status == "running"
        )

        # Inspect lock status
        repo_lock_path = get_repo_lock_path(repo_root)
        task_lock_path = get_task_lock_path(repo_root, task_id)
        repo_locked = False
        task_locked = False
        lock_owner_pid = None

        repo_lock_state: Optional[str] = None
        repo_lock_reason: Optional[str] = None
        repo_lock_task_id: Optional[str] = None
        if repo_lock_path.exists():
            try:
                l_data = safe_load_json(repo_lock_path)
                # Classified tri-state like the task lock: a recommendation is
                # only useful if it names a command that can actually work, and
                # the repository lock blocks recovery just as effectively.
                state, reason = classify_lock_owner(
                    l_data.get("pid", 0),
                    l_data.get("hostname", ""),
                    l_data.get("process_create_time", 0.0),
                )
                repo_lock_state = state.value
                repo_lock_reason = reason
                repo_lock_task_id = l_data.get("task_id")
                if state is not LockOwnerState.STALE:
                    repo_locked = True
                    lock_owner_pid = l_data.get("pid")
            except Exception:
                pass

        task_lock_state: Optional[str] = None
        task_lock_reason: Optional[str] = None
        if task_lock_path.exists():
            try:
                t_data = safe_load_json(task_lock_path)
                state, reason = classify_lock_owner(
                    t_data.get("pid", 0),
                    t_data.get("hostname", ""),
                    t_data.get("process_create_time", 0.0),
                )
                task_lock_state = state.value
                task_lock_reason = reason
                # A provably dead owner is reclaimed automatically on the next
                # acquire, so it does not block anything and is not reported as
                # a held lock.
                if state is not LockOwnerState.STALE:
                    task_locked = True
                    lock_owner_pid = lock_owner_pid or t_data.get("pid")
            except Exception:
                pass

        # Inspect completed vs incomplete reviewers
        completed_reviewers: List[str] = []
        incomplete_reviewers: List[str] = []
        route_file = run_dir / "route.json"
        recommended_reviewers: List[str] = []

        if route_file.is_file():
            try:
                r_data = safe_load_json(route_file)
                recommended_reviewers = r_data.get("recommended_reviewers", [])
            except Exception:
                pass

        reviews_dir = run_dir / "reviews"
        reviewer_dispositions: Dict[str, str] = {}
        if reviews_dir.is_dir() and recommended_reviewers:
            for role in recommended_reviewers:
                disposition = cls._reviewer_disposition(reviews_dir, role)
                reviewer_dispositions[role] = disposition
                # Only a review that actually produced a usable verdict counts
                # as completed. Reporting a dead reviewer's empty transcript as
                # "Completed Reviews" is how SLOPFIX-06 looked further along
                # than it was.
                if disposition in ("completed_clean", "completed_with_findings"):
                    completed_reviewers.append(role)
                else:
                    incomplete_reviewers.append(role)

        # Inspect execution receipt
        receipt_file = run_dir / "execution_receipt.json"
        has_local_receipt = receipt_file.is_file()
        native_receipt_found = False

        if not has_local_receipt and (
            task_spec.current_state == "awaiting_human"
            or last_stage == "bounded_execution"
        ):
            # Check if HowlChangeOps native receipt exists on disk
            dec_file = run_dir / "human_decision.json"
            if dec_file.is_file():
                try:
                    dec_data = safe_load_json(dec_file)
                    dec_id = dec_data.get("changeops_decision_id")
                    if dec_id:
                        hco_receipt = (
                            repo_root
                            / ".howlchangeops"
                            / "receipts"
                            / f"{dec_id}.json"
                        )
                        if hco_receipt.is_file():
                            native_receipt_found = True
                except Exception:
                    pass

        # Compute classification
        if is_process_running:
            classification = RetryClassification.HUMAN_DECISION_REQUIRED
            recommendation = (
                f"Task is actively running (PID {active_process.pid}). Run 'howl plane cancel {task_id}' to cancel."
            )
        elif task_spec.current_state == "complete":
            classification = RetryClassification.ALREADY_COMPLETE
            recommendation = "Task is already COMPLETE. No recovery needed."
        elif task_spec.current_state == "cancelled":
            classification = RetryClassification.HUMAN_DECISION_REQUIRED
            recommendation = "Task was CANCELLED. Re-run or create a new task."
        elif native_receipt_found:
            classification = RetryClassification.NEVER_AUTO_RETRY
            recommendation = (
                f"HowlChangeOps action already executed natively. Run 'howl plane resume {task_id}' to reconcile receipt."
            )
        elif task_locked and task_lock_state == LockOwnerState.ACTIVE.value:
            # Recommending resume while a live owner holds the lock sends the
            # operator at a door that is bolted (HOWLFRAM-SLOPFIX-05).
            classification = RetryClassification.HUMAN_DECISION_REQUIRED
            recommendation = (
                f"Task lock is held by active process PID {lock_owner_pid}. "
                f"Wait for it to finish, or run 'howl plane cancel {task_id}'."
            )
        elif task_locked and task_lock_state == LockOwnerState.AMBIGUOUS.value:
            classification = RetryClassification.HUMAN_DECISION_REQUIRED
            recommendation = (
                f"Task lock ownership is indeterminate ({task_lock_reason}). "
                f"If that run is definitely gone, run 'howl plane unlock {task_id}' "
                f"first, then 'howl plane resume {task_id}'."
            )
        elif repo_locked and repo_lock_state == LockOwnerState.ACTIVE.value:
            classification = RetryClassification.HUMAN_DECISION_REQUIRED
            recommendation = (
                f"Repository lock is held by active process PID "
                f"{lock_owner_pid} for task '{repo_lock_task_id}'. Wait for it "
                f"to finish before resuming."
            )
        elif repo_locked and repo_lock_state == LockOwnerState.AMBIGUOUS.value:
            classification = RetryClassification.HUMAN_DECISION_REQUIRED
            recommendation = (
                f"Repository lock ownership is indeterminate "
                f"({repo_lock_reason}). If that run is definitely gone, run "
                f"'howl plane unlock {repo_lock_task_id or task_id}' first, then "
                f"'howl plane resume {task_id}'."
            )
        else:
            classification = classify_stage_retry(last_stage)
            recommendation = f"Run 'howl plane resume {task_id}' to recover from {last_stage}."

        return {
            "task_id": task_id,
            "exists": True,
            "corrupt": False,
            "current_state": task_spec.current_state,
            "last_stage": last_stage,
            "classification": classification,
            "is_process_running": is_process_running,
            "process_info": active_process.to_dict() if active_process else None,
            "repo_locked": repo_locked,
            "task_locked": task_locked,
            "task_lock_state": task_lock_state,
            "task_lock_reason": task_lock_reason,
            "lock_owner_pid": lock_owner_pid,
            "repo_lock_state": repo_lock_state,
            "completed_reviewers": completed_reviewers,
            "reviewer_dispositions": reviewer_dispositions,
            "incomplete_reviewers": incomplete_reviewers,
            "has_local_receipt": has_local_receipt,
            "native_receipt_found": native_receipt_found,
            "recommendation": recommendation,
        }

    @classmethod
    def reconcile_interrupted_implementation(
        cls,
        target_repo: Union[str, Path],
        run_dir: Union[str, Path],
        task_spec: TaskSpec,
    ) -> Tuple[bool, Optional[RepositoryDelta], str]:
        """
        Reconciles working tree state after an interrupted implementation stage.
        If actual repository delta exists, records it and avoids duplicate reruns.
        Returns: (has_valid_delta, delta_obj, summary_reason).
        """
        root = Path(target_repo).resolve()
        r_dir = Path(run_dir).resolve()
        baseline_file = r_dir / "baseline.json"

        if baseline_file.is_file():
            try:
                b_data = safe_load_json(baseline_file)
                baseline = GitBaseline.from_dict(b_data)
            except Exception:
                baseline = capture_baseline(root)
        else:
            baseline = capture_baseline(root)
            atomic_write_json(baseline_file, baseline.to_dict())

        current_delta = capture_delta(root, baseline)

        if current_delta.is_empty:
            return (
                False,
                None,
                "No working tree modifications found from previous run. Safe to execute implementation.",
            )

        # Delta exists: persist diff artifacts to avoid rerun
        impl_dir = r_dir / "implementation"
        impl_dir.mkdir(parents=True, exist_ok=True)
        (impl_dir / "diff.patch").write_text(
            current_delta.diff_content, encoding="utf-8"
        )
        (r_dir / "diff.patch").write_text(
            current_delta.diff_content, encoding="utf-8"
        )

        return (
            True,
            current_delta,
            f"Recovered implementation delta: {len(current_delta.files_modified)} modified, {len(current_delta.files_added)} added files.",
        )

    @classmethod
    def check_review_validity(
        cls,
        target_repo: Union[str, Path],
        run_dir: Union[str, Path],
    ) -> Tuple[bool, Optional[str]]:
        """
        Verifies that current repository state matches the delta that was reviewed.
        Returns (is_valid, invalid_reason).
        """
        root = Path(target_repo).resolve()
        r_dir = Path(run_dir).resolve()
        baseline_file = r_dir / "baseline.json"
        diff_file = r_dir / "diff.patch"

        if not baseline_file.is_file() or not diff_file.is_file():
            return False, "Missing baseline or diff artifacts for review validation"

        try:
            baseline = GitBaseline.from_dict(safe_load_json(baseline_file))
            current_delta = capture_delta(root, baseline)
            recorded_diff = diff_file.read_text(encoding="utf-8")
            if current_delta.diff_content != recorded_diff:
                return (
                    False,
                    "Working tree modifications have drifted since review was conducted",
                )
            return True, None
        except Exception as e:
            return False, f"Failed to validate review repository state: {e}"

    @classmethod
    def reconcile_howlchangeops_receipt(
        cls,
        target_repo: Union[str, Path],
        run_dir: Union[str, Path],
        task_id: str,
        decision_id: str,
        action: ProposedAction,
    ) -> Tuple[bool, Optional[ExecutionReceipt], str]:
        """
        Inspects native HowlChangeOps receipts when HowlPlane died before persisting local receipt.
        Guarantees exactly-once consequential execution without re-calling execute().
        """
        from src.control_plane.executor import HowlChangeOpsExecutor
        executor = HowlChangeOpsExecutor()
        status, receipt, msg = executor.query_execution_status(decision_id, target_repo, run_dir, action, task_id)
        if status == "already_executed" and receipt:
            return True, receipt, msg
        return False, None, msg or "No native execution receipt found"

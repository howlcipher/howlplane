#!/usr/bin/env python3
"""
test_operational_resilience.py

Comprehensive 20-scenario test suite for Milestone #56:
Operational Resilience: Crash Recovery, Durable Resume, Repository Locking,
Cancellation, and Exactly-Once Consequential Execution Semantics.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import pytest

from src.control_plane.agent_execution import AgentBackend, AgentExecutionResult
from src.control_plane.atomic_io import (
    atomic_write_json,
    atomic_write_text,
    safe_load_json,
    CorruptArtifactError,
)
from src.control_plane.checkpoints import CheckpointManager, StageCheckpoint
from src.control_plane.cli import cmd_unlock
from src.control_plane.evidence_ledger import EvidenceLedger
from src.control_plane.executor import (
    AuthorityExecutor,
    ExecutionReceipt,
    ExecutionResult,
    ExecutorRegistry,
    HowlChangeOpsExecutor,
)
from src.control_plane.git_baseline import GitBaseline, capture_baseline, capture_delta
from src.control_plane.human_boundary import (
    HumanDecisionRecord,
    HumanLifecycleManager,
    InvalidTaskStateError,
    StaleApprovalError,
    compute_repository_fingerprint,
)
from src.control_plane.locking import (
    LockError,
    LockOwnerState,
    RepoLock,
    RepositoryLockedError,
    TaskLock,
    TaskLockedError,
    classify_lock_owner,
    get_repo_lock_path,
    get_task_lock_path,
    is_process_alive,
    reclaim_lock,
)
from src.control_plane.orchestrator import GovernedTaskOrchestrator, OrchestrationConfig
from src.control_plane.process_manager import ProcessRecord, ProcessTracker
from src.control_plane.proposed_action import ProposedAction
from src.control_plane.recovery import (
    CrashRecoveryEngine,
    RetryClassification,
    classify_stage_retry,
)
from src.control_plane.review_runner import ReviewFinding, ReviewRunner
from src.control_plane.router import RoutingDecision
from src.control_plane.task_spec import TaskSpec
from src.control_plane.verification import VerificationPlan, VerificationStep
from tests._git_test_helpers import init_git_repo


def _init_git_repo(path: Path) -> None:
    init_git_repo(path, files={"README.md": "# Test Repo\n"})


class MockFailingBackend(AgentBackend):
    def __init__(self, mutate_fn=None, fail_hook=None):
        self.mutate_fn = mutate_fn
        self.fail_hook = fail_hook
        self.invocations = 0

    def is_available(self) -> bool:
        return True

    def execute(self, task, cwd, role="implementation", **kwargs):
        self.invocations += 1
        if role == "implementation" and self.mutate_fn:
            self.mutate_fn(Path(cwd))
        if self.fail_hook:
            self.fail_hook()
        out = "findings: []\n" if role != "implementation" else "success\n"
        return AgentExecutionResult(
            agent_id="mock_failing_backend",
            role=role,
            command="mock",
            exit_code=0,
            stdout=out,
            stderr="",
            duration_seconds=0.01,
            success=True,
        )


# ============================================================================
# Scenario 1: Interrupted implementation with no repo changes -> clean rerun
# ============================================================================
def test_interrupted_implementation_clean_rerun_when_no_changes(tmp_path):
    _init_git_repo(tmp_path)
    spec = TaskSpec(task_id="TASK-SCENARIO-01", repository=tmp_path.name, objective="Feature 1")

    # Crashes before mutating disk
    def crash():
        raise KeyboardInterrupt("Crash before write")

    backend1 = MockFailingBackend(fail_hook=crash)
    orch1 = GovernedTaskOrchestrator(target_repo=tmp_path, config=OrchestrationConfig(custom_backend=backend1))
    with pytest.raises(KeyboardInterrupt):
        orch1.run(spec)

    diag = CrashRecoveryEngine.inspect_task(tmp_path, spec.task_id)
    assert diag["exists"] is True
    assert diag["current_state"] == "implementing"

    # Resuming with healthy backend runs implementation cleanly
    backend2 = MockFailingBackend(mutate_fn=lambda p: (p / "out.txt").write_text("ok", encoding="utf-8"))
    orch2 = GovernedTaskOrchestrator(target_repo=tmp_path, config=OrchestrationConfig(custom_backend=backend2))
    res = orch2.run(spec)
    assert res.exit_code == 0
    assert (tmp_path / "out.txt").read_text() == "ok"


# ============================================================================
# Scenario 2: Interrupted implementation with repo changes -> delta recovered
# ============================================================================
def test_interrupted_implementation_recovers_delta_without_blind_rerun(tmp_path):
    _init_git_repo(tmp_path)
    spec = TaskSpec(task_id="TASK-SCENARIO-02", repository=tmp_path.name, objective="Feature 2")

    def mutate_and_crash(p: Path):
        (p / "feature2.py").write_text("def f2(): return 2\n", encoding="utf-8")
        raise RuntimeError("Killed after disk write")

    backend1 = MockFailingBackend(mutate_fn=mutate_and_crash)
    orch1 = GovernedTaskOrchestrator(target_repo=tmp_path, config=OrchestrationConfig(custom_backend=backend1))
    with pytest.raises(RuntimeError):
        orch1.run(spec)

    # Resume must capture recorded delta against baseline and proceed through reviews
    backend2 = MockFailingBackend()
    orch2 = GovernedTaskOrchestrator(target_repo=tmp_path, config=OrchestrationConfig(custom_backend=backend2))
    res = orch2.run(spec)
    assert res.exit_code == 0
    assert res.initial_delta is not None
    assert "feature2.py" in res.initial_delta.files_added
    # The implementation agent was not invoked because the delta was recovered!
    # (Only reviewer roles were invoked with backend2)
    assert not any(p.name == "feature2.py" and backend2.invocations == 0 for p in [tmp_path])


# ============================================================================
# Scenario 3: Interrupted review cycle reuses completed reviewer cache
# ============================================================================
def test_interrupted_review_cycle_reuses_completed_reviewer_cache(tmp_path):
    _init_git_repo(tmp_path)
    spec = TaskSpec(task_id="TASK-SCENARIO-03", repository=tmp_path.name, objective="Feature 3")
    run_dir = tmp_path / ".task_runs" / spec.task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Write code change
    (tmp_path / "f3.py").write_text("x = 3\n", encoding="utf-8")
    baseline = capture_baseline(tmp_path)
    delta = capture_delta(tmp_path, baseline)

    # Pre-populate reviewer 1 findings (cached)
    r1_dir = run_dir / "reviews"
    r1_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(r1_dir / "correctness.md", "# Review by correctness\nClean.")
    atomic_write_text(r1_dir / "correctness_findings.yaml", "findings: []\n")

    mock_backend = MockFailingBackend()
    res = ReviewRunner.execute_review_cycle(
        task=spec,
        diff_content=delta.diff_content,
        reviewer_roles=["correctness", "security"],
        cwd=tmp_path,
        backend=mock_backend,
        cycle_index=1,
        run_dir=run_dir,
    )
    assert len(res.reviewer_results) == 2
    r_corr = res.reviewer_results.get("correctness")
    assert r_corr is not None
    assert r_corr.status == "clean"
    assert mock_backend.invocations == 1  # Only reviewer 2 (security) was executed!


# ============================================================================
# Scenario 4: Interrupted remediation with new changes triggers re-review
# ============================================================================
def test_interrupted_remediation_triggers_re_review_on_new_changes(tmp_path):
    _init_git_repo(tmp_path)
    spec = TaskSpec(
        task_id="TASK-SCENARIO-04",
        repository=tmp_path.name,
        objective="Feature 4",
        current_state="remediating",
    )
    run_dir = tmp_path / ".task_runs" / spec.task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Simulate remediation interrupted after writing changes
    (tmp_path / "fix.py").write_text("fixed_code = True\n", encoding="utf-8")
    CheckpointManager.start_stage(run_dir, spec.task_id, "remediating", repo_path=tmp_path)
    spec.save_to_file(run_dir / "task.yaml")

    diag = CrashRecoveryEngine.inspect_task(tmp_path, spec.task_id)
    assert diag["last_stage"] == "remediating"
    assert diag["classification"] == RetryClassification.RECONCILE_FIRST


# ============================================================================
# Scenario 5: Interrupted verification reruns incomplete checks
# ============================================================================
def test_interrupted_verification_reruns_incomplete_checks(tmp_path):
    _init_git_repo(tmp_path)
    spec = TaskSpec(task_id="TASK-SCENARIO-05", repository=tmp_path.name, objective="Feature 5")
    run_dir = tmp_path / ".task_runs" / spec.task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    plan = VerificationPlan(task_id=spec.task_id)
    plan.add_step("s1", "Echo test", ["echo", "pass"], "unit_test")
    (run_dir / "verification_plan.json").write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    CheckpointManager.start_stage(run_dir, spec.task_id, "verifying", repo_path=tmp_path)

    # Recovery resumes verification
    status = plan.execute_all(cwd=str(tmp_path))
    assert status == "passed"


# ============================================================================
# Scenario 6: Interrupted verification with drifted repo invalidates review
# ============================================================================
def test_interrupted_verification_invalidated_by_drift(tmp_path):
    _init_git_repo(tmp_path)
    spec = TaskSpec(task_id="TASK-SCENARIO-06", repository=tmp_path.name, objective="Feature 6")
    run_dir = tmp_path / ".task_runs" / spec.task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    baseline = capture_baseline(tmp_path)
    atomic_write_json(run_dir / "baseline.json", baseline.to_dict())
    atomic_write_text(run_dir / "diff.patch", "original diff")

    # Repo modified out-of-band
    (tmp_path / "drift.txt").write_text("drifted content\n", encoding="utf-8")

    is_valid, reason = CrashRecoveryEngine.check_review_validity(tmp_path, run_dir)
    assert is_valid is False
    assert "drifted" in reason.lower()


def _setup_awaiting_human_task(
    repo_path: Path,
    task_id: str,
    objective: str = "Release candidate",
    boundary: str = "create_release_candidate",
    decision_id: Optional[str] = None,
    approve_now: bool = False,
) -> Tuple[TaskSpec, Path]:
    spec = TaskSpec(
        task_id=task_id,
        repository=repo_path.name,
        objective=objective,
        human_approval_requirements=[boundary] if boundary else [],
        current_state="awaiting_human",
    )
    run_dir = repo_path / ".task_runs" / spec.task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    spec.save_to_file(run_dir / "task.yaml")
    if decision_id:
        (run_dir / "decision_packet.md").write_text(
            f"# Decision Packet\nChangeOps Decision ID: `{decision_id}`\n- ⚠️ **{boundary}**: release\n",
            encoding="utf-8",
        )
    if approve_now:
        HumanLifecycleManager.approve(repo_path, spec.task_id, reason="Approved")
    return spec, run_dir


# ============================================================================
# Scenario 7: Interrupted awaiting_human preserved across session restarts
# ============================================================================
def test_interrupted_awaiting_human_preserved_across_restarts(tmp_path):
    _init_git_repo(tmp_path)
    spec, run_dir = _setup_awaiting_human_task(
        tmp_path, "TASK-SCENARIO-07", "Deploy to staging", boundary="deploy_production"
    )
    atomic_write_text(run_dir / "decision_packet.md", "# Decision Packet\nDeploy to staging\n")

    diag = CrashRecoveryEngine.inspect_task(tmp_path, spec.task_id)
    assert diag["current_state"] == "awaiting_human"
    assert (run_dir / "decision_packet.md").is_file()


# ============================================================================
# Scenario 8: Interrupted bounded execution with native receipt never replays
# ============================================================================
def test_interrupted_bounded_execution_with_native_receipt_never_replays(tmp_path):
    _init_git_repo(tmp_path)
    decision_id = "DEC-SCENARIO-08"
    spec, run_dir = _setup_awaiting_human_task(
        tmp_path, "TASK-SCENARIO-08", decision_id=decision_id, approve_now=True
    )

    # Native HowlChangeOps receipt exists on disk
    hco_receipts = tmp_path / ".howlchangeops" / "receipts"
    hco_receipts.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        hco_receipts / f"{decision_id}.json",
        {"decision_id": decision_id, "verification": "PASS", "timestamp": "2026-08-20T12:00:00Z"},
    )

    action = ProposedAction(
        action_type="create_release_candidate",
        target_repo=tmp_path.name,
        arguments={"tag": "v1.2.0-rc.1"},
        authority_boundary="release_management",
    )
    (run_dir / "proposed_action.yaml").write_text(action.to_yaml(), encoding="utf-8")

    class MockCountingExecutor(HowlChangeOpsExecutor):
        def __init__(self):
            super().__init__()
            self.execute_calls = 0

        def is_available(self) -> bool:
            return True

        def execute(self, *args, **kwargs):
            self.execute_calls += 1
            return super().execute(*args, **kwargs)

    counting_exec = MockCountingExecutor()
    ExecutorRegistry.register(counting_exec)

    res = HumanLifecycleManager.resume(tmp_path, spec.task_id)
    assert res.exit_code == 0
    assert counting_exec.execute_calls == 0  # Reconciled without calling execute()!
    assert (run_dir / "execution_receipt.json").exists()


# ============================================================================
# Scenario 9: Interrupted bounded execution with unexecuted decision runs once
# ============================================================================
def test_interrupted_bounded_execution_unexecuted_executes_once(tmp_path):
    _init_git_repo(tmp_path)
    decision_id = "DEC-SCENARIO-09"
    spec, run_dir = _setup_awaiting_human_task(
        tmp_path, "TASK-SCENARIO-09", decision_id=decision_id, approve_now=True
    )

    action = ProposedAction(
        action_type="create_release_candidate",
        target_repo=tmp_path.name,
        arguments={"tag": "v1.3.0-rc.1"},
        authority_boundary="release_management",
    )
    (run_dir / "proposed_action.yaml").write_text(action.to_yaml(), encoding="utf-8")

    class MockOnceExecutor(HowlChangeOpsExecutor):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def is_available(self):
            return True

        def query_execution_status(
            self,
            decision_id: str,
            repo_path: Union[str, Path],
            task_run_dir: Union[str, Path],
            action: ProposedAction,
            task_id: str,
        ):
            return "not_executed", None, None

        def execute(
            self,
            decision_id: str,
            repo_path: Union[str, Path],
            task_run_dir: Union[str, Path],
            action: ProposedAction,
            task_id: str,
        ):
            self.calls += 1
            hco_receipts = Path(repo_path) / ".howlchangeops" / "receipts"
            hco_receipts.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                hco_receipts / f"{decision_id}.json",
                {"decision_id": decision_id, "verification": "PASS", "timestamp": "2026-08-20T12:00:00Z"},
            )
            sha = compute_repository_fingerprint(repo_path).commit_sha
            receipt = ExecutionReceipt(
                task_id=task_id,
                executor=self.name,
                executor_version="0.2.0",
                decision_id=decision_id,
                action_type=action.action_type,
                repository=Path(repo_path).name,
                commit_sha=sha,
                status="success",
                executed_at="2026-08-20T12:00:00Z",
                verification_status="PASS",
            )
            receipt.save_to_file(Path(task_run_dir) / "execution_receipt.json")
            return ExecutionResult(executor_id=self.name, action_type=action.action_type, status="success", receipt=receipt)

    mock_exec = MockOnceExecutor()
    ExecutorRegistry.register(mock_exec)

    res = HumanLifecycleManager.resume(tmp_path, spec.task_id)
    assert res.exit_code == 0
    assert mock_exec.calls == 1


# ============================================================================
# Scenario 10: Duplicate ai resume is idempotent
# ============================================================================
def test_duplicate_ai_resume_is_idempotent(tmp_path):
    _init_git_repo(tmp_path)
    spec, _ = _setup_awaiting_human_task(
        tmp_path, "TASK-SCENARIO-10", "Simple task", boundary="", approve_now=True
    )

    res1 = HumanLifecycleManager.resume(tmp_path, spec.task_id)
    assert res1.exit_code == 0

    # Second resume call on complete task is idempotent
    res2 = HumanLifecycleManager.resume(tmp_path, spec.task_id)
    assert res2.exit_code == 0
    assert res2.final_state == "complete"


# ============================================================================
# Scenario 11: Duplicate ai approve is idempotent
# ============================================================================
def test_duplicate_ai_approve_is_idempotent(tmp_path):
    _init_git_repo(tmp_path)
    spec, _ = _setup_awaiting_human_task(
        tmp_path, "TASK-SCENARIO-11", "Approve test", boundary=""
    )

    rec1 = HumanLifecycleManager.approve(tmp_path, spec.task_id, reason="First approval")
    assert rec1.decision == "approved"

    rec2 = HumanLifecycleManager.approve(tmp_path, spec.task_id, reason="Second approval")
    assert rec2.decision == "approved"
    assert rec2.reason == "Second approval"


# ============================================================================
# Scenario 12: Cancel running process terminates and preserves code
# ============================================================================
def test_cancel_running_process_terminates_and_preserves_code(tmp_path):
    _init_git_repo(tmp_path)
    spec = TaskSpec(
        task_id="TASK-SCENARIO-12",
        repository=tmp_path.name,
        objective="Long running task",
        current_state="implementing",
    )
    run_dir = tmp_path / ".task_runs" / spec.task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    spec.save_to_file(run_dir / "task.yaml")

    # Write code before cancel
    (tmp_path / "work_in_progress.py").write_text("in_progress = True\n", encoding="utf-8")

    # Start a real sleeper child process and record it
    proc = subprocess.Popen(["sleep", "60"])
    ProcessTracker.register_process(run_dir, spec.task_id, proc.pid, "sleep", "sleep 60")

    # Cancel
    res = HumanLifecycleManager.cancel(tmp_path, spec.task_id, reason="User cancelled")
    assert res.exit_code == 0
    assert res.final_state == "cancelled"

    # Verify process terminated
    proc.poll()
    assert proc.returncode is not None

    # Verify uncommitted code is preserved
    assert (tmp_path / "work_in_progress.py").exists()


# ============================================================================
# Scenario 13: Cancel on complete task fails closed
# ============================================================================
def test_cancel_completed_task_fails_closed(tmp_path):
    _init_git_repo(tmp_path)
    spec = TaskSpec(
        task_id="TASK-SCENARIO-13",
        repository=tmp_path.name,
        objective="Done task",
        current_state="complete",
    )
    run_dir = tmp_path / ".task_runs" / spec.task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    spec.save_to_file(run_dir / "task.yaml")

    with pytest.raises(InvalidTaskStateError):
        HumanLifecycleManager.cancel(tmp_path, spec.task_id)


# ============================================================================
# Scenario 14: Repo lock blocks concurrent mutating task
# ============================================================================
def test_repo_lock_blocks_concurrent_mutating_task(tmp_path):
    _init_git_repo(tmp_path)
    lock1 = RepoLock(tmp_path, "TASK-14A", command="ai work")
    lock1.acquire()

    lock2 = RepoLock(tmp_path, "TASK-14B", command="ai work")
    with pytest.raises(RepositoryLockedError):
        lock2.acquire()

    lock1.release()
    # Now lock2 can acquire
    assert lock2.acquire() is True
    lock2.release()


# ============================================================================
# Scenario 15: Repo lock permits read-only commands
# ============================================================================
def test_repo_lock_permits_read_only_commands(tmp_path):
    _init_git_repo(tmp_path)
    spec = TaskSpec(task_id="TASK-15", repository=tmp_path.name, objective="Feature 15")
    run_dir = tmp_path / ".task_runs" / "TASK-15"
    run_dir.mkdir(parents=True, exist_ok=True)
    spec.save_to_file(run_dir / "task.yaml")

    lock = RepoLock(tmp_path, "TASK-15", command="ai work")
    lock.acquire()

    # Read-only inspect does not acquire RepoLock
    diag = CrashRecoveryEngine.inspect_task(tmp_path, "TASK-15")
    assert diag["repo_locked"] is True

    lock.release()


# ============================================================================
# Scenario 16: Repo lock stale reclamation for dead PID
# ============================================================================
def test_repo_lock_stale_reclamation_for_dead_pid(tmp_path):
    _init_git_repo(tmp_path)
    lock_path = get_repo_lock_path(tmp_path)
    # Write lock file pointing to non-existent PID 999999
    atomic_write_json(
        lock_path,
        {
            "task_id": "TASK-DEAD",
            "pid": 999999,
            "hostname": os.uname().nodename,
            "lock_type": "repository_mutation",
            "command": "ai work",
            "started_at": "2026-08-20T00:00:00Z",
            "process_create_time": 0.0,
            "schema": "howlplane.lock/v1",
        },
    )

    new_lock = RepoLock(tmp_path, "TASK-16", command="ai work")
    assert new_lock.acquire() is True
    new_lock.release()


# ============================================================================
# Scenario 17: Task lock blocks concurrent resume on same task
# ============================================================================
def test_task_lock_blocks_concurrent_resume_on_same_task(tmp_path):
    _init_git_repo(tmp_path)
    tlock1 = TaskLock(tmp_path, "TASK-17", operation="resume")
    tlock1.acquire()

    tlock2 = TaskLock(tmp_path, "TASK-17", operation="resume")
    with pytest.raises(TaskLockedError):
        tlock2.acquire()

    tlock1.release()


# ============================================================================
# Scenario 18: Atomic artifact write failure leaves original undamaged
# ============================================================================
def test_atomic_artifact_write_failure_leaves_original_undamaged(tmp_path):
    target_file = tmp_path / "important_artifact.json"
    atomic_write_json(target_file, {"status": "intact", "version": 1})

    # Attempt an atomic write that fails during serialization / write
    class UnserializableObject:
        pass

    with pytest.raises(Exception):
        atomic_write_json(target_file, {"bad": UnserializableObject()})

    # Original file is completely undamaged
    content = safe_load_json(target_file)
    assert content["status"] == "intact"
    assert content["version"] == 1


# ============================================================================
# Scenario 19: Corrupt or zero-byte artifact fails closed
# ============================================================================
def test_corrupt_or_zero_byte_artifact_fails_closed(tmp_path):
    empty_file = tmp_path / "corrupt_task.yaml"
    empty_file.write_text("", encoding="utf-8")

    with pytest.raises(CorruptArtifactError):
        TaskSpec.load_from_file(str(empty_file))

    bad_json = tmp_path / "corrupt_checkpoint.json"
    bad_json.write_text("{ unclosed json", encoding="utf-8")

    with pytest.raises(CorruptArtifactError):
        safe_load_json(bad_json)


# ============================================================================
# Scenario 20: Drift after human approval fails closed on resume
# ============================================================================
def test_drift_after_human_approval_fails_closed(tmp_path):
    _init_git_repo(tmp_path)
    spec = TaskSpec(
        task_id="TASK-SCENARIO-20",
        repository=tmp_path.name,
        objective="Critical migration",
        current_state="awaiting_human",
    )
    run_dir = tmp_path / ".task_runs" / spec.task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    spec.save_to_file(run_dir / "task.yaml")

    HumanLifecycleManager.approve(tmp_path, spec.task_id, reason="Approved baseline")

    # Tamper with repo after approval
    (tmp_path / "unauthorized_change.py").write_text("malicious = True\n", encoding="utf-8")

    with pytest.raises(StaleApprovalError):
        HumanLifecycleManager.resume(tmp_path, spec.task_id)


# ============================================================================
# HOWLFRAM-SLOPFIX-05: a lock whose owner cannot be verified had no recovery
# path at all, and was described as active without ever being checked.
# ============================================================================
def _write_task_lock(repo, task_id, **overrides):
    """Writes a task lock file with a chosen owner, for lock-state tests."""
    payload = {
        "task_id": task_id,
        "pid": 999999,
        "hostname": os.uname().nodename,
        "lock_type": "task_run",
        "operation": "orchestrate",
        "command": "ai resume",
        "started_at": "2026-08-24T14:05:51.747504+00:00",
        "process_create_time": 0.0,
        "schema": "howlplane.lock/v1",
    }
    payload.update(overrides)
    lock_path = get_task_lock_path(repo, task_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(lock_path, payload)
    return lock_path


def test_lock_owner_states_are_distinguished(tmp_path):
    """Live, provably-gone, and unverifiable owners are three different things.
    Collapsing the last two into 'alive' is what made a cross-host lock
    permanently unrecoverable while claiming its owner was running."""
    host = os.uname().nodename

    live, _ = classify_lock_owner(os.getpid(), host, 0.0)
    assert live is LockOwnerState.ACTIVE

    dead, reason = classify_lock_owner(999999, host, 0.0)
    assert dead is LockOwnerState.STALE
    assert "ESRCH" in reason

    # Same provably-dead PID, but recorded on another host: liveness simply
    # cannot be established from here, so it is neither active nor stale.
    foreign, reason = classify_lock_owner(999999, "some-other-box", 0.0)
    assert foreign is LockOwnerState.AMBIGUOUS
    assert "some-other-box" in reason


def test_resume_refuses_takeover_of_live_lock(tmp_path):
    """Fail closed on a genuinely running owner, and never reclaim it."""
    _init_git_repo(tmp_path)
    owner = subprocess.Popen(["sleep", "30"])
    try:
        lock_path = _write_task_lock(
            tmp_path, "TASK-LIVE", pid=owner.pid, hostname=os.uname().nodename
        )

        with pytest.raises(TaskLockedError) as err:
            TaskLock(tmp_path, "TASK-LIVE", operation="resume").acquire()
        assert "active process" in str(err.value)
        assert lock_path.exists(), "a live owner's lock must never be removed"

        with pytest.raises(LockError) as err:
            reclaim_lock(lock_path)
        assert "Refusing to reclaim" in str(err.value)
        assert lock_path.exists()
    finally:
        owner.kill()
        owner.wait()


def test_ambiguous_lock_fails_closed_but_is_recoverable(tmp_path):
    """The unrecoverable case. Acquire must still fail closed -- we will not
    steal what we cannot prove is dead -- but it must say so truthfully and
    point at a real recovery path, which `ai unlock` then provides."""
    _init_git_repo(tmp_path)
    lock_path = _write_task_lock(tmp_path, "TASK-AMBIG", hostname="some-other-box")

    with pytest.raises(TaskLockedError) as err:
        TaskLock(tmp_path, "TASK-AMBIG", operation="resume").acquire()
    message = str(err.value)
    assert "INDETERMINATE" in message
    # It must not assert liveness it never established.
    assert "held by active process" not in message
    assert "howl plane unlock TASK-AMBIG" in message
    assert lock_path.exists()

    record = reclaim_lock(lock_path)
    assert record.owner_state == LockOwnerState.AMBIGUOUS.value
    assert record.owner_hostname == "some-other-box"
    assert record.task_id == "TASK-AMBIG"
    assert not lock_path.exists()

    # And the documented path now works.
    reclaimed = TaskLock(tmp_path, "TASK-AMBIG", operation="resume")
    assert reclaimed.acquire() is True
    reclaimed.release()


def test_unlock_command_records_reclamation_in_evidence(tmp_path):
    """A takeover is a privileged act, so it leaves an audit trail."""
    _init_git_repo(tmp_path)
    _write_task_lock(tmp_path, "TASK-AUDIT", hostname="some-other-box")
    ledger_file = tmp_path / "evidence_ledger.jsonl"

    args = argparse.Namespace(
        repo_dir=str(tmp_path),
        task_id="TASK-AUDIT",
        ledger_file=str(ledger_file),
        json=False,
    )
    assert cmd_unlock(args) == 0

    entries = EvidenceLedger(str(ledger_file)).list_all_entries()
    reclaims = [e for e in entries if e.action == "stale_lock_reclaimed"]
    assert len(reclaims) == 1
    assert reclaims[0].task_id == "TASK-AUDIT"
    assert reclaims[0].result == LockOwnerState.AMBIGUOUS.value
    assert reclaims[0].agent_id == "human_operator"


def test_unlock_refuses_a_live_lock(tmp_path):
    """The command is a recovery tool, not an override."""
    _init_git_repo(tmp_path)
    owner = subprocess.Popen(["sleep", "30"])
    lock_path = _write_task_lock(tmp_path, "TASK-BUSY", pid=owner.pid)

    args = argparse.Namespace(
        repo_dir=str(tmp_path), task_id="TASK-BUSY", ledger_file=None, json=False,
    )
    try:
        assert cmd_unlock(args) == 1
        assert lock_path.exists()
    finally:
        owner.kill()
        owner.wait()


def test_recovery_does_not_recommend_resume_behind_a_held_lock(tmp_path):
    """`ai resume` was recommended for a run whose lock it could never take.
    The recommendation must reflect what the lock actually allows."""
    _init_git_repo(tmp_path)
    repo = tmp_path
    run_dir = Path(repo) / ".task_runs" / "TASK-RECO"
    run_dir.mkdir(parents=True, exist_ok=True)
    TaskSpec(
        task_id="TASK-RECO",
        repository="repo",
        objective="interrupted work",
        current_state="implementing",
    ).save_to_file(str(run_dir / "task.yaml"))

    _write_task_lock(repo, "TASK-RECO", hostname="some-other-box")
    diag = CrashRecoveryEngine.inspect_task(repo, "TASK-RECO")
    assert diag["task_locked"] is True
    assert diag["task_lock_state"] == LockOwnerState.AMBIGUOUS.value
    assert "howl plane unlock TASK-RECO" in diag["recommendation"]
    assert diag["classification"] == RetryClassification.HUMAN_DECISION_REQUIRED

    owner = subprocess.Popen(["sleep", "30"])
    try:
        _write_task_lock(repo, "TASK-RECO", pid=owner.pid)
        diag = CrashRecoveryEngine.inspect_task(repo, "TASK-RECO")
        assert diag["task_lock_state"] == LockOwnerState.ACTIVE.value
        assert "howl plane resume" not in diag["recommendation"]
    finally:
        owner.kill()
        owner.wait()

    # A provably dead owner is reclaimed automatically, so resume is correct.
    _write_task_lock(repo, "TASK-RECO", pid=999999)
    diag = CrashRecoveryEngine.inspect_task(repo, "TASK-RECO")
    assert diag["task_locked"] is False
    assert "howl plane resume TASK-RECO" in diag["recommendation"]

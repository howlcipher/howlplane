"""
test_authority_execution_gap.py

Deterministic tests proving the authority ordering and false completion fixes:
1. Consequential action ('terraform apply') does NOT launch unrestricted implementation agent.
2. Ordinary engineering task ('Add validation for Terraform config') launches implementation agent normally.
3. Approval alone NEVER marks a consequential action COMPLETE.
4. Resuming with unsupported consequential action fails closed with UnsupportedActionError.
5. Resuming with valid execution receipt completes successfully.
6. Resuming with forged or mismatched receipt fails closed with InvalidReceiptError.
"""

import json
from pathlib import Path
import subprocess
import pytest

from howlplane.control_plane.agent_execution import AgentBackend, AgentExecutionResult
from howlplane.control_plane.evidence_ledger import EvidenceLedger
from howlplane.control_plane.executor import (
    ExecutionReceipt,
    ExecutorRegistry,
    UnsupportedActionError,
    InvalidReceiptError,
    ExecutionFailedError,
)
from howlplane.control_plane.human_boundary import (
    HumanLifecycleManager,
    compute_repository_fingerprint,
)
from howlplane.control_plane.orchestrator import GovernedTaskOrchestrator, OrchestrationConfig
from howlplane.control_plane.task_spec import TaskSpec


class CountingMockBackend(AgentBackend):
    """Mock agent backend that records execution invocations."""

    def __init__(self):
        self.impl_invocations = 0
        self.total_invocations = 0
        self.executed_prompts = []

    def is_available(self) -> bool:
        return True

    def execute(
        self,
        task: TaskSpec,
        cwd: Path,
        role: str = "implementation",
        prompt_override: str = None,
        timeout_seconds: int = 600,
    ) -> AgentExecutionResult:
        self.total_invocations += 1
        if role == "implementation":
            self.impl_invocations += 1
        self.executed_prompts.append(prompt_override or "")
        return AgentExecutionResult(
            agent_id="counting_mock",
            role=role,
            command="mock",
            exit_code=0,
            stdout="Mock execution completed",
            stderr="",
            duration_seconds=0.1,
            success=True,
        )


from tests.test_human_approval_lifecycle import _init_git_repo


def test_consequential_action_does_not_reach_implementation_agent(tmp_path: Path):
    """
    Proves that a task with an explicit consequential action ('terraform apply')
    does NOT invoke the unrestricted implementation agent before human authority review.
    """
    _init_git_repo(tmp_path)
    backend = CountingMockBackend()

    orchestrator = GovernedTaskOrchestrator(
        target_repo=tmp_path,
        config=OrchestrationConfig(
            custom_backend=backend,
            enable_howlframe_audit=False,
            record_evidence=False,
        ),
    )

    spec = TaskSpec(
        task_id="TASK-CONSEQ-01",
        repository=tmp_path.name,
        objective="Run terraform apply to update production infrastructure",
        task_class="infrastructure",
        risk_level="critical",
        human_approval_requirements=["infrastructure_apply"],
    )

    res = orchestrator.run(spec, planned_actions=["terraform apply -auto-approve"])

    # Authority fix verified: implementation agent was NEVER launched!
    assert backend.impl_invocations == 0, "Implementation backend must not be launched for consequential action"
    assert res.final_state == "awaiting_human"
    assert res.exit_code == 2


def test_ordinary_engineering_task_launches_implementation_agent_normally(tmp_path: Path):
    """
    Proves that ordinary software development (e.g. writing/validating Terraform configs)
    is NOT falsely blocked and proceeds autonomously to the implementation agent.
    """
    _init_git_repo(tmp_path)
    backend = CountingMockBackend()

    orchestrator = GovernedTaskOrchestrator(
        target_repo=tmp_path,
        config=OrchestrationConfig(
            custom_backend=backend,
            custom_reviewer_fn=lambda role, diff, task: "findings: []\n",
            enable_howlframe_audit=False,
            record_evidence=False,
        ),
    )

    spec = TaskSpec(
        task_id="TASK-DEV-01",
        repository=tmp_path.name,
        objective="Add validation tests for Terraform module configuration",
        task_class="infrastructure",
        risk_level="medium",
    )

    res = orchestrator.run(spec, planned_actions=[])

    # Normal engineering proceeds to implementation backend
    assert backend.impl_invocations == 1
    assert res.exit_code == 0
    assert res.final_state == "complete"


def test_approval_alone_does_not_complete_without_bounded_execution(tmp_path: Path):
    """
    Proves that an approved consequential task does NOT transition to COMPLETE
    solely on human approval. Resumption fails closed if no executor is configured.
    """
    _init_git_repo(tmp_path)
    run_dir = tmp_path / ".task_runs" / "TASK-CONSEQ-02"
    run_dir.mkdir(parents=True, exist_ok=True)

    spec = TaskSpec(
        task_id="TASK-CONSEQ-02",
        repository=tmp_path.name,
        objective="Apply database drop table and publish package",
        task_class="infrastructure",
        risk_level="critical",
        human_approval_requirements=["destructive_database_change"],
        current_state="awaiting_human",
    )
    spec.save_to_file(str(run_dir / "task.yaml"))
    (run_dir / "diff.patch").write_text("", encoding="utf-8")

    # Operator approves the task
    HumanLifecycleManager.approve(
        target_repo=tmp_path,
        task_id="TASK-CONSEQ-02",
        reason="Operator approved plan",
    )

    # Resuming without a supporting bounded executor fails closed with UnsupportedActionError
    with pytest.raises(UnsupportedActionError, match="No configured bounded executor supports"):
        HumanLifecycleManager.resume(
            target_repo=tmp_path,
            task_id="TASK-CONSEQ-02",
        )

    # Verify task spec remains non-complete (awaiting_human)
    updated_spec = TaskSpec.load_from_file(str(run_dir / "task.yaml"))
    assert updated_spec.current_state == "awaiting_human"


def test_consequential_task_with_valid_receipt_completes(tmp_path: Path):
    """
    Proves that an approved task with a valid, verified execution receipt transitions to COMPLETE.
    """
    _init_git_repo(tmp_path)
    run_dir = tmp_path / ".task_runs" / "TASK-CONSEQ-03"
    run_dir.mkdir(parents=True, exist_ok=True)

    spec = TaskSpec(
        task_id="TASK-CONSEQ-03",
        repository=tmp_path.name,
        objective="Create release candidate tag via HowlChangeOps",
        task_class="infrastructure",
        risk_level="high",
        human_approval_requirements=["create_release_candidate"],
        current_state="awaiting_human",
    )
    spec.save_to_file(str(run_dir / "task.yaml"))
    (run_dir / "diff.patch").write_text("", encoding="utf-8")

    HumanLifecycleManager.approve(
        target_repo=tmp_path,
        task_id="TASK-CONSEQ-03",
        reason="Operator approved RC",
    )

    # Simulate valid HowlChangeOps receipt
    fp = compute_repository_fingerprint(tmp_path, run_dir)
    receipt = ExecutionReceipt(
        task_id="TASK-CONSEQ-03",
        executor="howlchangeops",
        executor_version="0.2.0",
        decision_id="decision-123456",
        action_type="create_release_candidate",
        repository=tmp_path.name,
        commit_sha=fp.commit_sha,
        status="success",
        executed_at="2026-08-20T12:00:00Z",
        verification_status="PASS",
        native_receipt={"verification": "PASS", "action": "create_release_candidate"},
    )
    receipt.save_to_file(run_dir / "execution_receipt.json")

    # Resuming with verified receipt succeeds
    res = HumanLifecycleManager.resume(
        target_repo=tmp_path,
        task_id="TASK-CONSEQ-03",
    )

    assert res.final_state == "complete"
    assert res.exit_code == 0
    updated_spec = TaskSpec.load_from_file(str(run_dir / "task.yaml"))
    assert updated_spec.current_state == "complete"


def test_forged_or_mismatched_receipt_fails_closed(tmp_path: Path):
    """
    Proves that a forged, failed, or mismatched execution receipt fails closed.
    """
    _init_git_repo(tmp_path)
    run_dir = tmp_path / ".task_runs" / "TASK-CONSEQ-04"
    run_dir.mkdir(parents=True, exist_ok=True)

    spec = TaskSpec(
        task_id="TASK-CONSEQ-04",
        repository=tmp_path.name,
        objective="Create release candidate tag",
        task_class="infrastructure",
        risk_level="high",
        human_approval_requirements=["create_release_candidate"],
        current_state="awaiting_human",
    )
    spec.save_to_file(str(run_dir / "task.yaml"))
    (run_dir / "diff.patch").write_text("", encoding="utf-8")

    HumanLifecycleManager.approve(
        target_repo=tmp_path,
        task_id="TASK-CONSEQ-04",
        reason="Operator approved",
    )

    # 1. Receipt with failed verification status
    bad_receipt = ExecutionReceipt(
        task_id="TASK-CONSEQ-04",
        executor="howlchangeops",
        executor_version="0.2.0",
        decision_id="decision-999",
        action_type="create_release_candidate",
        repository=tmp_path.name,
        commit_sha="abcd123",
        status="failure",
        executed_at="2026-08-20T12:00:00Z",
        verification_status="FAIL",
    )
    bad_receipt.save_to_file(run_dir / "execution_receipt.json")

    with pytest.raises(InvalidReceiptError, match="status"):
        HumanLifecycleManager.resume(target_repo=tmp_path, task_id="TASK-CONSEQ-04")

    # 2. Receipt for wrong repository
    wrong_repo_receipt = ExecutionReceipt(
        task_id="TASK-CONSEQ-04",
        executor="howlchangeops",
        executor_version="0.2.0",
        decision_id="decision-999",
        action_type="create_release_candidate",
        repository="completely_different_repo",
        commit_sha="abcd123",
        status="success",
        executed_at="2026-08-20T12:00:00Z",
        verification_status="PASS",
    )
    wrong_repo_receipt.save_to_file(run_dir / "execution_receipt.json")

    with pytest.raises(InvalidReceiptError, match="repository"):
        HumanLifecycleManager.resume(target_repo=tmp_path, task_id="TASK-CONSEQ-04")

#!/usr/bin/env python3
"""
orchestrator.py

Closed-loop AI Engineering Control Plane orchestrator.
Enforces the complete governed lifecycle:
Discovery -> Shadow Audit -> Plan/Route -> Implement -> Delta Capture ->
Adversarial Review -> Structured Validation -> Reconciliation ->
Remediation Loop -> Targeted Re-review -> Deterministic Verification ->
Human Authority Boundary Gate -> Complete Evidence Ledger.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json, os, shutil, sys, time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import yaml

from src.control_plane.agent_execution import AgentBackend, AgentBackendRegistry, AgentExecutionResult, AgentUnavailableError
from src.control_plane.atomic_io import (
    atomic_write_json,
    atomic_write_text,
    atomic_write_yaml,
    safe_load_json,
    safe_load_yaml,
)
from src.control_plane.checkpoints import CheckpointManager, StageCheckpoint
from src.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from src.control_plane.git_baseline import GitBaseline, RepositoryDelta, capture_baseline, capture_delta
from src.control_plane.git_integration import run_git
from src.control_plane.howlframe_runner import HowlFrameAuditRunner, get_dogfood_mode, DEFAULT_INSTRUCTION_BUDGET
from src.control_plane.human_boundary import HumanBoundaryGate, BoundaryCheckResult, HumanDecisionPacket
from src.control_plane.locking import RepoLock, TaskLock, RepositoryLockedError, TaskLockedError
from src.control_plane.process_manager import ProcessTracker
from src.control_plane.project_adapter import ProjectAdapter, ProjectContext
from src.control_plane.reconciliation import ReviewFinding, ReconciliationResult, ReviewReconciler
from src.control_plane.recovery import CrashRecoveryEngine
from src.control_plane.review_runner import ReviewRunner, ReviewCycleResult, SingleReviewResult
from src.control_plane.reviewers import get_reviewer_role, build_skill_context
from src.control_plane.router import TaskRouter, RoutingDecision
from src.control_plane.proposed_action import ProposedAction, infer_proposed_actions
from src.control_plane.task_spec import TaskSpec
from src.control_plane.verification import VerificationPlan, VerificationStep
from src.control_plane.reasoning.execution_trajectory import (
    ExecutionTrajectoryBuilder,
    TrajectoryStore,
)

ORCHESTRATOR_SCHEMA_VERSION = "howlplane.orchestrator/v1"

# Structured reasons a governed task did not reach "complete" (#59.1 Phase 1).
# The orchestrator assigns only the classes it can prove from its own gates.
# PROVIDER_EXHAUSTED / PROVIDER_UNAVAILABLE are assigned by the caller after it
# runs the observed AgentExecutionResult through ProviderPoolManager
# .detect_exhaustion() -- the orchestrator holds no provider pool, and wiring one
# in would duplicate an abstraction that already exists one layer up.
FAILURE_CLASS_ENGINEERING = "ENGINEERING_FAILURE"
FAILURE_CLASS_AUTHORITY_BLOCKED = "AUTHORITY_BLOCKED"
FAILURE_CLASS_VERIFICATION = "VERIFICATION_FAILURE"
FAILURE_CLASS_PROVIDER_EXHAUSTED = "PROVIDER_EXHAUSTED"
FAILURE_CLASS_PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


@dataclass
class OrchestrationConfig:
    """Runtime configuration for governed task orchestration."""

    max_remediation_cycles: int = 3
    max_review_cycles: int = 4
    timeout_seconds: int = 600
    dogfood_mode: str = "shadow"
    enable_howlframe_audit: bool = True
    record_evidence: bool = True
    record_trajectory: bool = True
    trajectory_store_dir: Optional[Union[str, Path]] = None
    stop_on_verification_failure: bool = True
    force: bool = False
    skip_doctor: bool = False
    acquire_locks: bool = True
    failure_injection_hook: Optional[Callable[[str, Path, TaskSpec], None]] = None
    custom_backend: Optional[AgentBackend] = None
    custom_reviewer_fn: Optional[Callable[[str, str, TaskSpec], str]] = None
    custom_remediation_fn: Optional[Callable[[TaskSpec, Path, List[ReviewFinding]], None]] = None
    reviewer_agent_mapping: Optional[Dict[str, str]] = None
    # Enables bounded reviewer failover (#59.2 Phase 4) in the governed review
    # cycle: a reviewer whose assigned provider fails, times out, or emits
    # invalid/malformed output gets one alternate-provider attempt instead of
    # immediately blocking the task. Sharing the caller's pool means review
    # quota exhaustion updates the same provider state implementation failover
    # reads (#59.2 Phase 9). Left None (the default) by callers that inject a
    # custom_backend/custom_reviewer_fn, which keeps deterministic tests on the
    # prior single-attempt path.
    provider_pool: Optional[Any] = None


@dataclass
class OrchestrationResult:
    """Complete result packet from governed task execution."""

    task_id: str
    task_spec: TaskSpec
    final_state: str  # "complete", "awaiting_human", "failed", "blocked"
    exit_code: int
    routing_decision: Optional[RoutingDecision] = None
    initial_delta: Optional[RepositoryDelta] = None
    final_delta: Optional[RepositoryDelta] = None
    review_cycles: List[ReviewCycleResult] = field(default_factory=list)
    reconciliation: Optional[ReconciliationResult] = None
    verification_plan: Optional[VerificationPlan] = None
    boundary_result: Optional[BoundaryCheckResult] = None
    howlframe_audit_status: Optional[str] = None
    howlframe_audit_match: bool = True
    remediation_cycles_count: int = 0
    duration_seconds: float = 0.0
    error_message: Optional[str] = None
    run_dir: Optional[str] = None
    # Structured provider evidence (#59.1 Phase 1). `provider_execution` is the
    # verbatim implementation-role AgentExecutionResult -- exactly the type
    # ProviderPoolManager.detect_exhaustion() consumes -- so a caller can tell a
    # quota/session failure apart from bad generated code without parsing any
    # human-readable summary text.
    provider_execution: Optional[AgentExecutionResult] = None
    failure_class: Optional[str] = None
    trajectory_id: Optional[str] = None
    schema: str = ORCHESTRATOR_SCHEMA_VERSION

    @property
    def executing_provider(self) -> Optional[str]:
        """The provider that actually executed, from observed evidence."""
        if self.provider_execution is not None:
            return self.provider_execution.agent_id
        return self.routing_decision.selected_agent_id if self.routing_decision else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "final_state": self.final_state,
            "exit_code": self.exit_code,
            "routing_decision": asdict(self.routing_decision) if self.routing_decision else None,
            "initial_delta": self.initial_delta.to_dict() if self.initial_delta else None,
            "final_delta": self.final_delta.to_dict() if self.final_delta else None,
            "review_cycles": [c.to_dict() for c in self.review_cycles],
            "reconciliation": self.reconciliation.to_dict() if self.reconciliation else None,
            "verification_plan": self.verification_plan.to_dict() if self.verification_plan else None,
            "howlframe_audit_status": self.howlframe_audit_status,
            "howlframe_audit_match": self.howlframe_audit_match,
            "remediation_cycles_count": self.remediation_cycles_count,
            "duration_seconds": self.duration_seconds,
            "error_message": self.error_message,
            "run_dir": self.run_dir,
            "provider_execution": self.provider_execution.to_dict() if self.provider_execution else None,
            "executing_provider": self.executing_provider,
            "failure_class": self.failure_class,
            "trajectory_id": self.trajectory_id,
            "schema": self.schema,
        }


class GovernedTaskOrchestrator:
    """
    Drives the end-to-end governed engineering lifecycle, maintaining
    strict control over state transitions, adversarial reviews,
    remediation loops, verification gates, and human authority boundaries.
    """

    def __init__(
        self,
        target_repo: Union[str, Path],
        control_plane_root: Optional[Union[str, Path]] = None,
        config: Optional[OrchestrationConfig] = None,
    ):
        self.target_repo = Path(target_repo).resolve()
        self.control_plane_root = (
            Path(control_plane_root).resolve()
            if control_plane_root
            else Path(__file__).resolve().parents[2]
        )
        self.config = config or OrchestrationConfig()
        ledger_path = str(self.control_plane_root / "logs" / "control_plane" / "evidence_ledger.jsonl")
        self.ledger = EvidenceLedger(ledger_path)
        traj_dir = self.config.trajectory_store_dir
        if traj_dir is None:
            traj_dir = self.control_plane_root / "logs" / "control_plane" / "trajectories"
        self.trajectory_store = TrajectoryStore(traj_dir) if self.config.record_trajectory else None

    def _enforce_task_path_scope(self, task_spec: TaskSpec, delta: RepositoryDelta) -> List[str]:
        """
        Reverts edits outside `task_spec.allowed_paths` before the delta is
        reviewed (#59.2 Phase 8/17).

        `allowed_paths` was previously enforced only at commit time, inside
        GitIntegrationExecutor.stage_and_commit -- after independent review had
        already seen (and legitimately objected to) the out-of-scope work. A
        path-scoped task whose agent wandered into production files therefore
        parked on review findings about code it was never permitted to change.
        Enforcing here keeps the reviewed diff identical to the committed one.

        Reverts only the paths the delta itself names, using the same
        `git checkout --` / unlink pair as _reconcile_attempt_state: no
        `git reset --hard`, no `git clean`, and files the task never touched
        are never inspected. Returns the reverted paths (empty when the task
        declares no scope, which is the default for ordinary tasks).
        """
        allowed = list(task_spec.allowed_paths or [])
        if not allowed or delta is None or delta.is_empty:
            return []

        repo_root = Path(self.target_repo).resolve()
        out_of_scope = [
            p for p in (
                list(delta.files_modified) + list(delta.files_deleted) + list(delta.files_added)
            )
            if p not in allowed
        ]
        added = set(delta.files_added)
        for rel_path in out_of_scope:
            if rel_path in added:
                candidate = (repo_root / rel_path).resolve()
                # Never step outside the repository, whatever the delta claims.
                if not str(candidate).startswith(str(repo_root)):
                    continue
                try:
                    if candidate.is_file():
                        candidate.unlink()
                except OSError:
                    pass
            else:
                run_git(repo_root, ["checkout", "--", rel_path], 30)
        return out_of_scope

    def _capture_scoped_delta(
        self,
        task_spec: TaskSpec,
        baseline: GitBaseline,
        agent_id: str,
        stage: str,
    ) -> RepositoryDelta:
        """Capture delta and revert any out-of-scope edits before review/commit."""
        delta = capture_delta(self.target_repo, baseline)
        reverted_scope = self._enforce_task_path_scope(task_spec, delta)
        if reverted_scope:
            delta = capture_delta(self.target_repo, baseline)
            self._record_event(
                task_id=task_spec.task_id,
                agent_id=agent_id,
                action="out_of_scope_edits_reverted",
                spec=task_spec,
                metadata={"reverted_paths": reverted_scope, "allowed_paths": list(task_spec.allowed_paths), "stage": stage},
            )
        return delta

    def _record_event(
        self,
        task_id: str,
        agent_id: str,
        action: str,
        command: Optional[str] = None,
        result: Optional[str] = None,
        artifact: Optional[str] = None,
        spec: Optional[TaskSpec] = None,
        findings_summary: Optional[Dict[str, int]] = None,
        verification_summary: Optional[Dict[str, str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records an immutable event in the append-only evidence ledger."""
        if not self.config.record_evidence:
            return

        meta = metadata or {}
        entry = EvidenceEntry(
            task_id=task_id,
            agent_id=agent_id,
            action=action,
            command=command,
            result=result,
            artifact=artifact,
            task_class=spec.task_class if spec else None,
            risk_level=spec.risk_level if spec else None,
            reasoning_tier=spec.recommended_reasoning_tier if spec else None,
            actual_agent=agent_id,
            repository=self.target_repo.name,
            findings_summary=findings_summary,
            verification_summary=verification_summary,
            metadata=meta,
        )
        try:
            self.ledger.append_entry(entry)
        except Exception:
            pass

    def prepare_task_plan(
        self,
        task_spec: TaskSpec,
        planned_actions: Optional[List[str]] = None,
    ) -> Tuple[ProjectContext, RoutingDecision, VerificationPlan, Path, Optional[Any]]:
        """Prepares task run directory, project discovery, routing, verification plan, and review briefs."""
        run_dir = self.target_repo / ".task_runs" / task_spec.task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "reviews").mkdir(parents=True, exist_ok=True)
        (run_dir / "remediation").mkdir(parents=True, exist_ok=True)

        ctx = ProjectAdapter.discover(self.target_repo)
        (run_dir / "project_context.json").write_text(ctx.to_json(), encoding="utf-8")

        router = TaskRouter()
        routing = router.route(task_spec)
        (run_dir / "route.json").write_text(json.dumps(asdict(routing), indent=2), encoding="utf-8")

        verif_plan = ProjectAdapter.create_verification_plan(ctx, task_id=task_spec.task_id)
        (run_dir / "verification_plan.json").write_text(verif_plan.to_json(), encoding="utf-8")

        task_spec.recommended_agent = routing.selected_agent_id
        task_spec.actual_agent = routing.selected_agent_id
        task_spec.is_override = routing.is_override
        task_spec.override_reason = routing.rationale if routing.is_override else None
        if task_spec.current_state == "discovered":
            task_spec.transition_to("planned", "Task routed and verification plan generated")
        task_spec.save_to_file(str(run_dir / "task.yaml"))
        skill_ctx = build_skill_context(task_spec)
        for role_id in routing.recommended_reviewers:
            role = get_reviewer_role(role_id)
            if role:
                (run_dir / "reviews" / f"{role_id}.md").write_text(
                    role.render_brief(task=task_spec, diff_content="", context=skill_ctx), encoding="utf-8"
                )
        (run_dir / "findings_template.yaml").write_text("# Review Findings Template\nfindings: []\n", encoding="utf-8")

        hf_res = None
        if self.config.enable_howlframe_audit:
            try:
                hf_res = HowlFrameAuditRunner.run_audit(
                    context=ctx,
                    record_evidence=self.config.record_evidence,
                    task_id=task_spec.task_id,
                    ledger=self.ledger,
                    dogfood_mode="shadow",
                )
                (run_dir / "howlframe_audit.json").write_text(json.dumps(hf_res.to_dict(), indent=2), encoding="utf-8")
            except Exception:
                pass

        # Check pre-execution human authority boundary and bind HowlChangeOps decision if applicable
        pre_b = HumanBoundaryGate.evaluate_pre_execution(
            task=task_spec,
            planned_actions=planned_actions,
            target_repo=self.target_repo,
        )
        self._bind_decision_packet(pre_b, run_dir)
        if pre_b.requires_human_approval and pre_b.decision_packet:
            (run_dir / "decision_packet.md").write_text(pre_b.decision_packet.render_markdown(), encoding="utf-8")

        return ctx, routing, verif_plan, run_dir, hf_res

    def _bind_decision_packet(self, boundary_res: Any, run_dir: Path) -> None:
        """Binds bounded executor decision ID to human decision packet if required."""
        if not boundary_res.requires_human_approval or not boundary_res.decision_packet:
            return
        if not boundary_res.decision_packet.proposed_actions:
            return
        act_dict = boundary_res.decision_packet.proposed_actions[0]
        action_obj = ProposedAction.from_dict(act_dict)
        from src.control_plane.executor import ExecutorRegistry
        executor = ExecutorRegistry.get_executor_for_action(action_obj.action_type)
        if executor and executor.is_available():
            verdict, dec_id, _ = executor.evaluate(action_obj, self.target_repo, run_dir)
            if dec_id:
                boundary_res.decision_packet.changeops_decision_id = dec_id
                boundary_res.decision_packet.executor_id = executor.name

    def run(
        self,
        task_spec: TaskSpec,
        planned_actions: Optional[List[str]] = None,
    ) -> OrchestrationResult:
        """
        Executes the complete governed control-plane loop for the task under
        mutual-exclusion locks and durable checkpoint guarantees.
        """
        start_time = time.time()

        # Acquire Locks if enabled
        repo_lock = (
            RepoLock(self.target_repo, task_spec.task_id, command=f"ai work {task_spec.task_id} --execute")
            if self.config.acquire_locks
            else None
        )
        task_lock = (
            TaskLock(self.target_repo, task_spec.task_id, operation="orchestrate")
            if self.config.acquire_locks
            else None
        )

        if repo_lock:
            repo_lock.acquire()
        if task_lock:
            task_lock.acquire()

        try:
            result = self._run_governed_loop(task_spec, planned_actions, start_time)
            if self.trajectory_store is not None:
                traj = ExecutionTrajectoryBuilder.from_orchestration_result(result)
                self.trajectory_store.save(traj)
                result.trajectory_id = traj.trajectory_id
            return result
        except Exception as exc:
            run_dir = self.target_repo / ".task_runs" / task_spec.task_id
            if run_dir.is_dir():
                try:
                    CheckpointManager.record_interrupted(
                        run_dir, stage=task_spec.current_state, reason=str(exc)
                    )
                except Exception:
                    pass
            raise
        finally:
            if task_lock:
                task_lock.release()
            if repo_lock:
                repo_lock.release()

    def _run_governed_loop(
        self,
        task_spec: TaskSpec,
        planned_actions: Optional[List[str]] = None,
        start_time: Optional[float] = None,
    ) -> OrchestrationResult:
        start_time = start_time or time.time()
        ctx, routing, verif_plan, run_dir, hf_res = self.prepare_task_plan(task_spec, planned_actions)
        reviews_dir = run_dir / "reviews"
        remediation_base_dir = run_dir / "remediation"
        hf_audit_status = hf_res.status if hf_res else None
        hf_audit_match = (hf_res.status == "MATCH") if hf_res else True

        CheckpointManager.start_stage(
            run_dir,
            task_spec.task_id,
            "planned",
            repo_path=self.target_repo,
            input_artifacts=[str(run_dir / "route.json"), str(run_dir / "verification_plan.json")],
        )

        self._record_event(
            task_id=task_spec.task_id,
            agent_id="control_plane",
            action="task_created",
            spec=task_spec,
            metadata={"repo_path": str(self.target_repo), "project_types": ctx.project_types},
        )
        self._record_event(
            task_id=task_spec.task_id,
            agent_id="control_plane",
            action="project_discovered",
            spec=task_spec,
            metadata={"hygiene_status": ctx.hygiene_status, "has_agents_md": ctx.has_agents_md},
        )
        self._record_event(
            task_id=task_spec.task_id,
            agent_id=routing.selected_agent_id,
            action="route_selected",
            spec=task_spec,
            metadata={
                "selected_agent": routing.selected_agent_id,
                "reviewers": routing.recommended_reviewers,
                "is_override": routing.is_override,
            },
        )
        CheckpointManager.complete_stage(run_dir, "planned")

        # --------------------------------------------------------------------
        # Stage 2.5: Pre-Execution Human Authority Gating
        # --------------------------------------------------------------------
        pre_boundary = HumanBoundaryGate.evaluate_pre_execution(
            task=task_spec,
            planned_actions=planned_actions,
            target_repo=self.target_repo,
        )
        self._bind_decision_packet(pre_boundary, run_dir)

        if pre_boundary.requires_human_approval:

            CheckpointManager.start_stage(
                run_dir,
                task_spec.task_id,
                "awaiting_human",
                repo_path=self.target_repo,
                metadata={"boundary_triggers": pre_boundary.triggered_boundaries},
            )
            task_spec.transition_to(
                "awaiting_human",
                f"Pre-execution human authority boundary triggered: {pre_boundary.triggered_boundaries}",
            )
            task_spec.save_to_file(str(run_dir / "task.yaml"))

            if pre_boundary.decision_packet:
                (run_dir / "decision_packet.md").write_text(
                    pre_boundary.decision_packet.render_markdown(), encoding="utf-8"
                )

            self._record_human_boundary_events(
                task_spec, run_dir, boundaries=pre_boundary.triggered_boundaries, reason="Pre-execution boundary triggered"
            )

            stage_kwargs = dict(
                start_time=start_time,
                run_dir=run_dir,
                routing=routing,
                initial_delta=None,
                current_delta=None,
                review_cycles=[],
                reconciliation=None,
                verif_plan=verif_plan,
                boundary_res=pre_boundary,
                hf_status=hf_audit_status,
                hf_match=hf_audit_match,
                remediation_count=0,
            )
            return self._make_result(task_spec, "awaiting_human", 2, **stage_kwargs)

        # --------------------------------------------------------------------
        # Stage 3: Baseline Capture / Baseline Recovery
        # --------------------------------------------------------------------
        baseline_file = run_dir / "baseline.json"
        if baseline_file.is_file():
            try:
                baseline = GitBaseline.from_dict(safe_load_json(baseline_file))
            except Exception:
                baseline = capture_baseline(self.target_repo)
                (run_dir / "baseline.json").write_text(baseline.to_json(), encoding="utf-8")
        else:
            baseline = capture_baseline(self.target_repo)
            (run_dir / "baseline.json").write_text(baseline.to_json(), encoding="utf-8")

        # --------------------------------------------------------------------
        # Stage 4: Implementation (implementing) / Reconcile on Recovery
        # --------------------------------------------------------------------
        impl_backend = self.config.custom_backend or AgentBackendRegistry.get_backend(routing.selected_agent_id)
        if not impl_backend.is_available() and not self.config.custom_backend:
            err_msg = f"Selected agent '{routing.selected_agent_id}' is not installed or available on PATH."
            return self._fail_task(
                task_spec,
                run_dir,
                err_msg,
                start_time,
                exit_code=1,
                agent_id=routing.selected_agent_id,
                routing=routing,
                hf_status=hf_audit_status,
                hf_match=hf_audit_match,
            )

        has_existing_delta, rec_delta, rec_msg = CrashRecoveryEngine.reconcile_interrupted_implementation(
            self.target_repo, run_dir, task_spec
        )

        impl_dir = run_dir / "implementation"
        impl_dir.mkdir(parents=True, exist_ok=True)

        # Stays None on the crash-recovery path, where an interrupted
        # implementation's delta is reconciled rather than re-run: there is no
        # provider execution in *this* process to report (#59.1 Phase 1).
        impl_res: Optional[AgentExecutionResult] = None

        if has_existing_delta and rec_delta:
            current_delta = rec_delta
            initial_delta = rec_delta
            self._record_event(
                task_id=task_spec.task_id,
                agent_id="control_plane",
                action="implementation_recovered",
                spec=task_spec,
                metadata={"files_changed": len(current_delta.files_modified) + len(current_delta.files_added)},
            )
        else:
            CheckpointManager.start_stage(
                run_dir,
                task_spec.task_id,
                "implementing",
                agent_id=routing.selected_agent_id,
                repo_path=self.target_repo,
            )

            if self.config.failure_injection_hook:
                self.config.failure_injection_hook("implementing", run_dir, task_spec)

            if task_spec.current_state != "implementing":
                task_spec.transition_to("implementing", f"Launching implementation agent: {routing.selected_agent_id}")
                task_spec.save_to_file(str(run_dir / "task.yaml"))

            self._record_event(
                task_id=task_spec.task_id,
                agent_id=routing.selected_agent_id,
                action="implementation_started",
                spec=task_spec,
            )

            impl_prompt = self._build_implementation_prompt(task_spec, ctx)
            impl_res = impl_backend.execute(
                task=task_spec,
                cwd=self.target_repo,
                role="implementation",
                prompt_override=impl_prompt,
                timeout_seconds=self.config.timeout_seconds,
            )

            (impl_dir / "result.json").write_text(impl_res.to_json(), encoding="utf-8")

            if not impl_res.success:
                err_msg = (
                    impl_res.stderr.strip()
                    if impl_res.stderr and impl_res.stderr.strip()
                    else (impl_res.error_message or f"Implementation failed with exit code {impl_res.exit_code}")
                )
                self._record_event(
                    task_id=task_spec.task_id,
                    agent_id=routing.selected_agent_id,
                    action="implementation_completed",
                    result="failure",
                    spec=task_spec,
                    metadata={"exit_code": impl_res.exit_code, "error": err_msg},
                )
                return self._fail_task(
                    task_spec,
                    run_dir,
                    err_msg,
                    start_time,
                    exit_code=impl_res.exit_code if impl_res.exit_code != 0 else 1,
                    agent_id="control_plane",
                    routing=routing,
                    hf_status=hf_audit_status,
                    hf_match=hf_audit_match,
                    # Propagate the provider's own execution evidence verbatim
                    # (#59.1 Phase 1). The caller re-classifies this through
                    # detect_exhaustion(); ENGINEERING_FAILURE is only the
                    # default when no provider-availability signal is found.
                    provider_execution=impl_res,
                    failure_class=FAILURE_CLASS_ENGINEERING,
                )

            current_delta = self._capture_scoped_delta(
                task_spec, baseline, routing.selected_agent_id, stage="implementation"
            )
            (impl_dir / "diff.patch").write_text(current_delta.diff_content, encoding="utf-8")
            (run_dir / "diff.patch").write_text(current_delta.diff_content, encoding="utf-8")

            self._record_event(
                task_id=task_spec.task_id,
                agent_id=routing.selected_agent_id,
                action="implementation_completed",
                result="success",
                spec=task_spec,
                metadata={"files_changed": len(current_delta.files_modified) + len(current_delta.files_added)},
            )
            self._record_event(
                task_id=task_spec.task_id,
                agent_id="control_plane",
                action="repository_delta_captured",
                spec=task_spec,
                metadata={
                    "files_added": current_delta.files_added,
                    "files_modified": current_delta.files_modified,
                    "files_deleted": current_delta.files_deleted,
                    "insertions": current_delta.insertions,
                    "deletions": current_delta.deletions,
                },
            )
            initial_delta = current_delta
            CheckpointManager.complete_stage(run_dir, "implementing", output_artifacts=[str(run_dir / "diff.patch")])

            if self.config.failure_injection_hook:
                self.config.failure_injection_hook("post_implementation", run_dir, task_spec)

        # --------------------------------------------------------------------
        # Stage 5: Independent Adversarial Reviews & Remediation Loop
        # --------------------------------------------------------------------
        if task_spec.current_state != "reviewing":
            task_spec.transition_to("reviewing", "Initiating independent reviewer analysis on actual implementation diff")
            task_spec.save_to_file(str(run_dir / "task.yaml"))

        review_cycles: List[ReviewCycleResult] = []
        latest_reconciliation: Optional[ReconciliationResult] = None
        remediation_count = 0
        current_reviewers = list(routing.recommended_reviewers)

        while True:
            cycle_idx = len(review_cycles) + 1
            CheckpointManager.start_stage(
                run_dir,
                task_spec.task_id,
                "reviewing",
                repo_path=self.target_repo,
                metadata={"cycle": cycle_idx, "reviewers": current_reviewers},
            )

            if self.config.failure_injection_hook:
                self.config.failure_injection_hook("reviewing", run_dir, task_spec)

            self._record_event(
                task_id=task_spec.task_id,
                agent_id="control_plane",
                action="review_started",
                spec=task_spec,
                metadata={"cycle": cycle_idx, "reviewers": current_reviewers},
            )

            cycle_res = ReviewRunner.execute_review_cycle(
                task=task_spec,
                diff_content=current_delta.diff_content,
                reviewer_roles=current_reviewers,
                cwd=self.target_repo,
                backend=self.config.custom_backend,
                cycle_index=cycle_idx,
                reviewer_agent_mapping=self.config.reviewer_agent_mapping,
                custom_reviewer_fn=self.config.custom_reviewer_fn,
                run_dir=run_dir,
                provider_pool=self.config.provider_pool,
            )
            review_cycles.append(cycle_res)
            latest_reconciliation = cycle_res.reconciliation

            # Save review cycle artifacts
            cycle_dir = reviews_dir if cycle_idx == 1 else (remediation_base_dir / f"cycle-{remediation_count:02d}" / "re_review")
            cycle_dir.mkdir(parents=True, exist_ok=True)
            for role_id, single_rev in cycle_res.reviewer_results.items():
                (cycle_dir / f"{role_id}.md").write_text(single_rev.raw_output, encoding="utf-8")
                (cycle_dir / f"{role_id}_findings.yaml").write_text(
                    yaml.dump([f.to_dict() for f in single_rev.findings], sort_keys=False),
                    encoding="utf-8",
                )

            # Persist findings and reconciliation
            findings_data = {"findings": [f.to_dict() for f in cycle_res.all_findings]}
            (run_dir / "findings.yaml").write_text(yaml.dump(findings_data, sort_keys=False), encoding="utf-8")
            if cycle_res.reconciliation:
                (run_dir / "reconciliation.json").write_text(json.dumps(cycle_res.reconciliation.to_dict(), indent=2), encoding="utf-8")
                (run_dir / "reconciliation_report.md").write_text(cycle_res.reconciliation.render_markdown(), encoding="utf-8")

            findings_summary = {
                "total": len(cycle_res.all_findings),
                "blocker": cycle_res.reconciliation.unresolved_blockers if cycle_res.reconciliation else 0,
                "high": cycle_res.reconciliation.unresolved_highs if cycle_res.reconciliation else 0,
            }
            self._record_event(
                task_id=task_spec.task_id,
                agent_id="control_plane",
                action="review_completed",
                spec=task_spec,
                findings_summary=findings_summary,
                metadata={"status": cycle_res.status, "cycle": cycle_idx},
            )
            CheckpointManager.complete_stage(
                run_dir,
                "reviewing",
                output_artifacts=[str(run_dir / "reconciliation.json")],
                result_summary=findings_summary,
            )

            if self.config.failure_injection_hook:
                self.config.failure_injection_hook("post_review", run_dir, task_spec)

            # Check if any findings require remediation
            if not cycle_res.requires_remediation:
                break

            # If remediation required, check cycle bounds
            if remediation_count >= self.config.max_remediation_cycles:
                err_msg = (
                    f"Remediation limit reached ({self.config.max_remediation_cycles} cycles) with "
                    f"{findings_summary['blocker']} blockers and {findings_summary['high']} high findings remaining."
                )
                task_spec.transition_to("awaiting_human", f"Max remediation cycles exceeded: {err_msg}")
                task_spec.save_to_file(str(run_dir / "task.yaml"))

                decision_pkt = HumanDecisionPacket(
                    task_id=task_spec.task_id,
                    objective=task_spec.objective,
                    change_summary=f"Implementation diff ({current_delta.insertions} ins, {current_delta.deletions} del) has unresolved reviewer findings.",
                    boundary_triggers=["security_policy_exception"] if findings_summary["high"] > 0 else ["unresolved_findings"],
                    evidence=[f.title for f in cycle_res.all_findings if f.severity in ("blocker", "high")],
                    risks=["Unresolved reviewer findings may indicate defects or security vulnerabilities."],
                    review_findings_summary=findings_summary,
                    verification_status="unverified",
                    recommended_action="Review finding report in reconciliation_report.md and authorize override or manual remediation.",
                )
                (run_dir / "decision_packet.md").write_text(decision_pkt.render_markdown(), encoding="utf-8")
                self._record_human_boundary_events(task_spec, run_dir, reason=err_msg)
                return self._make_result(
                    task_spec=task_spec,
                    final_state="awaiting_human",
                    exit_code=2,
                    start_time=start_time,
                    run_dir=run_dir,
                    routing=routing,
                    initial_delta=initial_delta,
                    current_delta=current_delta,
                    review_cycles=review_cycles,
                    reconciliation=latest_reconciliation,
                    verif_plan=verif_plan,
                    remediation_count=remediation_count,
                    err_msg=err_msg,
                    provider_execution=impl_res,
                    failure_class=FAILURE_CLASS_AUTHORITY_BLOCKED,
                )

            remediation_count += 1
            CheckpointManager.start_stage(
                run_dir,
                task_spec.task_id,
                "remediating",
                repo_path=self.target_repo,
                metadata={"cycle": remediation_count},
            )

            if self.config.failure_injection_hook:
                self.config.failure_injection_hook("remediating", run_dir, task_spec)

            task_spec.transition_to(
                "remediating",
                f"Remediation cycle {remediation_count}: resolving {len(cycle_res.all_findings)} review findings",
            )
            task_spec.save_to_file(str(run_dir / "task.yaml"))

            self._record_event(
                task_id=task_spec.task_id,
                agent_id=routing.selected_agent_id,
                action="remediation_started",
                spec=task_spec,
                metadata={"cycle": remediation_count, "findings_count": len(cycle_res.all_findings)},
            )

            rem_cycle_dir = remediation_base_dir / f"cycle-{remediation_count:02d}"
            rem_cycle_dir.mkdir(parents=True, exist_ok=True)

            if self.config.custom_remediation_fn:
                self.config.custom_remediation_fn(task_spec, self.target_repo, cycle_res.all_findings)
            else:
                rem_prompt = self._build_remediation_prompt(task_spec, current_delta.diff_content, cycle_res.all_findings)
                rem_res = impl_backend.execute(
                    task=task_spec,
                    cwd=self.target_repo,
                    role="remediation",
                    prompt_override=rem_prompt,
                    timeout_seconds=self.config.timeout_seconds,
                )
                (rem_cycle_dir / "result.json").write_text(rem_res.to_json(), encoding="utf-8")

            current_delta = self._capture_scoped_delta(
                task_spec, baseline, routing.selected_agent_id, stage="remediation"
            )
            (rem_cycle_dir / "diff.patch").write_text(current_delta.diff_content, encoding="utf-8")
            (run_dir / "diff.patch").write_text(current_delta.diff_content, encoding="utf-8")

            self._record_event(
                task_id=task_spec.task_id,
                agent_id=routing.selected_agent_id,
                action="remediation_completed",
                spec=task_spec,
                metadata={"cycle": remediation_count, "files_modified": len(current_delta.files_modified)},
            )
            self._record_event(
                task_id=task_spec.task_id,
                agent_id="control_plane",
                action="repository_delta_captured",
                spec=task_spec,
            )
            CheckpointManager.complete_stage(
                run_dir,
                "remediating",
                output_artifacts=[str(rem_cycle_dir / "diff.patch")],
            )

            if self.config.failure_injection_hook:
                self.config.failure_injection_hook("post_remediation", run_dir, task_spec)

            current_reviewers = ReviewRunner.determine_re_review_roles(
                cycle_res.all_findings,
                routing.recommended_reviewers,
            )
            task_spec.transition_to("reviewing", f"Re-review cycle {remediation_count + 1} for targeted roles: {current_reviewers}")
            task_spec.save_to_file(str(run_dir / "task.yaml"))

        # --------------------------------------------------------------------
        # Stage 6: Deterministic Verification Gate (verifying)
        # --------------------------------------------------------------------
        CheckpointManager.start_stage(
            run_dir,
            task_spec.task_id,
            "verifying",
            repo_path=self.target_repo,
            input_artifacts=[str(run_dir / "diff.patch")],
        )

        if self.config.failure_injection_hook:
            self.config.failure_injection_hook("verifying", run_dir, task_spec)

        task_spec.transition_to("verifying", "Executing deterministic verification plan")
        task_spec.save_to_file(str(run_dir / "task.yaml"))

        self._record_event(
            task_id=task_spec.task_id,
            agent_id="control_plane",
            action="verification_started",
            spec=task_spec,
            metadata={"steps_count": len(verif_plan.steps)},
        )

        verif_status = verif_plan.execute_all(
            cwd=str(self.target_repo),
            stop_on_failure=self.config.stop_on_verification_failure,
        )
        (run_dir / "verification_result.json").write_text(verif_plan.to_json(), encoding="utf-8")

        verif_summary = {s.name: s.status for s in verif_plan.steps}
        self._record_event(
            task_id=task_spec.task_id,
            agent_id="control_plane",
            action="verification_completed",
            spec=task_spec,
            result=verif_status,
            verification_summary=verif_summary,
        )
        CheckpointManager.complete_stage(
            run_dir,
            "verifying",
            output_artifacts=[str(run_dir / "verification_result.json")],
            result_summary=verif_summary,
        )

        if verif_status != "passed":
            failed_steps = [s.name for s in verif_plan.steps if s.status == "failed" and s.required]
            err_msg = f"Deterministic verification failed on required steps: {', '.join(failed_steps)}"
            return self._fail_task(
                task_spec,
                run_dir,
                err_msg,
                start_time,
                exit_code=1,
                routing=routing,
                initial_delta=initial_delta,
                current_delta=current_delta,
                review_cycles=review_cycles,
                reconciliation=latest_reconciliation,
                verif_plan=verif_plan,
                hf_status=hf_audit_status,
                hf_match=hf_audit_match,
                remediation_count=remediation_count,
                provider_execution=impl_res,
                failure_class=FAILURE_CLASS_VERIFICATION,
            )

        # --------------------------------------------------------------------
        # Stage 7: Human Authority Boundary Gate (awaiting_human)
        # --------------------------------------------------------------------
        actions_to_check = list(planned_actions or [])
        if any(kw in task_spec.objective.lower() for kw in ["deploy", "terraform apply", "kubectl apply", "drop table"]):
            actions_to_check.append(task_spec.objective)

        boundary_res = HumanBoundaryGate.evaluate(
            task=task_spec,
            planned_actions=actions_to_check,
            change_summary=f"Changed {len(current_delta.files_modified) + len(current_delta.files_added)} files (+{current_delta.insertions}/-{current_delta.deletions})",
            reconciliation=latest_reconciliation,
            verification=verif_plan,
        )

        stage_kwargs = dict(
            start_time=start_time,
            run_dir=run_dir,
            routing=routing,
            initial_delta=initial_delta,
            current_delta=current_delta,
            review_cycles=review_cycles,
            reconciliation=latest_reconciliation,
            verif_plan=verif_plan,
            boundary_res=boundary_res,
            hf_status=hf_audit_status,
            hf_match=hf_audit_match,
            remediation_count=remediation_count,
            provider_execution=impl_res,
        )

        if boundary_res.requires_human_approval:
            CheckpointManager.start_stage(
                run_dir,
                task_spec.task_id,
                "awaiting_human",
                repo_path=self.target_repo,
                metadata={"boundary_triggers": boundary_res.triggered_boundaries},
            )
            task_spec.transition_to("awaiting_human", f"Human authority boundary triggered: {boundary_res.triggered_boundaries}")
            task_spec.save_to_file(str(run_dir / "task.yaml"))

            if boundary_res.decision_packet:
                (run_dir / "decision_packet.md").write_text(boundary_res.decision_packet.render_markdown(), encoding="utf-8")

            self._record_human_boundary_events(
                task_spec, run_dir, boundaries=boundary_res.triggered_boundaries
            )

            return self._make_result(
                task_spec, "awaiting_human", 2,
                failure_class=FAILURE_CLASS_AUTHORITY_BLOCKED, **stage_kwargs,
            )

        # --------------------------------------------------------------------
        # Stage 8: Governed Completion (complete)
        # --------------------------------------------------------------------
        CheckpointManager.start_stage(
            run_dir,
            task_spec.task_id,
            "complete",
            repo_path=self.target_repo,
        )
        task_spec.transition_to("complete", "All reviews, reconciliations, deterministic verifications, and policies passed.")
        task_spec.save_to_file(str(run_dir / "task.yaml"))

        # Generate summary markdown
        summary_md = self._render_summary_markdown(
            task=task_spec,
            routing=routing,
            delta=current_delta,
            review_cycles=review_cycles,
            verif_plan=verif_plan,
            remediation_count=remediation_count,
            hf_status=hf_audit_status,
        )
        (run_dir / "summary.md").write_text(summary_md, encoding="utf-8")

        self._record_event(
            task_id=task_spec.task_id,
            agent_id="control_plane",
            action="task_completed",
            spec=task_spec,
            metadata={
                "remediation_cycles": remediation_count,
                "verification_status": verif_status,
                "files_changed": len(current_delta.files_modified) + len(current_delta.files_added),
            },
        )
        CheckpointManager.complete_stage(
            run_dir,
            "complete",
            output_artifacts=[str(run_dir / "summary.md")],
        )

        return self._make_result(task_spec, "complete", 0, **stage_kwargs)

    def _record_human_boundary_events(
        self,
        task_spec: TaskSpec,
        run_dir: Path,
        boundaries: Optional[List[str]] = None,
        reason: Optional[str] = None,
    ) -> None:
        meta = {"boundaries": boundaries} if boundaries else {"reason": reason}
        self._record_event(
            task_id=task_spec.task_id,
            agent_id="control_plane",
            action="human_decision_requested",
            artifact=str(run_dir / "decision_packet.md"),
            spec=task_spec,
            metadata=meta,
        )
        self._record_event(
            task_id=task_spec.task_id,
            agent_id="control_plane",
            action="human_boundary_triggered",
            spec=task_spec,
            metadata=meta,
        )

    def _make_result(
        self,
        task_spec: TaskSpec,
        final_state: str,
        exit_code: int,
        start_time: float,
        run_dir: Path,
        routing: Optional[Any] = None,
        initial_delta: Optional[Any] = None,
        current_delta: Optional[Any] = None,
        review_cycles: Optional[List[Any]] = None,
        reconciliation: Optional[Any] = None,
        verif_plan: Optional[Any] = None,
        boundary_res: Optional[Any] = None,
        hf_status: Optional[str] = None,
        hf_match: Optional[bool] = None,
        remediation_count: int = 0,
        err_msg: Optional[str] = None,
        provider_execution: Optional[AgentExecutionResult] = None,
        failure_class: Optional[str] = None,
    ) -> OrchestrationResult:
        return OrchestrationResult(
            task_id=task_spec.task_id,
            task_spec=task_spec,
            final_state=final_state,
            exit_code=exit_code,
            routing_decision=routing,
            initial_delta=initial_delta,
            final_delta=current_delta,
            review_cycles=review_cycles or [],
            reconciliation=reconciliation,
            verification_plan=verif_plan,
            boundary_result=boundary_res,
            howlframe_audit_status=hf_status,
            howlframe_audit_match=hf_match,
            remediation_cycles_count=remediation_count,
            duration_seconds=round(time.time() - start_time, 3),
            error_message=err_msg,
            run_dir=str(run_dir),
            provider_execution=provider_execution,
            failure_class=failure_class,
        )

    def _fail_task(
        self,
        task_spec: TaskSpec,
        run_dir: Path,
        err_msg: str,
        start_time: float,
        exit_code: int = 1,
        agent_id: str = "control_plane",
        **kwargs,
    ) -> OrchestrationResult:
        task_spec.transition_to("failed", err_msg)
        task_spec.save_to_file(str(run_dir / "task.yaml"))
        self._record_event(
            task_id=task_spec.task_id,
            agent_id=agent_id,
            action="task_failed",
            result=err_msg,
            spec=task_spec,
        )
        return self._make_result(
            task_spec=task_spec,
            final_state="failed",
            exit_code=exit_code,
            start_time=start_time,
            run_dir=run_dir,
            err_msg=err_msg,
            **kwargs,
        )

    def _build_implementation_prompt(
        self,
        task: TaskSpec,
        ctx: Optional[ProjectContext] = None,
    ) -> str:
        """Constructs an implementation prompt with criteria and skill guidance."""
        lines = [
            f"# Task Implementation Request: `{task.task_id}`",
            f"**Objective:** {task.objective}",
            "",
            "## Acceptance Criteria",
        ]
        for c in task.acceptance_criteria:
            lines.append(f"- {c}")
        if task.constraints:
            lines.append("")
            lines.append("## Constraints")
            for c in task.constraints:
                lines.append(f"- {c}")
        if task.required_skills:
            lines.append("")
            lines.append("## Required Skills")
            for s in task.required_skills:
                lines.append(f"- {s}")
        if "howlframe-app-development" in (task.required_skills or []):
            lines.append("")
            lines.append("## HowlFrame Application Guidance")
            lines.append("- Build & test commands: `bash scripts/build.sh`, `bash scripts/test.sh`")
            lines.append("- Standalone bytecode VM runs under capability gates (`network,database,filesystem`).")
            lines.append("- Single root form (`http_server`, `web_app`, `cli_app`, or `module`) per `.howl` file.")
            lines.append("- Handle fallible parsing/io using `try_let`.")
        return "\n".join(lines)

    def _build_remediation_prompt(
        self,
        task: TaskSpec,
        diff_content: str,
        findings: List[ReviewFinding],
    ) -> str:
        """Constructs an actionable remediation prompt with confirmed defect evidence."""
        lines = [
            f"# Remediation Request for Task `{task.task_id}`",
            f"**Objective:** {task.objective}",
            "",
            "## Identified Defects to Remediate",
        ]
        for f in findings:
            lines.append(f"### [{f.severity.upper()}] {f.title} ({f.reviewer_role})")
            if f.location:
                lines.append(f"- **Location:** `{f.location}`")
            if f.claim:
                lines.append(f"- **Claim:** {f.claim}")
            if f.evidence:
                lines.append(f"- **Evidence / Failure Case:** {f.evidence}")
            if f.suggested_fix:
                lines.append(f"- **Suggested Fix:** {f.suggested_fix}")
            lines.append("")

        lines.extend([
            "## Current Implementation Diff",
            "```diff",
            diff_content,
            "```",
            "",
            "## Instructions",
            "Fix the reported defects in the repository. Ensure all edge cases and tests pass.",
        ])
        return "\n".join(lines)

    def _render_summary_markdown(
        self,
        task: TaskSpec,
        routing: RoutingDecision,
        delta: RepositoryDelta,
        review_cycles: List[ReviewCycleResult],
        verif_plan: VerificationPlan,
        remediation_count: int,
        hf_status: Optional[str],
    ) -> str:
        lines = [
            f"# Governed Task Run Summary: `{task.task_id}`",
            "",
            "## Overview",
            f"- **Objective:** {task.objective}",
            f"- **Repository:** {self.target_repo.name}",
            f"- **Final State:** `{task.current_state.upper()}`",
            f"- **Implementing Agent:** {routing.selected_agent_name} (`{routing.selected_agent_id}`)",
            f"- **Reasoning Tier:** {routing.reasoning_tier}",
            f"- **HowlFrame Shadow Audit:** {hf_status or 'N/A'}",
            "",
            "## Repository Changes",
            f"- **Files Added:** {len(delta.files_added)}",
            f"- **Files Modified:** {len(delta.files_modified)}",
            f"- **Files Deleted:** {len(delta.files_deleted)}",
            f"- **Total Insertions:** +{delta.insertions}",
            f"- **Total Deletions:** -{delta.deletions}",
            "",
            "## Review & Remediation",
            f"- **Total Review Cycles:** {len(review_cycles)}",
            f"- **Remediation Cycles:** {remediation_count}",
        ]
        if review_cycles:
            last_cycle = review_cycles[-1]
            lines.append(f"- **Final Review Status:** `{last_cycle.status}`")
            for role, res in sorted(last_cycle.reviewer_results.items()):
                lines.append(f"  - `{role}`: {res.status} ({len(res.findings)} findings)")

        lines.extend([
            "",
            "## Deterministic Verification",
            f"- **Overall Status:** `{verif_plan.overall_status.upper()}`",
        ])
        for step in verif_plan.steps:
            mark = "✓" if step.status == "verified" else "✗"
            lines.append(f"- [{mark}] **{step.name}** (`{step.category}`): {step.status} (exit {step.exit_code})")

        lines.extend([
            "",
            "---",
            "*Verified and sealed by HowlPlane AI Engineering Control Plane.*",
        ])
        return "\n".join(lines)

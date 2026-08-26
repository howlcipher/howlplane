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

from src.control_plane.agent_execution import (
    AgentBackend,
    AgentBackendRegistry,
    AgentExecutionResult,
    AgentUnavailableError,
    TOOL_PERMISSION_DENIED,
    TOOL_PERMISSION_KEY,
)
from src.control_plane.atomic_io import (
    atomic_write_json,
    atomic_write_text,
    atomic_write_yaml,
    safe_load_json,
    safe_load_yaml,
)
from src.control_plane.checkpoints import CheckpointManager, StageCheckpoint
from src.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from src.control_plane.git_baseline import (
    GitBaseline,
    RepositoryDelta,
    capture_baseline,
    capture_delta,
    restore_repository_to_baseline,
)
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
from src.control_plane.progress import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    TaskPhase,
    TaskProgressState,
    TaskProgressTracker,
)
from src.control_plane.proposed_action import ProposedAction, infer_proposed_actions
from src.control_plane.resource_models import ProviderFailureClass
from src.control_plane.task_spec import TaskSpec
from src.control_plane.verification import VerificationPlan, VerificationStep
from src.control_plane.reasoning.execution_trajectory import (
    ExecutionTrajectoryBuilder,
    TrajectoryStore,
)

ORCHESTRATOR_SCHEMA_VERSION = "howlplane.orchestrator/v1"
FAILOVER_SUMMARY_SCHEMA_VERSION = "howlplane.failover_summary/v1"

# Structured reasons a governed task did not reach "complete" (#59.1 Phase 1).
# The orchestrator assigns only the classes it can prove from its own gates.
# When the shared pool is supplied, the orchestrator classifies the observed
# implementation result through that same pool before returning. Legacy callers
# without a pool retain caller-side classification compatibility.
FAILURE_CLASS_ENGINEERING = "ENGINEERING_FAILURE"
FAILURE_CLASS_AUTHORITY_BLOCKED = "AUTHORITY_BLOCKED"
FAILURE_CLASS_VERIFICATION = "VERIFICATION_FAILURE"
FAILURE_CLASS_PROVIDER_EXHAUSTED = "PROVIDER_EXHAUSTED"
FAILURE_CLASS_PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
FAILURE_CLASS_NO_ELIGIBLE_RESOURCE = "NO_ELIGIBLE_AI_RESOURCE"

# Why bounded implementation failover stopped. Recorded verbatim in the run's
# failover summary so an operator never has to infer it from a message string.
TERMINATION_MAX_ATTEMPTS_REACHED = "max_attempts_reached"
TERMINATION_NO_ELIGIBLE_RESOURCE = "no_eligible_resource"
TERMINATION_NON_FAILOVER_FAILURE = "non_failover_failure"
TERMINATION_ROLLBACK_FAILED = "rollback_failed"
TERMINATION_ATTEMPTS_EXHAUSTED = "attempts_exhausted"
TERMINATION_IMPLEMENTATION_SUCCEEDED = "implementation_succeeded"


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
    # Maximum number of implementation providers to try before giving up.
    # Each provider/resource is attempted at most once per task.
    max_provider_failover_attempts: int = 3
    # Optional resolver that overrides AgentBackendRegistry.get_backend. Useful
    # in deterministic tests to inject per-resource fake backends.
    backend_resolver: Optional[Callable[[str], AgentBackend]] = None
    # Enables bounded reviewer failover (#59.2 Phase 4) in the governed review
    # cycle: a reviewer whose assigned provider fails, times out, or emits
    # invalid/malformed output gets one alternate-provider attempt instead of
    # immediately blocking the task. Sharing the caller's pool means review
    # quota exhaustion updates the same provider state implementation failover
    # reads (#59.2 Phase 9). Left None (the default) by callers that inject a
    # custom_backend/custom_reviewer_fn, which keeps deterministic tests on the
    # prior single-attempt path.
    provider_pool: Optional[Any] = None
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    progress_stream: Optional[Any] = None
    progress_mode: str = "auto"
    progress_tracker: Optional[Any] = None


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
    resource_selection: Optional[Dict[str, Any]] = None
    capacity_after: Dict[str, str] = field(default_factory=dict)
    trajectory_id: Optional[str] = None
    # Additive record of every implementation provider attempt, successful or not.
    implementation_attempts: List[Dict[str, Any]] = field(default_factory=list)
    # Why bounded failover stopped, and what was still eligible when it did.
    failover_summary: Optional[Dict[str, Any]] = None
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
            "resource_selection": self.resource_selection,
            "capacity_after": self.capacity_after,
            "trajectory_id": self.trajectory_id,
            "implementation_attempts": self.implementation_attempts,
            "failover_summary": self.failover_summary,
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

    def _record_implementation_attempt(
        self,
        run_dir: Path,
        attempts_dir: Path,
        attempt_num: int,
        resource_id: str,
        impl_res: Optional[AgentExecutionResult],
        delta: RepositoryDelta,
        failure_class: Optional[str],
        capacity_before: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Persists attempt-level evidence and returns an additive record."""
        attempt_dir = attempts_dir / f"{attempt_num:02d}-{resource_id}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        if impl_res is not None:
            (attempt_dir / "result.json").write_text(impl_res.to_json(), encoding="utf-8")
        (attempt_dir / "diff.patch").write_text(delta.diff_content, encoding="utf-8")
        if not delta.is_empty:
            (attempt_dir / "partial_work.patch").write_text(delta.diff_content, encoding="utf-8")

        capacity_after: Dict[str, str] = {}
        if self.config.provider_pool is not None:
            # record_result already mutated state; capture after.
            capacity_after = self.config.provider_pool.get_all_statuses()

        record: Dict[str, Any] = {
            "attempt": attempt_num,
            "resource_id": resource_id,
            "agent_id": impl_res.agent_id if impl_res else resource_id,
            "identity": self._resource_identity_fields(resource_id),
            "success": impl_res.success if impl_res else False,
            "exit_code": impl_res.exit_code if impl_res else None,
            "duration_seconds": impl_res.duration_seconds if impl_res else 0.0,
            "failure_class": failure_class,
            "delta": delta.to_dict(),
            "capacity_before": capacity_before,
            "capacity_after": capacity_after,
            "evidence_dir": str(attempt_dir.relative_to(run_dir)),
        }
        self._persist_attempt_record(record, attempts_dir)
        return record

    def _resource_identity_fields(self, resource_id: str) -> Dict[str, Any]:
        """Names which provider, interface, and model actually ran this attempt."""
        state = (
            self.config.provider_pool.get_resource_status(resource_id)
            if self.config.provider_pool is not None else None
        )
        if state is None:
            return {
                "provider_id": None,
                "interface_id": None,
                "resource_id": resource_id,
                "model_id": None,
            }
        return {
            "provider_id": state.provider_id,
            "interface_id": state.interface_id,
            "resource_id": state.resource_id or resource_id,
            "model_id": state.model_id,
        }

    @staticmethod
    def _persist_attempt_record(
        attempt_record: Dict[str, Any],
        attempts_dir: Path,
    ) -> None:
        """Rewrites an attempt's record in place as later evidence is amended.

        Attempt evidence is additive and is filled in across the attempt's
        lifetime -- the result first, then the failover selection, then the
        rollback outcome -- so every amendment goes through here rather than
        opening its own write path.
        """
        record_path = (
            attempts_dir
            / f"{attempt_record['attempt']:02d}-{attempt_record['resource_id']}"
            / "attempt_record.json"
        )
        record_path.write_text(json.dumps(attempt_record, indent=2), encoding="utf-8")

    def _attach_next_selection(
        self,
        attempt_record: Dict[str, Any],
        attempts_dir: Path,
        decision: Any,
    ) -> None:
        """Records which resource failover chose next, and why the others lost."""
        selected = getattr(decision, "selected", None)
        attempt_record["next_selection"] = {
            "selected_resource_id": selected.resource_id if selected else None,
            "blocked_reason": getattr(decision, "blocked_reason", None),
            "eligible_resources": [
                identity.resource_id
                for identity in getattr(decision, "eligible_resources", []) or []
            ],
            "exclusions": [
                {
                    "resource_id": exclusion.resource_id,
                    "reason": exclusion.reason,
                    "stage": exclusion.stage,
                }
                for exclusion in getattr(decision, "exclusions", []) or []
            ],
        }
        self._persist_attempt_record(attempt_record, attempts_dir)

    def _attach_rollback_result(
        self,
        attempt_record: Dict[str, Any],
        attempts_dir: Path,
        restored: bool,
        error: Optional[str],
    ) -> None:
        """Records whether this attempt's work was safely undone before the next."""
        attempt_record["rollback"] = {"restored": restored, "error": error}
        self._persist_attempt_record(attempt_record, attempts_dir)

    def _build_failover_summary(
        self,
        implementation_attempts: List[Dict[str, Any]],
        termination_reason: str,
        last_decision: Any,
    ) -> Dict[str, Any]:
        """Explains exhaustion so an operator never has to guess why it stopped."""
        remaining: List[str] = []
        excluded: Dict[str, str] = {}
        if last_decision is not None:
            selected = getattr(last_decision, "selected", None)
            remaining = [
                identity.resource_id
                for identity in getattr(last_decision, "eligible_resources", []) or []
                if selected is None or identity.resource_id != selected.resource_id
            ]
            excluded = {
                exclusion.resource_id: exclusion.reason
                for exclusion in getattr(last_decision, "exclusions", []) or []
            }
        return {
            "attempts_used": len(implementation_attempts),
            "attempts_allowed": self.config.max_provider_failover_attempts,
            "termination_reason": termination_reason,
            "attempted_resources": [
                {
                    "attempt": attempt.get("attempt"),
                    "resource_id": attempt.get("resource_id"),
                    "failure_class": attempt.get("failure_class"),
                }
                for attempt in implementation_attempts
            ],
            "remaining_eligible": remaining,
            "excluded": excluded,
            "schema": FAILOVER_SUMMARY_SCHEMA_VERSION,
        }

    def _is_failover_eligible_failure(self, failure_class: Optional[Any]) -> bool:
        """Returns True when the normalized failure class justifies provider failover."""
        if failure_class is None:
            return False
        return failure_class in {
            ProviderFailureClass.QUOTA_EXHAUSTED,
            ProviderFailureClass.SESSION_LIMIT,
            ProviderFailureClass.RATE_LIMITED,
            ProviderFailureClass.AUTHENTICATION_REQUIRED,
            ProviderFailureClass.PROVIDER_UNAVAILABLE,
            ProviderFailureClass.TRANSPORT_UNAVAILABLE,
            ProviderFailureClass.MISSING_EXECUTABLE,
            ProviderFailureClass.EXECUTION_PERMISSION_REQUIRED,
        }

    def _map_failure_class_to_orchestrator_class(
        self,
        failure_class: Optional[Any],
    ) -> str:
        """Maps a provider failure class to the orchestrator's coarse failure class."""
        if failure_class is None:
            return FAILURE_CLASS_ENGINEERING
        value = getattr(failure_class, "value", str(failure_class))
        if value in {"QUOTA_EXHAUSTED", "SESSION_LIMIT", "RATE_LIMITED"}:
            return FAILURE_CLASS_PROVIDER_EXHAUSTED
        if value in {
            "AUTHENTICATION_REQUIRED",
            "PROVIDER_UNAVAILABLE",
            "TRANSPORT_UNAVAILABLE",
            "MISSING_EXECUTABLE",
            "EXECUTION_PERMISSION_REQUIRED",
        }:
            return FAILURE_CLASS_PROVIDER_UNAVAILABLE
        return FAILURE_CLASS_ENGINEERING

    def _recompute_reviewers(
        self,
        routing: Any,
        task_spec: TaskSpec,
        final_impl_resource_id: str,
    ) -> None:
        """Re-evaluates reviewer independence after implementation failover."""
        if self.config.provider_pool is None:
            return
        initial_route = {
            "selected_agent_id": routing.selected_agent_id,
            "selected_agent_name": getattr(routing, "selected_agent_name", routing.selected_agent_id),
            "reviewer_resource_mapping": dict(routing.metadata.get("reviewer_resource_mapping", {})),
            "reviewer_resource_identities": dict(routing.metadata.get("reviewer_resource_identities", {})),
            "review_diversity_achieved": routing.metadata.get("review_diversity_achieved"),
        }
        mapping, diversity = self.config.provider_pool.select_reviewers(
            final_impl_resource_id,
            routing.recommended_reviewers,
            task=task_spec,
        )
        new_identities = {
            role: self.config.provider_pool.registry.get_resource(resource_id).resource_identity().to_dict()
            for role, resource_id in mapping.items()
            if self.config.provider_pool.registry.get_resource(resource_id) is not None
        }
        routing.metadata["reviewer_resource_mapping"] = mapping
        routing.metadata["reviewer_resource_identities"] = new_identities
        routing.metadata["review_diversity_achieved"] = diversity
        routing.metadata["initial_route"] = initial_route
        routing.metadata["final_implementation_resource"] = final_impl_resource_id
        routing.metadata["final_route"] = {
            "selected_agent_id": final_impl_resource_id,
            "reviewer_resource_mapping": mapping,
            "reviewer_resource_identities": new_identities,
            "review_diversity_achieved": diversity,
        }
        routing.metadata["route_status"] = "SUPERSEDED_BY_FAILOVER"
        routing.metadata["reassignment_reason"] = (
            f"Implementer failed over from {routing.selected_agent_id} to {final_impl_resource_id}; "
            f"reviewers recomputed to maintain reviewer independence"
        )
        run_dir = self.target_repo / ".task_runs" / task_spec.task_id
        if run_dir.exists():
            atomic_write_json(run_dir / "route.json", asdict(routing))
            effective_data = asdict(routing)
            effective_data["selected_agent_id"] = final_impl_resource_id
            atomic_write_json(run_dir / "effective_route.json", effective_data)

        self._record_event(
            task_id=task_spec.task_id,
            agent_id="control_plane",
            action="reviewers_recomputed_after_failover",
            spec=task_spec,
            metadata={
                "final_implementation_resource": final_impl_resource_id,
                "initial_implementation_resource": routing.selected_agent_id,
                "reviewer_mapping": mapping,
                "diversity_achieved": diversity,
            },
        )

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

    def _write_delta_patch(
        self,
        delta: RepositoryDelta,
        stage_dir: Path,
        run_dir: Path,
    ) -> None:
        """Publishes a captured patch as the stage artifact and the run's current diff."""
        (stage_dir / "diff.patch").write_text(delta.diff_content, encoding="utf-8")
        (run_dir / "diff.patch").write_text(delta.diff_content, encoding="utf-8")

    def _record_delta_captured(
        self,
        task_spec: TaskSpec,
        delta: Optional[RepositoryDelta] = None,
    ) -> None:
        """Records the repository_delta_captured event for a captured delta."""
        self._record_event(
            task_id=task_spec.task_id,
            agent_id="control_plane",
            action="repository_delta_captured",
            spec=task_spec,
            metadata=delta.to_event_metadata() if delta is not None else None,
        )

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

        router = TaskRouter(resource_pool=self.config.provider_pool)
        routing = router.route(task_spec)
        atomic_write_json(run_dir / "route.json", asdict(routing))
        atomic_write_json(run_dir / "initial_route.json", asdict(routing))

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
        run_dir = self.target_repo / ".task_runs" / task_spec.task_id

        # Setup Progress Tracker
        progress = self.config.progress_tracker
        if progress is None:
            enabled = (self.config.progress_mode != "never")
            stream = (
                self.config.progress_stream
                if self.config.progress_stream is not None
                else sys.stderr
            )
            progress = TaskProgressTracker(
                task_id=task_spec.task_id,
                run_dir=run_dir,
                stream=stream,
                heartbeat_interval=self.config.heartbeat_interval,
                enabled=enabled,
            )

        progress.start(
            task_id=task_spec.task_id,
            run_dir=run_dir,
            initial_phase=TaskPhase.PREPARING.value,
        )

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
            result = self._run_governed_loop(
                task_spec, planned_actions, start_time, progress=progress
            )
            if self.trajectory_store is not None:
                traj = ExecutionTrajectoryBuilder.from_orchestration_result(result)
                self.trajectory_store.save(traj)
                result.trajectory_id = traj.trajectory_id
            return result
        except Exception as exc:
            progress.record_terminal(
                TaskProgressState.FAILED,
                TaskPhase.FAILED,
                error_message=str(exc),
            )
            if run_dir.is_dir():
                try:
                    CheckpointManager.fail_stage(
                        run_dir,
                        stage=task_spec.current_state,
                        reason=str(exc),
                        result_summary={"error": str(exc), "interrupted": True},
                    )
                except Exception:
                    pass
            raise
        finally:
            progress.close()
            if task_lock:
                task_lock.release()
            if repo_lock:
                repo_lock.release()

    def _run_governed_loop(
        self,
        task_spec: TaskSpec,
        planned_actions: Optional[List[str]] = None,
        start_time: Optional[float] = None,
        progress: Optional[TaskProgressTracker] = None,
    ) -> OrchestrationResult:
        start_time = start_time or time.time()
        if progress is None:
            progress = TaskProgressTracker(
                task_id=task_spec.task_id,
                run_dir=None,
                enabled=False,
            )

        progress.transition(
            TaskPhase.ROUTING, details="selecting implementation resource"
        )
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
            progress.record_terminal(
                TaskProgressState.AWAITING_AUTHORIZATION,
                TaskPhase.AWAITING_AUTHORIZATION,
            )
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

        if not routing.selected_agent_id:
            blocked = routing.metadata.get("blocked_outcome") or {
                "status": "BLOCKED",
                "reason": FAILURE_CLASS_NO_ELIGIBLE_RESOURCE,
            }
            progress.record_terminal(
                TaskProgressState.FAILED,
                TaskPhase.FAILED,
                error_message=blocked.get("reason"),
            )
            task_spec.transition_to("blocked", blocked["reason"])
            task_spec.save_to_file(str(run_dir / "task.yaml"))
            return self._make_result(
                task_spec,
                "blocked",
                3,
                start_time=start_time,
                run_dir=run_dir,
                routing=routing,
                verif_plan=verif_plan,
                hf_status=hf_audit_status,
                hf_match=hf_audit_match,
                err_msg=json.dumps(blocked, sort_keys=True),
                failure_class=FAILURE_CLASS_NO_ELIGIBLE_RESOURCE,
            )

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
        has_existing_delta, rec_delta, rec_msg = CrashRecoveryEngine.reconcile_interrupted_implementation(
            self.target_repo, run_dir, task_spec
        )

        impl_dir = run_dir / "implementation"
        impl_dir.mkdir(parents=True, exist_ok=True)
        attempts_dir = impl_dir / "attempts"
        attempts_dir.mkdir(parents=True, exist_ok=True)

        # Stays None on the crash-recovery path, where an interrupted
        # implementation's delta is reconciled rather than re-run: there is no
        # provider execution in *this* process to report (#59.1 Phase 1).
        impl_res: Optional[AgentExecutionResult] = None
        implementation_attempts: List[Dict[str, Any]] = []
        # Declared out here because a resumed run can skip the attempt loop
        # entirely and still needs to report failover accounting.
        last_selection_decision: Optional[Any] = None

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

            impl_prompt = self._build_implementation_prompt(task_spec, ctx)
            current_impl_resource_id = routing.selected_agent_id
            attempted_impl_resource_ids: Set[str] = set()
            final_impl_resource_id: Optional[str] = None

            def fail_implementation(
                err_msg: str,
                failure_class: str,
                exit_code: int = 1,
                termination_reason: str = TERMINATION_NON_FAILOVER_FAILURE,
            ) -> OrchestrationResult:
                """Terminal-fails the run from inside the implementation attempt loop.

                Reads the attempt-scoped locals (delta, provider execution, attempt
                records) at call time, so every terminal path reports the state as of
                the attempt that failed.
                """
                return self._fail_task(
                    task_spec,
                    run_dir,
                    err_msg,
                    start_time,
                    exit_code=exit_code,
                    agent_id="control_plane",
                    progress_tracker=progress,
                    routing=routing,
                    initial_delta=current_delta,
                    current_delta=current_delta,
                    verif_plan=verif_plan,
                    hf_status=hf_audit_status,
                    hf_match=hf_audit_match,
                    provider_execution=impl_res,
                    failure_class=failure_class,
                    implementation_attempts=implementation_attempts,
                    failover_summary=self._build_failover_summary(
                        implementation_attempts,
                        termination_reason,
                        last_selection_decision,
                    ),
                )

            for attempt_num in range(1, self.config.max_provider_failover_attempts + 1):
                impl_res = None
                normalized_failure = None

                if self.config.custom_backend is not None:
                    impl_backend = self.config.custom_backend
                elif self.config.backend_resolver is not None:
                    impl_backend = self.config.backend_resolver(current_impl_resource_id)
                else:
                    impl_backend = AgentBackendRegistry.get_backend(current_impl_resource_id)

                if not impl_backend.is_available():
                    impl_res = AgentExecutionResult(
                        agent_id=current_impl_resource_id,
                        role="implementation",
                        command=f"backend:{current_impl_resource_id}",
                        exit_code=127,
                        stdout="",
                        stderr=f"Agent binary '{current_impl_resource_id}' is not installed or available on PATH.",
                        duration_seconds=0.0,
                        success=False,
                        error_message=f"Agent '{current_impl_resource_id}' unavailable",
                    )
                else:
                    if task_spec.current_state != "implementing":
                        task_spec.transition_to(
                            "implementing",
                            f"Launching implementation attempt {attempt_num}: {current_impl_resource_id}",
                        )
                        task_spec.save_to_file(str(run_dir / "task.yaml"))

                    task_spec.actual_agent = current_impl_resource_id
                    task_spec.save_to_file(str(run_dir / "task.yaml"))

                    self._record_event(
                        task_id=task_spec.task_id,
                        agent_id=current_impl_resource_id,
                        action="implementation_started",
                        spec=task_spec,
                        metadata={"attempt": attempt_num},
                    )

                    impl_agent_id = getattr(impl_backend, "agent_id", None) or current_impl_resource_id
                    with progress.operation(
                        phase=TaskPhase.IMPLEMENTING,
                        resource_id=impl_agent_id,
                        role="implementation",
                        details="started",
                        suppress_completion=True,
                    ):
                        impl_res = impl_backend.execute(
                            task=task_spec,
                            cwd=self.target_repo,
                            role="implementation",
                            prompt_override=impl_prompt,
                            timeout_seconds=self.config.timeout_seconds,
                        )

                # Snapshot capacity before record_result mutates it, so the
                # recorded "before" is genuinely the pre-attempt state.
                capacity_before: Dict[str, str] = (
                    self.config.provider_pool.get_all_statuses()
                    if self.config.provider_pool is not None else {}
                )

                if self.config.provider_pool is not None and impl_res is not None:
                    normalized_failure = self.config.provider_pool.record_result(
                        current_impl_resource_id,
                        impl_res,
                        task_id=task_spec.task_id,
                    )

                current_delta = self._capture_scoped_delta(
                    task_spec, baseline, current_impl_resource_id, stage="implementation"
                )

                denial_outcome = (
                    (impl_res.metadata or {}).get(TOOL_PERMISSION_KEY)
                    if impl_res is not None else None
                )
                if (
                    impl_res is not None
                    and impl_res.success
                    and denial_outcome == TOOL_PERMISSION_DENIED
                    and current_delta.is_empty
                ):
                    impl_res.success = False
                    if not impl_res.error_message:
                        impl_res.error_message = (
                            "Required tool permissions were unavailable and zero delta produced"
                        )
                    if self.config.provider_pool is not None:
                        normalized_failure = ProviderFailureClass.EXECUTION_PERMISSION_REQUIRED

                attempt_record = self._record_implementation_attempt(
                    run_dir=run_dir,
                    attempts_dir=attempts_dir,
                    attempt_num=attempt_num,
                    resource_id=current_impl_resource_id,
                    impl_res=impl_res,
                    delta=current_delta,
                    failure_class=normalized_failure.value if normalized_failure else None,
                    capacity_before=capacity_before,
                )
                implementation_attempts.append(attempt_record)

                if impl_res is not None and impl_res.success:
                    final_impl_resource_id = current_impl_resource_id
                    (impl_dir / "result.json").write_text(impl_res.to_json(), encoding="utf-8")
                    self._write_delta_patch(current_delta, impl_dir, run_dir)
                    self._record_event(
                        task_id=task_spec.task_id,
                        agent_id=current_impl_resource_id,
                        action="implementation_completed",
                        result="success",
                        spec=task_spec,
                        metadata={
                            "files_changed": len(current_delta.files_modified) + len(current_delta.files_added),
                            "attempt": attempt_num,
                        },
                    )
                    self._record_delta_captured(task_spec, current_delta)
                    break

                # Failed attempt: determine whether to failover or terminal-fail.
                err_msg = (
                    impl_res.stderr.strip()
                    if impl_res and impl_res.stderr and impl_res.stderr.strip()
                    else (impl_res.error_message if impl_res else f"Implementation failed on {current_impl_resource_id}")
                )
                failure_class_value = normalized_failure.value if normalized_failure else None

                progress.emit_implementation_failed(
                    current_impl_resource_id,
                    reason=failure_class_value or err_msg,
                )

                # This resource is spent for this task from here on, whatever
                # happens next. Recording it before selection is what stops a
                # resource whose cooldown elapsed mid-attempt from being offered
                # back and dead-ending the loop (SLOPFIX-03).
                attempted_impl_resource_ids.add(current_impl_resource_id)

                if not self._is_failover_eligible_failure(normalized_failure):
                    return fail_implementation(
                        err_msg,
                        self._map_failure_class_to_orchestrator_class(normalized_failure),
                        exit_code=impl_res.exit_code if impl_res and impl_res.exit_code != 0 else 1,
                        termination_reason=TERMINATION_NON_FAILOVER_FAILURE,
                    )

                if attempt_num >= self.config.max_provider_failover_attempts:
                    err_msg = (
                        f"Implementation failed on {current_impl_resource_id} ({failure_class_value}); "
                        f"max failover attempts ({self.config.max_provider_failover_attempts}) reached."
                    )
                    return fail_implementation(
                        err_msg,
                        FAILURE_CLASS_PROVIDER_EXHAUSTED,
                        termination_reason=TERMINATION_MAX_ATTEMPTS_REACHED,
                    )

                # Select next eligible implementation resource. Everything already
                # attempted is a hard exclusion, not merely a demotion.
                next_resource_id = None
                if self.config.provider_pool is not None:
                    next_decision = self.config.provider_pool.select_resource(
                        task_spec,
                        role="implementation",
                        avoid_resource_id=current_impl_resource_id,
                        exclude_resource_ids=attempted_impl_resource_ids,
                    )
                    last_selection_decision = next_decision
                    self._attach_next_selection(attempt_record, attempts_dir, next_decision)
                    if next_decision.selected:
                        next_resource_id = next_decision.selected.resource_id

                if (
                    not next_resource_id
                    or next_resource_id == current_impl_resource_id
                    or next_resource_id in attempted_impl_resource_ids
                ):
                    err_msg = f"Implementation failed on {current_impl_resource_id} ({failure_class_value}) and no eligible failover resource remains."
                    return fail_implementation(
                        err_msg,
                        FAILURE_CLASS_PROVIDER_EXHAUSTED,
                        termination_reason=TERMINATION_NO_ELIGIBLE_RESOURCE,
                    )

                # Restore repository to baseline before the next attempt.
                restored_ok, restore_err = restore_repository_to_baseline(
                    self.target_repo, baseline, current_delta
                )
                self._attach_rollback_result(
                    attempt_record, attempts_dir, restored_ok, restore_err
                )
                if not restored_ok:
                    err_msg = f"Implementation failover aborted: cannot safely restore baseline ({restore_err})."
                    return fail_implementation(
                        err_msg,
                        FAILURE_CLASS_PROVIDER_UNAVAILABLE,
                        termination_reason=TERMINATION_ROLLBACK_FAILED,
                    )

                progress.emit_failover(
                    current_impl_resource_id,
                    next_resource_id,
                    failure_class=failure_class_value,
                )
                self._record_event(
                    task_id=task_spec.task_id,
                    agent_id="control_plane",
                    action="implementation_failover",
                    spec=task_spec,
                    metadata={
                        "attempt": attempt_num,
                        "source_resource": current_impl_resource_id,
                        "target_resource": next_resource_id,
                        "failure_class": failure_class_value,
                    },
                )

                current_impl_resource_id = next_resource_id

            if impl_res is None or not impl_res.success:
                err_msg = (
                    f"Implementation exhausted all {self.config.max_provider_failover_attempts} "
                    "attempted provider(s) without success."
                )
                return self._fail_task(
                    task_spec,
                    run_dir,
                    err_msg,
                    start_time,
                    exit_code=1,
                    agent_id="control_plane",
                    progress_tracker=progress,
                    routing=routing,
                    current_delta=current_delta if "current_delta" in locals() else None,
                    verif_plan=verif_plan,
                    hf_status=hf_audit_status,
                    hf_match=hf_audit_match,
                    provider_execution=impl_res,
                    failure_class=FAILURE_CLASS_PROVIDER_EXHAUSTED,
                    implementation_attempts=implementation_attempts,
                    failover_summary=self._build_failover_summary(
                        implementation_attempts,
                        TERMINATION_ATTEMPTS_EXHAUSTED,
                        last_selection_decision,
                    ),
                )

            initial_delta = current_delta
            CheckpointManager.complete_stage(
                run_dir,
                "implementing",
                output_artifacts=[str(run_dir / "diff.patch")],
            )

            # If the final implementation resource differs from the initially
            # routed one, recompute reviewer assignments so independence claims
            # are truthful (#59.2 Phase 9).
            if (
                final_impl_resource_id is not None
                and final_impl_resource_id != routing.selected_agent_id
                and self.config.provider_pool is not None
            ):
                self._recompute_reviewers(routing, task_spec, final_impl_resource_id)

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
                reviewer_agent_mapping=(
                    self.config.reviewer_agent_mapping
                    or routing.metadata.get("reviewer_resource_mapping")
                ),
                custom_reviewer_fn=self.config.custom_reviewer_fn,
                run_dir=run_dir,
                provider_pool=self.config.provider_pool,
                progress_tracker=progress,
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
                progress.record_terminal(
                    TaskProgressState.AWAITING_AUTHORIZATION,
                    TaskPhase.AWAITING_AUTHORIZATION,
                    error_message=err_msg,
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
                    hf_status=hf_audit_status,
                    hf_match=hf_audit_match,
                    implementation_attempts=implementation_attempts,
                    failover_summary=self._build_failover_summary(
                        implementation_attempts,
                        TERMINATION_IMPLEMENTATION_SUCCEEDED,
                        last_selection_decision,
                    ),
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

            rem_agent_id = getattr(impl_backend, "agent_id", None) or routing.selected_agent_id
            with progress.operation(
                phase=TaskPhase.REMEDIATING,
                resource_id=rem_agent_id,
                role="remediation",
                cycle=remediation_count,
                details=f"cycle {remediation_count}",
            ):
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
            self._write_delta_patch(current_delta, rem_cycle_dir, run_dir)

            self._record_event(
                task_id=task_spec.task_id,
                agent_id=routing.selected_agent_id,
                action="remediation_completed",
                spec=task_spec,
                metadata={"cycle": remediation_count, "files_modified": len(current_delta.files_modified)},
            )
            self._record_delta_captured(task_spec)
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
            progress_tracker=progress,
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
                progress_tracker=progress,
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
            implementation_attempts=implementation_attempts,
            failover_summary=self._build_failover_summary(
                implementation_attempts,
                TERMINATION_IMPLEMENTATION_SUCCEEDED,
                last_selection_decision,
            ),
        )

        if boundary_res.requires_human_approval:
            progress.record_terminal(
                TaskProgressState.AWAITING_AUTHORIZATION,
                TaskPhase.AWAITING_AUTHORIZATION,
            )
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

        progress.record_terminal(
            TaskProgressState.COMPLETE,
            TaskPhase.COMPLETE,
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
        implementation_attempts: Optional[List[Dict[str, Any]]] = None,
        failover_summary: Optional[Dict[str, Any]] = None,
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
            resource_selection=(
                routing.metadata.get("resource_selection")
                if routing is not None else None
            ),
            capacity_after=(
                self.config.provider_pool.get_all_statuses()
                if self.config.provider_pool is not None else {}
            ),
            implementation_attempts=implementation_attempts or [],
            failover_summary=failover_summary,
        )

    def _fail_task(
        self,
        task_spec: TaskSpec,
        run_dir: Path,
        err_msg: str,
        start_time: float,
        exit_code: int = 1,
        agent_id: str = "control_plane",
        progress_tracker: Optional[Any] = None,
        **kwargs,
    ) -> OrchestrationResult:
        if progress_tracker is not None:
            progress_tracker.record_terminal(
                TaskProgressState.FAILED,
                TaskPhase.FAILED,
                error_message=err_msg,
            )
        task_spec.transition_to("failed", err_msg)
        task_spec.save_to_file(str(run_dir / "task.yaml"))

        # Terminalize current stage checkpoint so it does not remain permanently in_progress
        try:
            delta = kwargs.get("current_delta") or kwargs.get("initial_delta")
            summary: Dict[str, Any] = {
                "error": err_msg,
                "exit_code": exit_code,
            }
            if kwargs.get("failure_class"):
                summary["failure_class"] = kwargs.get("failure_class")
            if delta is not None:
                summary.update(delta.to_event_metadata())
                summary["partial_work"] = not delta.is_empty
            CheckpointManager.fail_stage(
                run_dir,
                stage=kwargs.get("stage") or task_spec.current_state,
                reason=err_msg,
                result_summary=summary,
            )
        except Exception:
            pass

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
            f"- **Implementing Agent:** {task.actual_agent or routing.selected_agent_id} (`{task.actual_agent or routing.selected_agent_id}`)",
            f"- **Reasoning Tier:** {routing.reasoning_tier}",
            f"- **HowlFrame Shadow Audit:** {hf_status or 'N/A'}",
            "",
        ]
        if task.actual_agent and task.actual_agent != routing.selected_agent_id:
            lines.append(f"- **Initial Route:** {routing.selected_agent_name} (`{routing.selected_agent_id}`)")
            lines.append("")
        lines.extend([
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
        ])
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
        executed_steps = [
            s for s in verif_plan.steps
            if s.exit_code is not None or s.status in ("verified", "failed")
        ]
        if not executed_steps and verif_plan.steps:
            lines.append(f"- **Discovered:** {len(verif_plan.steps)} steps (not executed — implementation failed before verification)")
        for step in verif_plan.steps:
            if step.exit_code is not None or step.status in ("verified", "failed"):
                mark = "✓" if step.status == "verified" else "✗"
                lines.append(f"- [{mark}] **{step.name}** (`{step.category}`): {step.status} (exit {step.exit_code})")
            else:
                lines.append(f"- [ ] **{step.name}** (`{step.category}`): {step.status}")

        lines.extend([
            "",
            "---",
            "*Verified and sealed by HowlPlane AI Engineering Control Plane.*",
        ])
        return "\n".join(lines)

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

from contextlib import ExitStack
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import hashlib, json, os, shutil, sys, time
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
from src.control_plane.locking import (
    LockOwnership,
    RepoLock,
    TaskLock,
    RepositoryLockedError,
    TaskLockedError,
)
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
from src.control_plane.verification_view import (
    VERIFICATION_VIEW_SCHEMA_VERSION,
    VerificationViewError,
    resolve_external_scratch_root,
    verification_view,
)
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
# This control plane stopped the provider at its own wall-clock budget. Kept
# distinct from PROVIDER_UNAVAILABLE so an operator reading the summary is never
# told a reachable provider was unreachable (HOWLFRAM-SLOPFIX-05).
FAILURE_CLASS_EXECUTION_BUDGET_EXCEEDED = "EXECUTION_BUDGET_EXCEEDED"

# A candidate that exists only because a provider was stopped at our budget.
# Recorded so no artifact can imply the provider reported success.
CANDIDATE_ORIGIN_TIMED_OUT = "timed_out_implementation_attempt"
TIMEOUT_CANDIDATE_SCHEMA_VERSION = "howlplane.timeout_candidate/v1"
PROVIDER_SCRATCH_SCHEMA_VERSION = "howlplane.provider_scratch/v1"
SCRATCH_MANIFEST_SCHEMA_VERSION = "howlplane.scratch_manifest/v1"
TERMINATION_TIMEOUT_CANDIDATE_GOVERNED = "timeout_candidate_governed"

# How durable routing evidence describes itself. SUPERSEDED_BY_FAILOVER means
# implementation moved and is still in flight; IMPLEMENTATION_FAILED means the
# named resource was the last one attempted and nothing was accepted.
ROUTE_STATUS_SUPERSEDED = "SUPERSEDED_BY_FAILOVER"
ROUTE_STATUS_IMPLEMENTATION_FAILED = "IMPLEMENTATION_FAILED"
# Terminal, and only reachable past the authority gate: the route names an
# implementer whose work governance actually accepted.
ROUTE_STATUS_ACCEPTED = "ACCEPTED"

# Terminal-attempt vocabulary, so a final attempt states its disposition
# explicitly instead of encoding it as missing fields.
ROLLBACK_PARKED_FOR_GOVERNANCE = "PARKED_FOR_GOVERNANCE"
NEXT_SELECTION_MAX_ATTEMPTS = "MAX_ATTEMPTS_REACHED"
TRANSITION_CANDIDATE_GOVERNANCE = "CANDIDATE_GOVERNANCE"

# Retention vocabulary. A retained salvage artifact is a productive timeout's
# work that was rolled back out of the working tree so the next provider could
# start clean, and kept as a bounded terminal fallback. Retention is not
# acceptance, and a retained artifact is deliberately not yet the run's
# candidate (HOWLFRAM-SLOPFIX-07R).
SALVAGE_ELIGIBLE = "ELIGIBLE"
SALVAGE_NOT_REPLAYABLE = "NOT_REPLAYABLE"
SALVAGE_RETAINED = "RETAINED"
SALVAGE_PROMOTED = "PROMOTED"
# A later attempt succeeded on its own, so the fallback is history, not an
# option. Recorded so nothing can later re-select an artifact that was
# superseded by provider-attested work.
SALVAGE_SUPERSEDED = "SUPERSEDED"
# Found, but proven not safely restorable. Recorded so a resumed run does not
# reach for the same unusable artifact forever.
SALVAGE_UNUSABLE = "UNUSABLE"
NEXT_SELECTION_SALVAGE_PROMOTED = "RETAINED_SALVAGE_PROMOTED"

# Interruption points that only exist so the retention lifecycle can be crash
# tested; inert unless a failure_injection_hook is configured.
FAULT_AFTER_SALVAGE_RETENTION = "after_salvage_retention"
FAULT_DURING_SALVAGE_PROMOTION = "during_salvage_promotion"
IMPLEMENTATION_ATTEMPT_SCHEMA_VERSION = "howlplane.implementation_attempt/v1"

# Everything the control plane itself writes into a task run directory. The
# sweep that relocates provider scratch must never move these, even if one is
# created after the pre-attempt snapshot was taken.
CONTROL_PLANE_RUN_ARTIFACTS = frozenset({
    ".task.lock",
    "baseline.json",
    "candidate.json",
    "checkpoints",
    "decision_packet.md",
    "diff.patch",
    "effective_route.json",
    "execution_receipt.json",
    "findings_template.yaml",
    "howlframe_audit.json",
    "implementation",
    "initial_route.json",
    "progress.json",
    "project_context.json",
    "provider_scratch",
    "remediation",
    "reviews",
    "route.json",
    "scratch_manifest.json",
    "stage_checkpoint.json",
    "summary.md",
    "task.yaml",
    "trajectories",
    "verification_plan.json",
})

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
    # Root directory for external provider scratch workspaces. When None, defaults
    # to $HOWLPLANE_SCRATCH_ROOT or $XDG_CACHE_HOME/howlplane/scratch (~/.cache).
    scratch_root: Optional[Union[str, Path]] = None
    # Run deterministic gates against a sanitized view (baseline + task delta)
    # instead of the live checkout, so untracked control plane evidence cannot
    # change a verification result (HOWLFRAM-SLOPFIX-07S).
    verification_isolation: bool = True


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
    # Whether the implementing provider actually reported completion. False for
    # a candidate salvaged from a budget-stopped attempt, which reaches the same
    # review and verification gates without ever being called a success
    # (HOWLFRAM-SLOPFIX-05).
    implementation_completion_claim: bool = True
    candidate_origin: Optional[str] = None
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

        # A retained salvage artifact from an earlier, interrupted run lives on
        # this attempt's record. Re-recording the same slot on resume must not
        # erase the fallback; its patch is kept under its own name and its
        # identity is digest-checked before any promotion, so carrying it
        # forward can only ever be verified, never trusted blindly.
        carried_salvage: Optional[Dict[str, Any]] = None
        existing_record = attempt_dir / "attempt_record.json"
        if existing_record.is_file():
            try:
                carried_salvage = (
                    safe_load_json(existing_record) or {}
                ).get("retained_salvage")
            except Exception:
                carried_salvage = None

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
            "schema": IMPLEMENTATION_ATTEMPT_SCHEMA_VERSION,
            "evidence_dir": str(attempt_dir.relative_to(run_dir)),
        }
        if carried_salvage:
            record["retained_salvage"] = carried_salvage
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
        rollback outcome, and now the retained-salvage block -- so every
        amendment goes through here rather than opening its own write path.

        The write is atomic because a retained salvage artifact is discoverable
        only through this file: a torn write during an interruption would lose
        the fallback that HOWLFRAM-SLOPFIX-07R proved must survive.
        """
        record_path = (
            attempts_dir
            / f"{attempt_record['attempt']:02d}-{attempt_record['resource_id']}"
            / "attempt_record.json"
        )
        atomic_write_json(record_path, attempt_record)

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

    def _park_awaiting_human(
        self,
        task_spec: TaskSpec,
        run_dir: Path,
        err_msg: str,
        decision_pkt: "HumanDecisionPacket",
        progress: Any,
    ) -> None:
        """Hands a task to human authority, leaving the working tree in place.

        Every parking path performs the same five steps, and the working tree is
        deliberately never rolled back here: a human is being asked to look at
        exactly what is on disk.
        """
        progress.record_terminal(
            TaskProgressState.AWAITING_AUTHORIZATION,
            TaskPhase.AWAITING_AUTHORIZATION,
            error_message=err_msg,
        )
        task_spec.transition_to("awaiting_human", err_msg)
        task_spec.save_to_file(str(run_dir / "task.yaml"))
        (run_dir / "decision_packet.md").write_text(
            decision_pkt.render_markdown(), encoding="utf-8"
        )
        self._record_human_boundary_events(task_spec, run_dir, reason=err_msg)

    def _get_scratch_base_path(self) -> Path:
        """Determines the root directory for external provider scratch workspaces.

        Provider scratch and the sanitized verification view share one root and
        one containment rule, so a root configured inside the target repository
        is rejected identically for both.
        """
        repo = self.target_repo if hasattr(self, "target_repo") else Path.cwd()
        return resolve_external_scratch_root(repo, self.config.scratch_root)

    def _execute_verification_plan(
        self,
        verif_plan: VerificationPlan,
        task_spec: TaskSpec,
        baseline: GitBaseline,
        delta: RepositoryDelta,
        run_dir: Path,
        progress: Optional[Any],
    ) -> str:
        """Runs the deterministic gates against a sanitized view of the repository.

        The gates measure the product, so they must see the product and nothing
        else. Running them in the live checkout let untracked control plane
        evidence decide the outcome: in HOWLFRAM-SLOPFIX-07S a provider's
        source clone under `.task_runs/` moved the `go_production` clone count
        from 291 to 1421 without a line of product code changing.

        A view that cannot be built fails the gate. Falling back to the live
        checkout would quietly restore that contamination and report the result
        as if it had been isolated.
        """
        if not self.config.verification_isolation:
            return verif_plan.execute_all(
                cwd=str(self.target_repo),
                stop_on_failure=self.config.stop_on_verification_failure,
                progress_tracker=progress,
            )

        view_ref: Optional[Any] = None
        try:
            with verification_view(
                target_repo=self.target_repo,
                baseline=baseline,
                delta=delta,
                task_id=task_spec.task_id,
                scratch_root=self.config.scratch_root,
            ) as view:
                view_ref = view
                self._record_event(
                    task_id=task_spec.task_id,
                    agent_id="control_plane",
                    action="verification_view_created",
                    spec=task_spec,
                    metadata={
                        "baseline_sha": view.baseline_sha,
                        "files_materialized": len(view.files_materialized),
                        "files_refused": view.files_refused,
                    },
                )
                status = verif_plan.execute_all(
                    cwd=str(view.path),
                    stop_on_failure=self.config.stop_on_verification_failure,
                    progress_tracker=progress,
                )
        except VerificationViewError as exc:
            reason = f"Sanitized verification view could not be built: {exc}"
            verif_plan.add_step(
                step_id="step-verification-view",
                name="Sanitized verification view",
                command=["git", "worktree", "add", "--detach"],
                category="policy_check",
                required=True,
                metadata={"baseline_sha": getattr(baseline, "initial_commit_sha", "")},
            )
            failed_step = verif_plan.steps[-1]
            failed_step.status = "failed"
            failed_step.exit_code = 1
            failed_step.stderr = reason
            verif_plan.overall_status = "failed"
            atomic_write_json(
                run_dir / "verification_view.json",
                {
                    "schema": VERIFICATION_VIEW_SCHEMA_VERSION,
                    "task_id": task_spec.task_id,
                    "status": "unavailable",
                    "error": str(exc),
                },
            )
            self._record_event(
                task_id=task_spec.task_id,
                agent_id="control_plane",
                action="verification_view_failed",
                spec=task_spec,
                result="failed",
                metadata={"error": str(exc)},
            )
            return "failed"

        # Written after teardown so the recorded cleanup status is the real one.
        if view_ref is not None:
            atomic_write_json(run_dir / "verification_view.json", view_ref.to_dict())
        return status

    def _attempt_workspace_hint(self, task: TaskSpec) -> str:
        """The provider-writable scratch path, as named to the provider.

        Deliberately outside `implementation/attempts/` and outside the target
        repository so scratch source trees or build caches cannot contaminate
        deterministic repository verification gates (HOWLFRAM-SLOPFIX-07S).
        """
        base = self._get_scratch_base_path()
        repo_slug = self.target_repo.resolve().name if hasattr(self, "target_repo") else "repo"
        return f"{base}/{repo_slug}/{task.task_id}/provider_scratch/<NN-resource>/"

    def _provider_scratch_dir(self, run_dir: Path, attempt_num: int, resource_id: str) -> Path:
        """Control-plane-owned scratch location for one attempt's provider."""
        base = self._get_scratch_base_path()
        repo_slug = self.target_repo.resolve().name if hasattr(self, "target_repo") else run_dir.parent.parent.name
        scratch_dir = base / repo_slug / run_dir.name / "provider_scratch" / f"{attempt_num:02d}-{resource_id}"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        return scratch_dir

    def _record_scratch_manifest(
        self,
        run_dir: Path,
        task_id: str,
        attempt_num: int,
        resource_id: str,
        scratch_dir: Path,
        status: str = "active",
        artifacts: Optional[List[str]] = None,
    ) -> None:
        """Records durable scratch location and ownership metadata in evidence."""
        manifest_path = run_dir / "scratch_manifest.json"
        manifest: Dict[str, Any] = {}
        if manifest_path.is_file():
            try:
                manifest = safe_load_json(manifest_path) or {}
            except Exception:
                manifest = {}
        if not manifest:
            manifest = {
                "task_id": task_id,
                "repository": str(self.target_repo.resolve()) if hasattr(self, "target_repo") else str(run_dir.parent.parent),
                "scratch_root": str(self._get_scratch_base_path()),
                "attempts": {},
                "schema": SCRATCH_MANIFEST_SCHEMA_VERSION,
            }
        attempt_key = f"{attempt_num:02d}-{resource_id}"
        attempt_entry = manifest.get("attempts", {}).get(attempt_key, {})
        attempt_entry.update({
            "attempt": attempt_num,
            "resource_id": resource_id,
            "scratch_path": str(scratch_dir.resolve()),
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        if "created_at" not in attempt_entry:
            attempt_entry["created_at"] = datetime.now(timezone.utc).isoformat()
        if artifacts:
            existing = set(attempt_entry.get("artifacts", []))
            existing.update(artifacts)
            attempt_entry["artifacts"] = sorted(existing)
        manifest.setdefault("attempts", {})[attempt_key] = attempt_entry
        atomic_write_json(manifest_path, manifest)

    def _prune_ephemeral_scratch(self, run_dir: Path, task_id: str) -> None:
        """Prunes large disposable build and tool caches from external scratch.

        Preserves candidate patches, diffs, provider transcripts, failure evidence,
        and provenance files. Never removes candidate material or durable evidence.
        Strictly validates containment beneath the authorized scratch base to prevent
        path traversal or unintended file deletion.
        """
        manifest_path = run_dir / "scratch_manifest.json"
        if not manifest_path.is_file():
            return
        try:
            manifest = safe_load_json(manifest_path)
            if not manifest or manifest.get("schema") != SCRATCH_MANIFEST_SCHEMA_VERSION:
                return
            if not isinstance(manifest.get("attempts"), dict):
                return
            scratch_base = self._get_scratch_base_path().resolve()
            disposable_names = {"go-cache", ".cache"}
            disposable_suffixes = {".tar", ".iso"}
            for attempt_key, attempt_info in manifest["attempts"].items():
                scratch_str = attempt_info.get("scratch_path")
                if not scratch_str:
                    continue
                scratch_p = Path(scratch_str).resolve()
                # Security: scratch directory must be strictly contained within scratch_base,
                # cannot be scratch_base itself, and cannot be a symlink.
                if not scratch_p.is_relative_to(scratch_base) or scratch_p == scratch_base:
                    continue
                if scratch_p.is_symlink() or not scratch_p.is_dir():
                    continue
                pruned = False
                for item in scratch_p.iterdir():
                    if item.is_symlink():
                        continue
                    if item.is_dir() and item.name in disposable_names:
                        shutil.rmtree(item, ignore_errors=True)
                        pruned = True
                    elif item.is_file() and item.suffix in disposable_suffixes:
                        try:
                            item.unlink()
                            pruned = True
                        except OSError:
                            pass
                if pruned:
                    attempt_info["status"] = "cleaned"
                    attempt_info["pruned_at"] = datetime.now(timezone.utc).isoformat()
            atomic_write_json(manifest_path, manifest)
        except Exception:
            pass

    def _resolve_backend(self, resource_id: str) -> AgentBackend:
        """Resolves the execution backend for a resource, however it is wired.

        A resumed run reconciles its implementation from the durable delta and
        never enters the attempt loop, so the loop-local backend it used to rely
        on did not exist by the time remediation needed one -- resume died with
        an UnboundLocalError before it could finish recovering anything.
        """
        if self.config.custom_backend is not None:
            return self.config.custom_backend
        if self.config.backend_resolver is not None:
            return self.config.backend_resolver(resource_id)
        return AgentBackendRegistry.get_backend(resource_id)

    def _sweep_provider_scratch(
        self,
        run_dir: Path,
        resource_id: str,
        attempt_num: int,
        known_before: Set[str],
        attempts_dir: Optional[Path] = None,
        attempts_before: Optional[Set[str]] = None,
        owned_attempt_name: Optional[str] = None,
    ) -> List[str]:
        """Moves anything a provider left in the evidence namespace into scratch.

        Providers write where they like -- they run in the repository with no
        filesystem sandbox -- so the boundary is enforced afterwards rather than
        assumed. Two escapes are swept: artifacts dropped at the run's evidence
        root (HOWLFRAM-SLOPFIX-05), and directories invented under
        `implementation/attempts/` that imitate a canonical attempt
        (HOWLFRAM-SLOPFIX-06, where `01-claude/` made a three-attempt run look
        like four). Both are relocated with provenance and nothing is deleted:
        the artifacts may well be useful, they are simply not evidence.
        """
        scratch = self._provider_scratch_dir(run_dir, attempt_num, resource_id)
        swept: List[str] = []

        def relocate(entry: Path, label: str) -> None:
            scratch.mkdir(parents=True, exist_ok=True)
            destination = scratch / entry.name
            if destination.exists():
                destination = scratch / f"{entry.name}.{attempt_num:02d}"
            try:
                entry.replace(destination)
            except OSError:
                return
            swept.append(label)

        for entry in sorted(run_dir.iterdir()):
            if entry.name in known_before:
                continue
            if entry.name in CONTROL_PLANE_RUN_ARTIFACTS:
                continue
            relocate(entry, entry.name)

        # A directory under attempts/ that this run did not create is a
        # provider's guess at its own attempt label, not an attempt.
        if attempts_dir is not None and attempts_dir.is_dir():
            known_attempts = attempts_before or set()
            for entry in sorted(attempts_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if entry.name in known_attempts or entry.name == owned_attempt_name:
                    continue
                if (entry / "attempt_record.json").is_file():
                    # Real control-plane evidence; never touch it.
                    continue
                for child in sorted(entry.iterdir()):
                    relocate(child, f"implementation/attempts/{entry.name}/{child.name}")
                try:
                    # Only ever removes the empty shell left behind, so the
                    # canonical attempt count stops counting a fake one.
                    entry.rmdir()
                except OSError:
                    pass

        if swept:
            atomic_write_json(
                scratch / "_provenance.json",
                {
                    "origin": "provider_scratch",
                    "created_by": resource_id,
                    "attempt": attempt_num,
                    "relocated_from": f".task_runs/{run_dir.name}/",
                    "files": swept,
                    "note": (
                        "Written by the provider inside the control plane's "
                        "evidence namespace and relocated here. Not control "
                        "plane evidence."
                    ),
                    "swept_at": datetime.now(timezone.utc).isoformat(),
                    "schema": PROVIDER_SCRATCH_SCHEMA_VERSION,
                },
            )
            self._record_scratch_manifest(
                run_dir,
                run_dir.name,
                attempt_num,
                resource_id,
                scratch,
                status="relocated",
                artifacts=swept,
            )
        return swept

    def _attach_terminal_disposition(
        self,
        attempt_record: Dict[str, Any],
        attempts_dir: Path,
        attempt_num: int,
        attempted: Set[str],
        task_spec: TaskSpec,
    ) -> None:
        """Records why a salvaged final attempt has no successor.

        Attempts that hand off carry `rollback` and `next_selection`; the one
        that exhausts the budget carried neither, so "no provider remained" and
        "nobody wrote the field" looked identical in the evidence. The candidate
        is deliberately not rolled back -- it is parked for governance -- and
        that has to be stated, not inferred (HOWLFRAM-SLOPFIX-06).
        """
        remaining: List[str] = []
        pool = self.config.provider_pool
        if pool is not None:
            try:
                for resource_id in pool.select_candidates(
                    task_category="code_heavy", task=task_spec
                ):
                    if resource_id not in attempted:
                        remaining.append(resource_id)
            except Exception:
                remaining = []

        attempt_record["max_attempts"] = self.config.max_provider_failover_attempts
        attempt_record["rollback"] = {
            "restored": False,
            "error": None,
            "status": ROLLBACK_PARKED_FOR_GOVERNANCE,
            "reason": (
                "Candidate preserved for governance instead of being rolled "
                "back; no further attempt follows it."
            ),
        }
        attempt_record["next_selection"] = None
        attempt_record["next_selection_reason"] = NEXT_SELECTION_MAX_ATTEMPTS
        attempt_record["remaining_eligible_resources"] = remaining
        attempt_record["transition"] = TRANSITION_CANDIDATE_GOVERNANCE
        self._persist_attempt_record(attempt_record, attempts_dir)

    @staticmethod
    def _recover_implementation_resource(run_dir: Path, routing: Any) -> Optional[str]:
        """Restores which resource produced the work being resumed.

        A resumed run re-routes from scratch, so the in-memory decision knows
        nothing about the failover that already happened. The durable effective
        route does, and it is the only truthful source for who to credit --
        and, until governance clears it, who not to let review their own work.
        """
        route_file = run_dir / "effective_route.json"
        if route_file.is_file():
            try:
                meta = (safe_load_json(route_file) or {}).get("metadata") or {}
            except Exception:
                meta = {}
            recovered = (
                meta.get("accepted_implementation_resource")
                or meta.get("candidate_resource")
                or meta.get("last_attempted_implementation_resource")
            )
            if recovered:
                # Carry the failover history forward so this run's evidence
                # continues the story instead of restarting it.
                for key in (
                    "initial_route",
                    "initial_implementation_resource",
                    "candidate_resource",
                    "last_attempted_implementation_resource",
                    "reviewer_resource_mapping",
                    "reviewer_resource_identities",
                ):
                    if key in meta and key not in routing.metadata:
                        routing.metadata[key] = meta[key]
                return recovered
        return routing.metadata.get("last_attempted_implementation_resource")

    @staticmethod
    def _load_parked_candidate(run_dir: Path) -> Optional[Dict[str, Any]]:
        """Recovers a parked timeout candidate's record after an interruption.

        The candidate is durable evidence, so a resumed run must know it is
        governing a salvaged fragment rather than provider-attested work.
        """
        attempts_dir = run_dir / "implementation" / "attempts"
        if not attempts_dir.is_dir():
            return None
        for attempt_dir in sorted(attempts_dir.iterdir(), reverse=True):
            candidate_file = attempt_dir / "candidate.json"
            if candidate_file.is_file():
                try:
                    return safe_load_json(candidate_file)
                except Exception:
                    return None
        return None

    @staticmethod
    def _is_salvageable_timeout_candidate(
        failure_class: Optional[Any],
        delta: Optional[RepositoryDelta],
    ) -> bool:
        """Reports whether a budget-stopped attempt left work worth governing.

        A provider we stopped at our own deadline never claimed completion, but
        that says nothing about what it had already written. When such an
        attempt leaves a non-empty task-attributable delta, the artifact is a
        candidate -- neither trustworthy nor disposable -- and belongs in review
        and verification rather than the bin (HOWLFRAM-SLOPFIX-05).
        """
        if failure_class != ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED:
            return False
        return delta is not None and not delta.is_empty

    @staticmethod
    def _timed_out_artifact_provenance(
        resource_id: str,
        attempt: int,
        delta: RepositoryDelta,
    ) -> Dict[str, Any]:
        """The provenance every budget-stopped artifact carries.

        A retained fallback and a governed candidate are the same artifact at
        two different points in its life, so they describe their origin
        identically -- including the completion claim their producer never made.
        """
        return {
            "provider_completion_claim": False,
            "origin": CANDIDATE_ORIGIN_TIMED_OUT,
            "failure_class": ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED.value,
            "resource_id": resource_id,
            "attempt": attempt,
            "files_added": list(delta.files_added),
            "files_modified": list(delta.files_modified),
            "files_deleted": list(delta.files_deleted),
            "insertions": delta.insertions,
            "deletions": delta.deletions,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

    def _capture_timeout_candidate(
        self,
        run_dir: Path,
        attempts_dir: Path,
        attempt_record: Dict[str, Any],
        task_spec: TaskSpec,
        resource_id: str,
        delta: RepositoryDelta,
    ) -> Dict[str, Any]:
        """Preserves a budget-stopped candidate and marks it as needing governance.

        Written before any rollback can run, and deliberately separate from the
        provider's own result: `implementation/result.json` keeps success=false,
        so nothing here fabricates a completion the provider never claimed.
        """
        candidate = {
            "candidate_captured": True,
            "requires_governance": True,
            **self._timed_out_artifact_provenance(
                resource_id, attempt_record["attempt"], delta
            ),
            "schema": TIMEOUT_CANDIDATE_SCHEMA_VERSION,
        }
        attempt_dir = attempts_dir / f"{attempt_record['attempt']:02d}-{resource_id}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(attempt_dir / "candidate.json", candidate)
        (attempt_dir / "candidate.patch").write_text(delta.diff_content, encoding="utf-8")

        attempt_record["candidate"] = candidate
        self._persist_attempt_record(attempt_record, attempts_dir)

        # Publish it as the run's current diff so the existing review and
        # verification stages act on it exactly as they would a normal one.
        self._write_delta_patch(delta, run_dir / "implementation", run_dir)
        self._record_event(
            task_id=task_spec.task_id,
            agent_id=resource_id,
            action="timeout_candidate_captured",
            spec=task_spec,
            metadata={
                "attempt": attempt_record["attempt"],
                "origin": CANDIDATE_ORIGIN_TIMED_OUT,
                "provider_completion_claim": False,
                "files_changed": len(delta.files_modified) + len(delta.files_added),
                "insertions": delta.insertions,
                "deletions": delta.deletions,
            },
        )
        return candidate

    def _retain_salvage_artifact(
        self,
        attempts_dir: Path,
        attempt_record: Dict[str, Any],
        task_spec: TaskSpec,
        resource_id: str,
        delta: RepositoryDelta,
        baseline: GitBaseline,
    ) -> Dict[str, Any]:
        """Keeps a productive timeout's work as a bounded terminal fallback.

        Rollback must not mean forgetting. HOWLFRAM-SLOPFIX-07R rolled Codex's
        eligible EXECUTION_BUDGET_EXCEEDED artifact out of the tree so Claude
        could attempt from a clean baseline -- which was correct -- and then
        terminal-failed without ever reconsidering it, because salvage was only
        ever evaluated for whichever attempt happened to be last.

        The record lives on the attempt record beside the already-written
        partial_work.patch, and deliberately not in a `candidate.json`: while
        fresh failover remains this is not the run's candidate and
        `candidate_resource` must stay null. Live implementation state and
        retained salvageable artifacts are different things.
        """
        retained = {
            "retained": True,
            "eligibility": SALVAGE_ELIGIBLE,
            "promotion_status": SALVAGE_RETAINED,
            **self._timed_out_artifact_provenance(
                resource_id, attempt_record["attempt"], delta
            ),
            "patch_path": f"{attempt_record['evidence_dir']}/retained_salvage.patch",
            "patch_sha256": hashlib.sha256(
                delta.diff_content.encode("utf-8")
            ).hexdigest(),
            # Patch identity: what this artifact was captured against, so a
            # later promotion can prove it is being replayed onto the same
            # repository state rather than whatever happens to be present.
            "baseline_head": baseline.initial_commit_sha,
            "baseline_status_digest": hashlib.sha256(
                baseline.status_porcelain.encode("utf-8")
            ).hexdigest(),
            # Answered against the restored baseline, which is the only tree
            # whose answer means anything. Unknown until rollback has run.
            "replayable": None,
        }
        # Written under a name no attempt-recording path ever produces:
        # partial_work.patch belongs to whichever attempt last occupied this
        # slot, but the fallback must outlive a resumed re-attempt.
        attempt_dir = attempts_dir / f"{attempt_record['attempt']:02d}-{resource_id}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "retained_salvage.patch").write_text(
            delta.diff_content, encoding="utf-8"
        )
        attempt_record["retained_salvage"] = retained
        self._persist_attempt_record(attempt_record, attempts_dir)
        self._record_event(
            task_id=task_spec.task_id,
            agent_id=resource_id,
            action="retained_salvage_captured",
            spec=task_spec,
            metadata={
                "attempt": attempt_record["attempt"],
                "origin": CANDIDATE_ORIGIN_TIMED_OUT,
                "provider_completion_claim": False,
                "insertions": delta.insertions,
                "deletions": delta.deletions,
                "baseline_head": baseline.initial_commit_sha,
            },
        )
        return retained

    def _confirm_salvage_replayable(
        self,
        run_dir: Path,
        attempts_dir: Path,
        attempt_record: Dict[str, Any],
        task_spec: TaskSpec,
    ) -> bool:
        """Proves the retained patch still applies to the restored baseline.

        Deliberately run after rollback rather than before it: the clean
        baseline is the tree a promotion would replay onto. `git apply --check`
        never writes, so this cannot disturb the state the next provider is
        about to be handed. An artifact that fails here stays as evidence but
        can never be selected, so it can never be partially applied.
        """
        retained = attempt_record.get("retained_salvage")
        if not retained:
            return False
        patch_file = run_dir / retained["patch_path"]
        replayable = False
        if patch_file.is_file():
            replayable = run_git(
                self.target_repo, ["apply", "--check", str(patch_file)], 60
            ).returncode == 0
        retained["replayable"] = replayable
        if not replayable:
            retained["eligibility"] = SALVAGE_NOT_REPLAYABLE
        self._persist_attempt_record(attempt_record, attempts_dir)
        self._record_event(
            task_id=task_spec.task_id,
            agent_id=retained["resource_id"],
            action="retained_salvage_replay_checked",
            spec=task_spec,
            metadata={
                "attempt": retained["attempt"],
                "replayable": replayable,
                "eligibility": retained["eligibility"],
            },
        )
        return replayable

    @staticmethod
    def _iter_attempt_records(attempts_dir: Path):
        """Yields (record, retained_salvage) per attempt, in attempt order.

        Attempt evidence is the durable source of truth for retention, so both
        selection and supersession read it from disk rather than from memory --
        which is what makes a retained fallback survive an interruption for
        free.
        """
        if not attempts_dir.is_dir():
            return
        for attempt_dir in sorted(attempts_dir.iterdir()):
            record_file = attempt_dir / "attempt_record.json"
            if not record_file.is_file():
                continue
            try:
                record = safe_load_json(record_file) or {}
            except Exception:
                continue
            yield record, record.get("retained_salvage") or {}

    @staticmethod
    def _select_retained_salvage(attempts_dir: Path) -> Optional[Dict[str, Any]]:
        """Picks the fallback deterministically: most recent eligible wins.

        No scoring, no ranking, no size heuristic, no model judgment. This
        preserves the existing semantics -- a productive *final* timeout is
        already the candidate -- and merely extends the search backward to the
        nearest prior equivalent when the later attempts produced nothing.

        Records are walked in attempt order and the last match is kept, so the
        rule holds however many attempts a run is configured to allow.
        """
        chosen: Optional[Dict[str, Any]] = None
        for record, retained in GovernedTaskOrchestrator._iter_attempt_records(
            attempts_dir
        ):
            if (
                retained.get("retained")
                and retained.get("eligibility") == SALVAGE_ELIGIBLE
                and retained.get("promotion_status") == SALVAGE_RETAINED
            ):
                chosen = record
        return chosen

    def _recover_promoted_salvage(
        self,
        run_dir: Path,
        delta: RepositoryDelta,
        task_spec: TaskSpec,
    ) -> Optional[Dict[str, Any]]:
        """Finishes a promotion that was interrupted after the patch landed.

        The crash window between applying a retained patch and capturing the
        candidate leaves the artifact sitting in the tree with nothing
        describing it. Recovery has to prove the delta in the tree *is* that
        artifact, byte for byte, before crediting anyone for it: a resumed run
        whose delta came from a later provider -- or from a provider that
        simply succeeded -- must not have a rolled-back fragment's producer
        stamped onto it. Without that proof this would launder discarded work
        into an accepted implementation and certify a self-review as
        independent.

        Proven, it rebuilds exactly the candidate the uninterrupted path would
        have written, so `provider_completion_claim` stays false and the
        artifact keeps its timed-out origin.
        """
        attempts_dir = run_dir / "implementation" / "attempts"
        record = self._select_retained_salvage(attempts_dir)
        if record is None:
            return None
        retained = record["retained_salvage"]
        if hashlib.sha256(
            delta.diff_content.encode("utf-8")
        ).hexdigest() != retained.get("patch_sha256"):
            return None

        candidate = self._capture_timeout_candidate(
            run_dir=run_dir,
            attempts_dir=attempts_dir,
            attempt_record=record,
            task_spec=task_spec,
            resource_id=retained["resource_id"],
            delta=delta,
        )
        retained["promotion_status"] = SALVAGE_PROMOTED
        retained["promoted_at"] = datetime.now(timezone.utc).isoformat()
        record["transition"] = TRANSITION_CANDIDATE_GOVERNANCE
        self._persist_attempt_record(record, attempts_dir)
        self._record_event(
            task_id=task_spec.task_id,
            agent_id=retained["resource_id"],
            action="retained_salvage_promotion_recovered",
            spec=task_spec,
            metadata={
                "attempt": retained["attempt"],
                "resource_id": retained["resource_id"],
                "provider_completion_claim": False,
            },
        )
        return candidate

    def _supersede_retained_salvage(
        self,
        attempts_dir: Path,
        task_spec: TaskSpec,
    ) -> None:
        """Retires every retained fallback once an attempt genuinely succeeds.

        Provider-attested work outranks any fragment, so the fallbacks stop
        being options the moment one lands. Leaving them selectable would let a
        later resume reach for work that was deliberately superseded.
        """
        for record, retained in self._iter_attempt_records(attempts_dir):
            if retained.get("promotion_status") != SALVAGE_RETAINED:
                continue
            retained["promotion_status"] = SALVAGE_SUPERSEDED
            self._persist_attempt_record(record, attempts_dir)
            self._record_event(
                task_id=task_spec.task_id,
                agent_id=retained.get("resource_id", "control_plane"),
                action="retained_salvage_superseded",
                spec=task_spec,
                metadata={"attempt": retained.get("attempt")},
            )

    def _abandon_salvage_promotion(
        self,
        task_spec: TaskSpec,
        record: Dict[str, Any],
        attempts_dir: Path,
        reason: str,
    ) -> None:
        """Retires a fallback that was found but cannot be safely restored.

        Marked unusable rather than merely logged: an artifact left eligible
        would be re-selected by every subsequent resume, retrying a restore
        that has already been proven impossible.
        """
        retained = record["retained_salvage"]
        retained["eligibility"] = SALVAGE_UNUSABLE
        retained["abandoned_reason"] = reason
        self._persist_attempt_record(record, attempts_dir)
        self._record_event(
            task_id=task_spec.task_id,
            agent_id="control_plane",
            action="retained_salvage_promotion_failed",
            spec=task_spec,
            metadata={
                "attempt": retained.get("attempt"),
                "resource_id": retained.get("resource_id"),
                "reason": reason,
            },
        )

    def _promote_retained_salvage(
        self,
        run_dir: Path,
        attempts_dir: Path,
        task_spec: TaskSpec,
        baseline: GitBaseline,
        current_delta: RepositoryDelta,
        attempt_record: Optional[Dict[str, Any]] = None,
    ) -> Optional[Tuple[Dict[str, Any], str, RepositoryDelta]]:
        """Recovers the retained fallback once no attempt produced a result.

        This is governance of an artifact that already exists, not a new
        implementation attempt: no provider is re-invoked, no attempt record is
        added, and the bounded failover budget is untouched. Promoting Codex's
        retained artifact after attempt 3 failed does not create attempt 4.

        Every step fails closed. A fallback that cannot be proven to belong to
        this baseline is left as evidence rather than applied, and nothing is
        ever partially applied.
        """
        record = self._select_retained_salvage(attempts_dir)
        if record is None:
            return None
        # `record` is a snapshot read from disk. When the attempt that just
        # failed occupies the same evidence slot -- which a resumed run makes
        # ordinary, since routing is deterministic -- `attempt_record` is the
        # live view of that same file, and writing the snapshot back would
        # erase the rollback result recorded below it.
        if (
            attempt_record is not None
            and attempt_record.get("attempt") == record.get("attempt")
            and attempt_record.get("resource_id") == record.get("resource_id")
        ):
            attempt_record["retained_salvage"] = record["retained_salvage"]
            record = attempt_record
        retained = record["retained_salvage"]
        producer = retained["resource_id"]

        # The last attempt's state goes back through the sanctioned restore
        # path first, so pre-existing user work is put back before anything is
        # applied on top of it.
        restored_ok, restore_err = restore_repository_to_baseline(
            self.target_repo, baseline, current_delta
        )
        # The attempt that just failed still owes an honest rollback record.
        # Its work was undone here rather than in the failover branch, and
        # evidence that goes silent about it is what SLOPFIX-06 fixed.
        if attempt_record is not None:
            self._attach_rollback_result(
                attempt_record, attempts_dir, restored_ok, restore_err
            )
        if not restored_ok:
            self._abandon_salvage_promotion(
                task_spec, record, attempts_dir, f"baseline restore failed: {restore_err}"
            )
            return None

        head_proc = run_git(self.target_repo, ["rev-parse", "HEAD"], 30)
        head = head_proc.stdout.strip() if head_proc.returncode == 0 else ""
        if head != retained.get("baseline_head"):
            self._abandon_salvage_promotion(
                task_spec,
                record,
                attempts_dir,
                f"baseline HEAD mismatch: expected {retained.get('baseline_head')}, "
                f"got {head or 'unknown'}",
            )
            return None

        if hashlib.sha256(
            baseline.status_porcelain.encode("utf-8")
        ).hexdigest() != retained.get("baseline_status_digest"):
            self._abandon_salvage_promotion(
                task_spec,
                record,
                attempts_dir,
                "baseline state digest does not match the one captured with "
                "the artifact",
            )
            return None

        patch_file = run_dir / retained["patch_path"]
        if not patch_file.is_file():
            self._abandon_salvage_promotion(
                task_spec, record, attempts_dir, f"retained patch missing at {retained['patch_path']}"
            )
            return None
        patch_text = patch_file.read_text(encoding="utf-8")
        if hashlib.sha256(patch_text.encode("utf-8")).hexdigest() != retained.get(
            "patch_sha256"
        ):
            self._abandon_salvage_promotion(
                task_spec, record, attempts_dir, "retained patch digest does not match its record"
            )
            return None

        # Never `git apply` blind. Prove it applies, then apply it.
        if run_git(
            self.target_repo, ["apply", "--check", str(patch_file)], 60
        ).returncode != 0:
            self._abandon_salvage_promotion(
                task_spec, record, attempts_dir, "retained patch no longer applies to the baseline"
            )
            return None
        applied = run_git(self.target_repo, ["apply", str(patch_file)], 60)
        if applied.returncode != 0:
            restore_repository_to_baseline(self.target_repo, baseline)
            self._abandon_salvage_promotion(
                task_spec,
                record,
                attempts_dir,
                f"retained patch failed to apply: {(applied.stderr or '').strip()}",
            )
            return None

        self._fault(FAULT_DURING_SALVAGE_PROMOTION, run_dir, task_spec)

        promoted_delta = self._capture_scoped_delta(
            task_spec, baseline, producer, stage="salvage_promotion"
        )
        if promoted_delta.is_empty:
            restore_repository_to_baseline(self.target_repo, baseline)
            self._abandon_salvage_promotion(
                task_spec, record, attempts_dir, "restored artifact produced no task-attributable delta"
            )
            return None

        # From here the artifact is an ordinary governed candidate: the same
        # capture the existing terminal-timeout path uses, with the same
        # provider_completion_claim=false, entering the same review,
        # verification and authority stages.
        candidate = self._capture_timeout_candidate(
            run_dir=run_dir,
            attempts_dir=attempts_dir,
            attempt_record=record,
            task_spec=task_spec,
            resource_id=producer,
            delta=promoted_delta,
        )
        retained["promotion_status"] = SALVAGE_PROMOTED
        retained["promoted_at"] = datetime.now(timezone.utc).isoformat()
        record["transition"] = TRANSITION_CANDIDATE_GOVERNANCE
        self._persist_attempt_record(record, attempts_dir)
        # The attempt that ended the chain says so on its own record, rather
        # than leaving "an earlier fallback was governed instead" recoverable
        # only from the ledger.
        if attempt_record is not None and attempt_record is not record:
            attempt_record["next_selection"] = None
            attempt_record["next_selection_reason"] = (
                NEXT_SELECTION_SALVAGE_PROMOTED
            )
            attempt_record["transition"] = TRANSITION_CANDIDATE_GOVERNANCE
            self._persist_attempt_record(attempt_record, attempts_dir)
        self._record_event(
            task_id=task_spec.task_id,
            agent_id=producer,
            action="retained_salvage_promoted",
            spec=task_spec,
            metadata={
                "attempt": retained["attempt"],
                "resource_id": producer,
                "provider_completion_claim": False,
                "insertions": promoted_delta.insertions,
                "deletions": promoted_delta.deletions,
                "baseline_head": retained["baseline_head"],
            },
        )
        return candidate, producer, promoted_delta

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
            ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED,
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
        if value == "EXECUTION_BUDGET_EXCEEDED":
            return FAILURE_CLASS_EXECUTION_BUDGET_EXCEEDED
        if value in {
            "AUTHENTICATION_REQUIRED",
            "PROVIDER_UNAVAILABLE",
            "TRANSPORT_UNAVAILABLE",
            "MISSING_EXECUTABLE",
            "EXECUTION_PERMISSION_REQUIRED",
        }:
            return FAILURE_CLASS_PROVIDER_UNAVAILABLE
        return FAILURE_CLASS_ENGINEERING

    def _persist_effective_route(
        self,
        routing: Any,
        task_spec: TaskSpec,
        attempt_resource_id: str,
        route_status: str,
        accepted: bool,
        candidate_resource: Optional[str] = None,
        last_attempted_resource_id: Optional[str] = None,
    ) -> None:
        """Makes routing evidence on disk match who is actually implementing.

        This used to run only after a *successful* failover, so a run that
        failed over and then failed left route.json still naming the originally
        routed provider, with no effective_route.json at all -- durable evidence
        contradicting the run (HOWLFRAM-SLOPFIX-05). Routing becomes durable at
        every real handoff instead, and distinguishes the resource currently
        attempting implementation from one whose work was actually accepted.

        initial_route.json is never touched here; it stays the immutable record
        of what was routed first.
        """
        if self.config.provider_pool is None:
            return
        if "initial_route" not in routing.metadata:
            routing.metadata["initial_route"] = {
                "selected_agent_id": routing.selected_agent_id,
                "selected_agent_name": getattr(
                    routing, "selected_agent_name", routing.selected_agent_id
                ),
                "reviewer_resource_mapping": dict(
                    routing.metadata.get("reviewer_resource_mapping", {})
                ),
                "reviewer_resource_identities": dict(
                    routing.metadata.get("reviewer_resource_identities", {})
                ),
                "review_diversity_achieved": routing.metadata.get(
                    "review_diversity_achieved"
                ),
            }

        # Reviewers are recomputed against whoever is implementing now, so no
        # mapping can imply a provider reviews its own work. Until a candidate
        # is actually accepted the mapping is labelled provisional, because the
        # implementer may change again on the next hop.
        mapping, diversity = self.config.provider_pool.select_reviewers(
            attempt_resource_id,
            routing.recommended_reviewers,
            task=task_spec,
        )
        registry = self.config.provider_pool.registry
        identities = {
            role: registry.get_resource(resource_id).resource_identity().to_dict()
            for role, resource_id in mapping.items()
            if registry.get_resource(resource_id) is not None
        }
        routing.metadata["reviewer_resource_mapping"] = mapping
        routing.metadata["reviewer_resource_identities"] = identities
        routing.metadata["review_diversity_achieved"] = diversity
        routing.metadata["reviewer_mapping_status"] = (
            "CONFIRMED"
            if accepted
            else ("CANDIDATE_REVIEW" if candidate_resource else "PROVISIONAL")
        )
        routing.metadata["route_status"] = route_status
        # Promoting a retained fallback credits the resource that produced the
        # artifact, but the last resource that actually *ran* is still the one
        # the failover chain ended on. Conflating them would rewrite history as
        # though the producer had been the final implementation attempt.
        routing.metadata["current_attempt_resource"] = (
            last_attempted_resource_id or attempt_resource_id
        )
        routing.metadata["last_attempted_implementation_resource"] = (
            last_attempted_resource_id or attempt_resource_id
        )
        routing.metadata["initial_implementation_resource"] = (
            routing.metadata.get("initial_route", {}).get("selected_agent_id")
            or routing.selected_agent_id
        )
        # A candidate is work that exists and is being governed; an accepted
        # implementation is work that cleared review, verification and
        # authority. Conflating them let SLOPFIX-06's route evidence claim an
        # accepted implementer while the task was still under review.
        routing.metadata["candidate_resource"] = candidate_resource
        routing.metadata["accepted_implementation_resource"] = (
            attempt_resource_id if accepted else None
        )
        routing.metadata["final_implementation_resource"] = (
            attempt_resource_id if accepted else None
        )
        routing.metadata["final_route"] = {
            "selected_agent_id": attempt_resource_id,
            "accepted": accepted,
            "reviewer_resource_mapping": mapping,
            "reviewer_resource_identities": identities,
            "review_diversity_achieved": diversity,
        }
        routing.metadata["reassignment_reason"] = (
            f"Implementation moved from {routing.selected_agent_id} to "
            f"{attempt_resource_id}; reviewers recomputed to keep review "
            f"independent of the implementer"
        )

        run_dir = self.target_repo / ".task_runs" / task_spec.task_id
        if run_dir.exists():
            atomic_write_json(run_dir / "route.json", asdict(routing))
            effective = asdict(routing)
            effective["selected_agent_id"] = attempt_resource_id
            atomic_write_json(run_dir / "effective_route.json", effective)

        self._record_event(
            task_id=task_spec.task_id,
            agent_id="control_plane",
            action="effective_route_updated",
            spec=task_spec,
            metadata={
                "initial_implementation_resource": routing.selected_agent_id,
                "attempt_resource": attempt_resource_id,
                "candidate_resource": candidate_resource,
                "accepted_implementation_resource": (
                    attempt_resource_id if accepted else None
                ),
                "route_status": route_status,
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

    def _fault(self, stage: str, run_dir: Path, task_spec: TaskSpec) -> None:
        """Fires the configured failure-injection hook for a lifecycle point.

        Used to prove the acquisition/cleanup boundary holds: a test raises from
        any of these points and asserts no lock file survives.
        """
        if self.config.failure_injection_hook:
            self.config.failure_injection_hook(stage, run_dir, task_spec)

    def run(
        self,
        task_spec: TaskSpec,
        planned_actions: Optional[List[str]] = None,
        lock_ownership: Optional[LockOwnership] = None,
    ) -> OrchestrationResult:
        """
        Executes the complete governed control-plane loop for the task under
        mutual-exclusion locks and durable checkpoint guarantees.

        `lock_ownership` is the task-lock token of an outer lifecycle that
        already owns this run -- `ai resume` holds the task lock across the
        whole recovery, then hands it here. Without it a resumed run would
        deadlock against itself (HOWLFRAM-SLOPFIX-06).
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

        # Every acquisition below is registered for release the moment it
        # succeeds. Previously the repo lock was taken before the task lock but
        # outside the try/finally, so a task-lock failure stranded
        # `.git/howlplane.lock` and no later run could proceed
        # (HOWLFRAM-SLOPFIX-06). Progress starts only once the locks are held,
        # so a resume that cannot acquire never overwrites the durable
        # progress record of the run it was trying to recover.
        with ExitStack() as stack:
            if self.config.acquire_locks:
                repo_lock = RepoLock(
                    self.target_repo,
                    task_spec.task_id,
                    command=f"ai work {task_spec.task_id} --execute",
                )
                repo_lock.acquire()
                stack.callback(repo_lock.release)
                self._fault("after_repo_lock", run_dir, task_spec)

                task_lock = TaskLock(
                    self.target_repo, task_spec.task_id, operation="orchestrate"
                )
                task_lock.acquire(lock_ownership)
                stack.callback(task_lock.release)
                self._fault("after_task_lock", run_dir, task_spec)

            progress.start(
                task_id=task_spec.task_id,
                run_dir=run_dir,
                initial_phase=TaskPhase.PREPARING.value,
            )
            stack.callback(progress.close)
            self._fault("after_progress_start", run_dir, task_spec)

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
                raise

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

        # Declared for the whole loop, not just the branch that runs providers:
        # a recovered implementation skips the attempt loop entirely, and later
        # stages still need to know who (if anyone) is credited with the work.
        final_impl_resource_id: Optional[str] = None

        # Stays None on the crash-recovery path, where an interrupted
        # implementation's delta is reconciled rather than re-run: there is no
        # provider execution in *this* process to report (#59.1 Phase 1).
        impl_res: Optional[AgentExecutionResult] = None
        implementation_attempts: List[Dict[str, Any]] = []
        # Declared out here because a resumed run can skip the attempt loop
        # entirely and still needs to report failover accounting.
        last_selection_decision: Optional[Any] = None
        # Set only when a budget-stopped attempt left a delta worth governing.
        timeout_candidate: Optional[Dict[str, Any]] = None
        # Set only when a retained fallback is promoted: the producer becomes
        # the candidate, but the failover chain still ended on this resource.
        salvage_last_attempted_id: Optional[str] = None

        if has_existing_delta and rec_delta:
            current_delta = rec_delta
            initial_delta = rec_delta
            # A resumed run reconciles the delta instead of re-running the
            # provider, so who produced it is reconstructed from the durable
            # route rather than re-derived. Without this the later stages had
            # no implementer to credit and acceptance could never be recorded.
            final_impl_resource_id = self._recover_implementation_resource(
                run_dir, routing
            )
            # A parked candidate is the strongest form of recovered work. If
            # there is none, the delta in the tree may be a retained fallback
            # whose promotion was interrupted -- but only if it actually is
            # that artifact, which _recover_promoted_salvage proves before
            # crediting its producer.
            timeout_candidate = self._load_parked_candidate(run_dir)
            if timeout_candidate is None:
                timeout_candidate = self._recover_promoted_salvage(
                    run_dir, current_delta, task_spec
                )
                if timeout_candidate is not None:
                    final_impl_resource_id = timeout_candidate["resource_id"]
                    # The durable route already records which resource the
                    # failover chain ended on. Crediting the producer as the
                    # candidate must not overwrite that with the producer.
                    salvage_last_attempted_id = routing.metadata.get(
                        "last_attempted_implementation_resource"
                    )
            self._record_event(
                task_id=task_spec.task_id,
                agent_id="control_plane",
                action="implementation_recovered",
                spec=task_spec,
                metadata={
                    "files_changed": len(current_delta.files_modified) + len(current_delta.files_added),
                    "recovered_implementation_resource": final_impl_resource_id,
                    "candidate_recovered": timeout_candidate is not None,
                },
            )
            if task_spec.current_state == "planned":
                task_spec.transition_to(
                    "implementing",
                    "Recovered existing implementation delta from interrupted run",
                )
                task_spec.save_to_file(str(run_dir / "task.yaml"))
            impl_chk = CheckpointManager._find_latest_checkpoint_for_stage(run_dir, "implementing")
            if impl_chk and impl_chk.status == "in_progress":
                CheckpointManager.complete_stage(
                    run_dir,
                    "implementing",
                    result_summary={"recovered": True, "resource_id": final_impl_resource_id},
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

            def fail_implementation(
                err_msg: str,
                failure_class: str,
                exit_code: int = 1,
                termination_reason: str = TERMINATION_NON_FAILOVER_FAILURE,
                rollback: bool = True,
            ) -> OrchestrationResult:
                """Terminal-fails the run from inside the implementation attempt loop.

                Reads the attempt-scoped locals (delta, provider execution, attempt
                records) at call time, so every terminal path reports the state as of
                the attempt that failed.

                Rollback used to sit at the end of the failover branch, where it
                only ever ran to prepare a clean tree for a *next* attempt. A
                terminal attempt therefore left its edits behind and its record
                carried no rollback key at all, contaminating the next run's
                starting state (HOWLFRAM-SLOPFIX-05). Undoing it here instead
                makes every terminal implementation failure restore the
                pre-task baseline, and say so. Evidence is already written by
                this point, so the patch survives the restore.
                """
                # Unconditional, exactly like the inter-attempt rollback: a
                # task-attributable delta can be empty while the provider has
                # still clobbered a file the operator had already modified,
                # which capture_delta deliberately excludes. Only the baseline
                # restore knows how to put those back.
                if current_impl_resource_id != routing.selected_agent_id:
                    self._persist_effective_route(
                        routing,
                        task_spec,
                        current_impl_resource_id,
                        ROUTE_STATUS_IMPLEMENTATION_FAILED,
                        accepted=False,
                    )
                if rollback:
                    restored_ok, restore_err = restore_repository_to_baseline(
                        self.target_repo, baseline, current_delta
                    )
                    self._attach_rollback_result(
                        attempt_record, attempts_dir, restored_ok, restore_err
                    )
                    if not restored_ok:
                        err_msg = (
                            f"{err_msg} Repository could not be restored to baseline "
                            f"({restore_err}); task-attributable changes remain."
                        )
                return self._fail_task(
                    task_spec,
                    run_dir,
                    err_msg,
                    start_time,
                    exit_code=exit_code,
                    agent_id="control_plane",
                    progress_tracker=progress,
                    stage="implementing",
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

            def govern_candidate() -> Dict[str, Any]:
                """Preserves the current attempt's delta as a governed candidate.

                Reads the attempt-scoped locals at call time, the same way
                fail_implementation does, so both terminal exits salvage
                identically.
                """
                captured = self._capture_timeout_candidate(
                    run_dir=run_dir,
                    attempts_dir=attempts_dir,
                    attempt_record=attempt_record,
                    task_spec=task_spec,
                    resource_id=current_impl_resource_id,
                    delta=current_delta,
                )
                self._record_delta_captured(task_spec, current_delta)
                return captured

            def promote_retained_fallback() -> Optional[str]:
                """Recovers a retained fallback at a terminal exit.

                Reads the attempt-scoped locals at call time, exactly like
                fail_implementation and govern_candidate, so all three terminal
                exits salvage identically instead of repeating the sequence.
                Returns the producing resource, or None to fail as before.
                """
                nonlocal timeout_candidate, current_delta
                promoted = self._promote_retained_salvage(
                    run_dir=run_dir,
                    attempts_dir=attempts_dir,
                    task_spec=task_spec,
                    baseline=baseline,
                    current_delta=current_delta,
                    attempt_record=attempt_record,
                )
                if promoted is None:
                    return None
                timeout_candidate, producer, current_delta = promoted
                self._record_delta_captured(task_spec, current_delta)
                return producer

            for attempt_num in range(1, self.config.max_provider_failover_attempts + 1):
                impl_res = None
                normalized_failure = None

                impl_backend = self._resolve_backend(current_impl_resource_id)

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

                    attempt_scratch = self._provider_scratch_dir(
                        run_dir, attempt_num, current_impl_resource_id
                    )
                    attempt_scratch.mkdir(parents=True, exist_ok=True)
                    self._record_scratch_manifest(
                        run_dir,
                        task_spec.task_id,
                        attempt_num,
                        current_impl_resource_id,
                        attempt_scratch,
                        status="active",
                    )
                    evidence_root_before = {
                        entry.name for entry in run_dir.iterdir()
                    }
                    attempts_before = {
                        entry.name for entry in attempts_dir.iterdir()
                    } if attempts_dir.is_dir() else set()

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

                # Relocate provider scratch before any evidence for this
                # attempt is written, so control-plane artifacts can never be
                # mistaken for files the provider left behind.
                if impl_backend.is_available():
                    swept = self._sweep_provider_scratch(
                        run_dir=run_dir,
                        resource_id=current_impl_resource_id,
                        attempt_num=attempt_num,
                        known_before=evidence_root_before,
                        attempts_dir=attempts_dir,
                        attempts_before=attempts_before,
                        owned_attempt_name=(
                            f"{attempt_num:02d}-{current_impl_resource_id}"
                        ),
                    )
                    if swept:
                        self._record_event(
                            task_id=task_spec.task_id,
                            agent_id=current_impl_resource_id,
                            action="provider_scratch_relocated",
                            spec=task_spec,
                            metadata={"attempt": attempt_num, "files": swept},
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
                    self._supersede_retained_salvage(attempts_dir, task_spec)
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
                    # EXECUTION_BUDGET_EXCEEDED is failover-eligible, so this
                    # attempt is never itself salvageable -- but an earlier one
                    # may be, and a provider erroring outright is still a
                    # termination without a successful implementation.
                    fallback_producer = promote_retained_fallback()
                    if fallback_producer is not None:
                        salvage_last_attempted_id = current_impl_resource_id
                        final_impl_resource_id = fallback_producer
                        break
                    return fail_implementation(
                        err_msg,
                        self._map_failure_class_to_orchestrator_class(normalized_failure),
                        exit_code=impl_res.exit_code if impl_res and impl_res.exit_code != 0 else 1,
                        termination_reason=TERMINATION_NON_FAILOVER_FAILURE,
                    )

                # While failover budget remains, a fresh provider may yet
                # produce a complete, provider-attested result, and that is
                # strictly better than governing a fragment -- so the current
                # attempt's work is still rolled back before the next one runs.
                # It is not discarded, though: SLOPFIX-07R rolled back Codex's
                # eligible artifact and then forgot it existed, because salvage
                # was only ever evaluated for whichever attempt happened to be
                # last. Retention keeps it recoverable without ever letting it
                # contaminate a later attempt.
                salvageable = self._is_salvageable_timeout_candidate(
                    normalized_failure, current_delta
                )
                exhausted = attempt_num >= self.config.max_provider_failover_attempts

                if exhausted and salvageable:
                    timeout_candidate = govern_candidate()
                    # State a terminal attempt reaches by exhausting the budget
                    # is still state: record it rather than letting the absence
                    # of `rollback`/`next_selection` imply it (SLOPFIX-06's
                    # attempt 3 was silent about all of it).
                    self._attach_terminal_disposition(
                        attempt_record,
                        attempts_dir=attempts_dir,
                        attempt_num=attempt_num,
                        attempted=attempted_impl_resource_ids,
                        task_spec=task_spec,
                    )
                    final_impl_resource_id = current_impl_resource_id
                    break

                if exhausted:
                    # This is the SLOPFIX-07R path: the last attempt produced
                    # nothing, but an earlier productive timeout may have.
                    fallback_producer = promote_retained_fallback()
                    if fallback_producer is not None:
                        salvage_last_attempted_id = current_impl_resource_id
                        final_impl_resource_id = fallback_producer
                        break
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
                    if salvageable:
                        timeout_candidate = govern_candidate()
                        final_impl_resource_id = current_impl_resource_id
                        break

                    fallback_producer = promote_retained_fallback()
                    if fallback_producer is not None:
                        salvage_last_attempted_id = current_impl_resource_id
                        final_impl_resource_id = fallback_producer
                        break

                    err_msg = f"Implementation failed on {current_impl_resource_id} ({failure_class_value}) and no eligible failover resource remains."
                    return fail_implementation(
                        err_msg,
                        FAILURE_CLASS_PROVIDER_EXHAUSTED,
                        termination_reason=TERMINATION_NO_ELIGIBLE_RESOURCE,
                    )

                # Preserve before destroying. Retention is recorded first so
                # an interruption between here and the rollback can only ever
                # leave a fallback that is known about, never one that was
                # silently dropped.
                if salvageable:
                    self._retain_salvage_artifact(
                        attempts_dir=attempts_dir,
                        attempt_record=attempt_record,
                        task_spec=task_spec,
                        resource_id=current_impl_resource_id,
                        delta=current_delta,
                        baseline=baseline,
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
                        rollback=False,
                    )

                # "Salvage preserved" and "rollback restored" are both true
                # here, and the evidence says both. The patch lives in the
                # attempt's evidence; the working tree is clean for the next
                # attempt. Replayability is proven against that clean tree,
                # because that is what a promotion would replay onto.
                if salvageable:
                    self._confirm_salvage_replayable(
                        run_dir, attempts_dir, attempt_record, task_spec
                    )
                    self._fault(FAULT_AFTER_SALVAGE_RETENTION, run_dir, task_spec)

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
                self._persist_effective_route(
                    routing,
                    task_spec,
                    current_impl_resource_id,
                    ROUTE_STATUS_SUPERSEDED,
                    accepted=False,
                )

            if timeout_candidate is None and (impl_res is None or not impl_res.success):
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
                    stage="implementing",
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

        # Whether the work came from this run's attempt loop or was recovered on
        # resume, reviewer independence is settled here. If the final
        # implementation resource differs from the initially
        # routed one, recompute reviewer assignments so independence claims
        # are truthful (#59.2 Phase 9).
        if (
            final_impl_resource_id is not None
            and self.config.provider_pool is not None
            and (
                final_impl_resource_id != routing.selected_agent_id
                # A promoted producer can coincide with the originally
                # routed resource, and reviewers must still be recomputed
                # against it before it can be reviewed.
                or salvage_last_attempted_id is not None
            )
        ):
            # Implementation being finished is not the same as the work
            # being accepted: review, reconciliation, verification and the
            # authority gate all still lie ahead. The route records who
            # produced the candidate and stays provisional until Stage 8
            # (HOWLFRAM-SLOPFIX-06).
            self._persist_effective_route(
                routing,
                task_spec,
                final_impl_resource_id,
                ROUTE_STATUS_SUPERSEDED,
                accepted=False,
                candidate_resource=final_impl_resource_id,
                last_attempted_resource_id=salvage_last_attempted_id,
            )

        # A salvaged candidate carries no completion claim from its
        # producer, so independent review is the only thing standing behind
        # it. When no independent reviewer is available the pool falls back
        # to the implementer reviewing its own work, which for this kind of
        # candidate is no review at all. Park it for a human rather than
        # completing on a self-review (HOWLFRAM-SLOPFIX-05).
        if timeout_candidate is not None and not routing.metadata.get(
            "review_diversity_achieved", True
        ):
            err_msg = (
                "Timed-out implementation candidate cannot be independently "
                f"reviewed: no reviewer is available other than "
                f"{final_impl_resource_id}, which produced it."
            )
            decision_pkt = HumanDecisionPacket(
                task_id=task_spec.task_id,
                objective=task_spec.objective,
                change_summary=(
                    f"Candidate ({current_delta.insertions} ins, "
                    f"{current_delta.deletions} del) was left by "
                    f"{final_impl_resource_id} after this control plane "
                    "stopped it at its execution budget. The provider never "
                    "reported completion."
                ),
                boundary_triggers=["reviewer_independence_unavailable"],
                evidence=[
                    f"origin={CANDIDATE_ORIGIN_TIMED_OUT}",
                    "provider_completion_claim=false",
                ],
                risks=[
                    "No independent reviewer is available, so the candidate "
                    "has nothing standing behind it.",
                ],
                review_findings_summary={
                    "blocker": 0, "high": 0, "medium": 0, "low": 0,
                },
                verification_status="unverified",
                recommended_action=(
                    "Inspect implementation/attempts/*/candidate.patch and "
                    "either authorize it explicitly or discard it."
                ),
            )
            self._park_awaiting_human(
                task_spec, run_dir, err_msg, decision_pkt, progress
            )
            return self._make_result(
                task_spec=task_spec,
                final_state="awaiting_human",
                exit_code=2,
                start_time=start_time,
                run_dir=run_dir,
                routing=routing,
                initial_delta=initial_delta,
                current_delta=current_delta,
                verif_plan=verif_plan,
                hf_status=hf_audit_status,
                hf_match=hf_audit_match,
                provider_execution=impl_res,
                implementation_attempts=implementation_attempts,
                timeout_candidate=timeout_candidate,
            )

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
                implementer_resource_id=final_impl_resource_id,
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
            # Written unconditionally. A cycle with no findings leaves
            # cycle_res.reconciliation as None, and skipping the write used to
            # leave the *previous* cycle's report on disk -- so the decision
            # packet pointed operators at a finding that had already been
            # remediated (HOWLFRAM-BUG-50). An empty cycle now says it is empty.
            if cycle_res.reconciliation:
                (run_dir / "reconciliation.json").write_text(json.dumps(cycle_res.reconciliation.to_dict(), indent=2), encoding="utf-8")
                (run_dir / "reconciliation_report.md").write_text(cycle_res.reconciliation.render_markdown(), encoding="utf-8")
            else:
                (run_dir / "reconciliation.json").write_text(
                    json.dumps(
                        {
                            "summary": {"total_findings": 0, "unresolved_blockers": 0, "unresolved_highs": 0},
                            "findings": [],
                            "cycle_index": cycle_res.cycle_index,
                            "status": cycle_res.status,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                (run_dir / "reconciliation_report.md").write_text(
                    "# Review Reconciliation Report\n\n"
                    f"Cycle {cycle_res.cycle_index} produced no reviewer findings to reconcile "
                    f"(cycle status: `{cycle_res.status}`).\n",
                    encoding="utf-8",
                )

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
                # Name the cause that actually stopped the loop. Reporting every
                # escalation as "unresolved findings" told operators a change had
                # defects when the real problem was that a reviewer never ran, or
                # that the implementer reviewed its own diff -- three different
                # situations calling for three different human judgments
                # (HOWLFRAM-BUG-50).
                triggers: List[str] = []
                risks: List[str] = []
                evidence = [f.title for f in cycle_res.all_findings if f.severity in ("blocker", "high")]
                if findings_summary["high"] > 0 or findings_summary["blocker"] > 0:
                    triggers.append("security_policy_exception")
                    risks.append("Unresolved reviewer findings may indicate defects or security vulnerabilities.")
                if cycle_res.non_independent_roles:
                    triggers.append("non_independent_review")
                    risks.append(
                        "The implementer reviewed its own change for "
                        f"{', '.join(cycle_res.non_independent_roles)}; those roles are not independent review."
                    )
                    evidence.extend(
                        f"{role}: reviewed by the implementer ({final_impl_resource_id or 'unknown'})"
                        for role in cycle_res.non_independent_roles
                    )
                if cycle_res.status == "review_failure":
                    triggers.append("review_incomplete")
                    risks.append("At least one reviewer never completed, so this change is under-reviewed.")
                    evidence.extend(
                        f"{role}: {res.status}"
                        for role, res in sorted(cycle_res.reviewer_results.items())
                        if res.status == "reviewer_failure"
                    )
                if not triggers:
                    triggers.append("unresolved_findings")
                    risks.append("Unresolved reviewer findings may indicate defects or security vulnerabilities.")

                decision_pkt = HumanDecisionPacket(
                    task_id=task_spec.task_id,
                    objective=task_spec.objective,
                    change_summary=(
                        f"Implementation diff ({current_delta.insertions} ins, {current_delta.deletions} del) "
                        f"reached the remediation limit; triggers: {', '.join(triggers)}."
                    ),
                    boundary_triggers=triggers,
                    evidence=evidence,
                    risks=risks,
                    review_findings_summary=findings_summary,
                    verification_status="unverified",
                    recommended_action="Review finding report in reconciliation_report.md and authorize override or manual remediation.",
                )
                self._park_awaiting_human(
                    task_spec,
                    run_dir,
                    f"Max remediation cycles exceeded: {err_msg}",
                    decision_pkt,
                    progress,
                )
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

            rem_resource_id = (
                final_impl_resource_id or routing.selected_agent_id
            )
            rem_backend = self._resolve_backend(rem_resource_id)
            rem_agent_id = getattr(rem_backend, "agent_id", None) or rem_resource_id
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
                    rem_res = rem_backend.execute(
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

        verif_status = self._execute_verification_plan(
            verif_plan=verif_plan,
            task_spec=task_spec,
            baseline=baseline,
            delta=current_delta,
            run_dir=run_dir,
            progress=progress,
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
        if verif_status != "passed":
            failed_steps = [s.name for s in verif_plan.steps if s.status == "failed" and s.required]
            err_msg = f"Deterministic verification failed on required steps: {', '.join(failed_steps)}"
            # A salvaged candidate only ever existed on sufferance. Governance
            # has now rejected it, so the repository goes back to its pre-task
            # baseline rather than keeping a patch nothing stands behind. A
            # provider-attested implementation still keeps its diff in place for
            # inspection, as before.
            if timeout_candidate is not None and not current_delta.is_empty:
                restored_ok, restore_err = restore_repository_to_baseline(
                    self.target_repo, baseline, current_delta
                )
                self._record_event(
                    task_id=task_spec.task_id,
                    agent_id="control_plane",
                    action="timeout_candidate_rejected",
                    spec=task_spec,
                    metadata={
                        "stage": "verifying",
                        "origin": CANDIDATE_ORIGIN_TIMED_OUT,
                        "rollback_restored": restored_ok,
                        "rollback_error": restore_err,
                        "failed_steps": failed_steps,
                    },
                )
                if not restored_ok:
                    err_msg = (
                        f"{err_msg}. Repository could not be restored to baseline "
                        f"({restore_err}); candidate changes remain."
                    )
            return self._fail_task(
                task_spec,
                run_dir,
                err_msg,
                start_time,
                exit_code=1,
                progress_tracker=progress,
                stage="verifying",
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
                timeout_candidate=timeout_candidate,
            )

        CheckpointManager.complete_stage(
            run_dir,
            "verifying",
            output_artifacts=[str(run_dir / "verification_result.json")],
            result_summary=verif_summary,
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
            non_independent_roles=sorted(
                {role for cyc in review_cycles for role in cyc.non_independent_roles}
            ),
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
                TERMINATION_IMPLEMENTATION_SUCCEEDED
                if timeout_candidate is None
                else TERMINATION_TIMEOUT_CANDIDATE_GOVERNED,
                last_selection_decision,
            ),
            timeout_candidate=timeout_candidate,
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

        # The one place acceptance becomes true. Everything before this point
        # survived review, reconciliation, deterministic verification and the
        # human authority boundary; only now is there an accepted implementer.
        if final_impl_resource_id is not None:
            self._persist_effective_route(
                routing,
                task_spec,
                final_impl_resource_id,
                ROUTE_STATUS_SUPERSEDED
                if final_impl_resource_id != routing.metadata.get(
                    "initial_implementation_resource", routing.selected_agent_id
                )
                else ROUTE_STATUS_ACCEPTED,
                accepted=True,
                # Acceptance does not erase how the work arrived. A candidate
                # that cleared governance is still a candidate in provenance.
                candidate_resource=(
                    final_impl_resource_id if timeout_candidate else None
                ),
                # Acceptance names an accepted implementer; it does not
                # retroactively make a promoted fallback the last resource that
                # was actually attempted.
                last_attempted_resource_id=salvage_last_attempted_id,
            )

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
        self._prune_ephemeral_scratch(run_dir, task_spec.task_id)
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
        timeout_candidate: Optional[Dict[str, Any]] = None,
    ) -> OrchestrationResult:
        return OrchestrationResult(
            implementation_completion_claim=timeout_candidate is None,
            candidate_origin=(
                timeout_candidate.get("origin") if timeout_candidate else None
            ),
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
        # Capture the original active stage identity BEFORE modifying task_spec state
        active_stage = kwargs.pop("stage", None)
        if not active_stage or active_stage in ("failed", "cancelled"):
            active_stage = task_spec.current_state
        if not active_stage or active_stage in ("failed", "cancelled"):
            latest_chk = CheckpointManager.load_latest_checkpoint(run_dir)
            if latest_chk and latest_chk.stage not in ("failed", "cancelled"):
                active_stage = latest_chk.stage

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
                stage=active_stage,
                reason=err_msg,
                result_summary=summary,
            )
        except Exception:
            pass

        if progress_tracker is not None:
            progress_tracker.record_terminal(
                TaskProgressState.FAILED,
                TaskPhase.FAILED,
                error_message=err_msg,
            )
        task_spec.transition_to("failed", err_msg)
        task_spec.save_to_file(str(run_dir / "task.yaml"))

        self._record_event(
            task_id=task_spec.task_id,
            agent_id=agent_id,
            action="task_failed",
            result=err_msg,
            spec=task_spec,
        )
        self._prune_ephemeral_scratch(run_dir, task_spec.task_id)
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
        lines.append("")
        lines.append("## Workspace")
        lines.append(
            f"- Scratch files belong in `{self._attempt_workspace_hint(task)}`."
        )
        lines.append(
            f"- Everything else under `.task_runs/{task.task_id}/` is "
            "control-plane evidence. Do not create or edit files there."
        )
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

#!/usr/bin/env python3
"""
execution_trajectory.py

Durable, schema-versioned observable orchestration history for a governed task.

An ExecutionTrajectory is NOT hidden model chain-of-thought. It captures the
code-selected choices (provider, model, agent, reviewer topology, context
strategy, etc.), evidence references, and outcomes so that later reasoning
strategy experiments can be evaluated against real trajectory evidence.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.control_plane.atomic_io import safe_load_json
from src.control_plane.reasoning._json_store import DurableObjectStore
from src.control_plane.reasoning.artifact_safety import (
    ArtifactIntegrityError,
    SafeArtifactSerializationMixin,
    canonical_digest,
)

EXECUTION_TRAJECTORY_SCHEMA_VERSION_V1 = "howlplane.execution_trajectory/v1"
EXECUTION_TRAJECTORY_SCHEMA_VERSION = "howlplane.execution_trajectory/v2"


# Forward imports are kept local to avoid circular dependency with orchestrator.py.
def _import_orchestration_result():
    from src.control_plane.orchestrator import OrchestrationResult
    return OrchestrationResult

# Fields that may contain hidden model reasoning or unnecessary source dumps.
@dataclass
class ExecutionTrajectory(SafeArtifactSerializationMixin):
    """
    Observable orchestration history for one governed task execution.

    Only fields that are knowable from the control plane are required.
    Cost, latency, and model version are stored only when actually observed;
    they are never invented.
    """

    trajectory_id: str
    task_id: str
    schema_version: str = EXECUTION_TRAJECTORY_SCHEMA_VERSION
    campaign_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    experiment_id: Optional[str] = None
    task_class: Optional[str] = None
    objective: Optional[str] = None
    selected_provider: Optional[str] = None
    selected_model: Optional[str] = None
    selected_agent: Optional[str] = None
    selected_reviewers: List[str] = field(default_factory=list)
    prompt_strategy_id: Optional[str] = None
    context_strategy_id: Optional[str] = None
    retrieval_strategy_id: Optional[str] = None
    tool_strategy_id: Optional[str] = None
    decomposition_strategy_id: Optional[str] = None
    review_strategy_id: Optional[str] = None
    verification_strategy_id: Optional[str] = None
    input_evidence_refs: List[str] = field(default_factory=list)
    selected_context_refs: List[str] = field(default_factory=list)
    actions_attempted: List[str] = field(default_factory=list)
    tools_invoked: List[str] = field(default_factory=list)
    provider_events: List[Dict[str, Any]] = field(default_factory=list)
    resource_selection: Optional[Dict[str, Any]] = None
    role_selections: List[Dict[str, Any]] = field(default_factory=list)
    capacity_after: Dict[str, str] = field(default_factory=dict)
    review_findings: List[Dict[str, Any]] = field(default_factory=list)
    verification_results: Optional[Dict[str, Any]] = None
    repair_cycles: List[Dict[str, Any]] = field(default_factory=list)
    final_status: Optional[str] = None
    outcome: Optional[str] = None
    cost_if_available: Optional[float] = None
    latency_if_available: Optional[float] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None
    content_digest: str = ""
    # Hidden reasoning fields are accepted during construction only so they can
    # be explicitly stripped by to_dict(); they are never persisted.
    hidden_reasoning: Optional[str] = None
    chain_of_thought: Optional[str] = None
    raw_prompt: Optional[str] = None
    private_notes: Optional[str] = None
    internal_thoughts: Optional[str] = None

    def __post_init__(self):
        if not self.content_digest:
            self.content_digest = self.compute_content_digest()

    def compute_content_digest(self) -> str:
        """Deterministic digest over the durable content of this trajectory."""
        payload = self.to_dict()
        if self.schema_version == EXECUTION_TRAJECTORY_SCHEMA_VERSION_V1:
            for field_name in (
                "resource_selection", "role_selections", "capacity_after"
            ):
                payload.pop(field_name, None)
        return canonical_digest(payload, "content_digest")

    def finalize(self, final_status: str, outcome: str) -> None:
        """Marks the trajectory complete and recomputes its digest."""
        if self.completed_at:
            if self.final_status == final_status and self.outcome == outcome:
                return
            raise ArtifactIntegrityError("Completed trajectory evidence is immutable.")
        self.final_status = final_status
        self.outcome = outcome
        self.completed_at = datetime.now(timezone.utc).isoformat()
        self.content_digest = self.compute_content_digest()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionTrajectory":
        d = dict(data)
        if d.get("schema_version") not in {
            EXECUTION_TRAJECTORY_SCHEMA_VERSION,
            EXECUTION_TRAJECTORY_SCHEMA_VERSION_V1,
        }:
            raise ArtifactIntegrityError("Unsupported execution trajectory schema.")
        trajectory = cls(**d)
        if not trajectory.verify_digest():
            raise ArtifactIntegrityError(
                f"Execution trajectory '{trajectory.trajectory_id}' digest mismatch."
            )
        return trajectory

    def verify_digest(self) -> bool:
        """Verifies that the stored content_digest matches recomputed digest."""
        return self.compute_content_digest() == self.content_digest


class TrajectoryStore(DurableObjectStore):
    """
    Durable, atomic store for ExecutionTrajectory records.

    Trajectories are written to a directory structure that supports resumption:
    the store refuses to overwrite an existing trajectory with the same id, so
    repeated event ingestion does not duplicate records.
    """

    _filename_suffix = ".json"

    def __init__(self, base_dir: Union[str, Path]):
        super().__init__(
            base_dir,
            factory=ExecutionTrajectory.from_dict,
            dedup_field="content_digest",
        )

    def save(self, trajectory: ExecutionTrajectory) -> Path:
        """Atomically persists a trajectory; idempotent on repeated calls."""
        target = self._path(trajectory.trajectory_id)
        if target.is_file():
            existing = self.load(trajectory.trajectory_id)
            if existing.content_digest == trajectory.content_digest:
                return target
            existing_payload = existing.to_dict()
            incoming_payload = trajectory.to_dict()
            for payload in (existing_payload, incoming_payload):
                payload.pop("created_at", None)
                payload.pop("completed_at", None)
                payload.pop("content_digest", None)
            if existing_payload == incoming_payload:
                return target
        return super().save(trajectory.trajectory_id, trajectory.to_dict())

    def load(self, trajectory_id: str) -> ExecutionTrajectory:
        return self._factory(safe_load_json(self._path(trajectory_id)))

    def load_for_task(self, task_id: str) -> List[ExecutionTrajectory]:
        return [t for t in self.list_all() if t.task_id == task_id]


def summarize_for_experiment(trajectory: ExecutionTrajectory) -> Dict[str, Any]:
    """
    Produces a compact, deterministic summary of a trajectory for experiment
    comparison. Omits large diff/prompt content by reference only.
    """
    return {
        "trajectory_id": trajectory.trajectory_id,
        "task_id": trajectory.task_id,
        "selected_provider": trajectory.selected_provider,
        "selected_agent": trajectory.selected_agent,
        "selected_reviewers": trajectory.selected_reviewers,
        "final_status": trajectory.final_status,
        "outcome": trajectory.outcome,
        "repair_cycles_count": len(trajectory.repair_cycles),
        "verification_status": (
            trajectory.verification_results.get("overall_status") if trajectory.verification_results else None
        ),
        "confirmed_defects": sum(
            1 for finding in trajectory.review_findings
            if finding.get("status") in ("confirmed", "fixed", "accepted")
        ),
        "escaped_defects": (
            trajectory.verification_results.get("escaped_defects")
            if trajectory.verification_results else None
        ),
        "reproducible": (
            trajectory.verification_results.get("reproducible")
            if trajectory.verification_results else None
        ),
        "cost_if_available": trajectory.cost_if_available,
        "latency_if_available": trajectory.latency_if_available,
        "content_digest": trajectory.content_digest,
    }


def _safe_provider_event(provider_execution: Optional[Any]) -> Dict[str, Any]:
    if provider_execution is None:
        return {}
    d = provider_execution.to_dict() if hasattr(provider_execution, "to_dict") else dict(provider_execution)
    # Drop raw stdout/stderr to avoid duplicating large agent transcripts; keep
    # metadata and reference identifiers.
    d.pop("stdout", None)
    d.pop("stderr", None)
    return d


def _extract_provider_events(result: Any) -> List[Dict[str, Any]]:
    """Collects observable provider calls with their truthful execution role."""
    events: List[Dict[str, Any]] = []
    implementation = _safe_provider_event(result.provider_execution)
    if implementation:
        events.append(implementation)
    for cycle in result.review_cycles or []:
        for review in cycle.reviewer_results.values():
            event = _safe_provider_event(getattr(review, "agent_result", None))
            if event:
                events.append(event)
    return events


def _extract_review_findings(review_cycles: Optional[List[Any]]) -> List[Dict[str, Any]]:
    if not review_cycles:
        return []
    findings: List[Dict[str, Any]] = []
    for cycle in review_cycles:
        cycle_findings = getattr(cycle, "all_findings", None)
        if cycle_findings is None:
            cycle_findings = cycle.get("all_findings", []) if hasattr(cycle, "get") else []
        for f in cycle_findings:
            fd = f.to_dict() if hasattr(f, "to_dict") else dict(f)
            findings.append(fd)
    return findings


def _extract_repair_cycles(review_cycles: Optional[List[Any]]) -> List[Dict[str, Any]]:
    if not review_cycles:
        return []
    cycles: List[Dict[str, Any]] = []
    for idx, cycle in enumerate(review_cycles, 1):
        if idx > 1:
            all_findings = getattr(cycle, "all_findings", None)
            if all_findings is None and hasattr(cycle, "get"):
                all_findings = cycle.get("all_findings", [])
            cycles.append({
                "cycle_index": idx - 1,
                "finding_count": len(all_findings or []),
            })
    return cycles


def _extract_role_selections(result: Any) -> List[Dict[str, Any]]:
    """Returns truthful implementation and review role resource evidence."""
    routing = result.routing_decision
    selections: List[Dict[str, Any]] = []
    if result.resource_selection:
        selections.append(dict(result.resource_selection))
    if not routing:
        return selections
    identities = routing.metadata.get("reviewer_resource_identities", {})
    for cycle in result.review_cycles or []:
        for role, review in cycle.reviewer_results.items():
            attempts = list(getattr(review, "attempts", []) or [])
            if attempts:
                for attempt in attempts:
                    identity = identities.get(role, {})
                    selections.append({
                        "role": role,
                        "resource_id": attempt.get("provider"),
                        "provider_id": identity.get("provider_id"),
                        "interface_id": identity.get("interface_id"),
                        "model_id": identity.get("model_id"),
                        "outcome": attempt.get("outcome"),
                    })
            elif role in identities:
                selections.append({"role": role, **identities[role]})
    return selections


class ExecutionTrajectoryBuilder:
    """Builds an ExecutionTrajectory from an OrchestrationResult."""

    @classmethod
    def from_orchestration_result(cls, result: Any) -> ExecutionTrajectory:
        OrchestrationResult = _import_orchestration_result()
        if not isinstance(result, OrchestrationResult):
            raise TypeError("Expected OrchestrationResult")

        task_spec = result.task_spec
        routing = result.routing_decision
        provider_exec = result.provider_execution
        identity = task_spec.metadata.get("trajectory_id")
        if not identity:
            event_key = task_spec.metadata.get("trajectory_event_id") or result.run_dir
            event_key = event_key or ":".join([
                task_spec.task_id,
                str(task_spec.metadata.get("experiment_id", "standalone")),
                str(task_spec.metadata.get("experiment_arm", "run")),
                str(task_spec.metadata.get("experiment_sample_id", "0")),
            ])
            identity = f"traj-{canonical_digest({'event': event_key})[:24]}"
        traj = ExecutionTrajectory(
            trajectory_id=identity,
            task_id=task_spec.task_id,
            campaign_id=task_spec.metadata.get("campaign_id"),
            opportunity_id=task_spec.metadata.get("opportunity_id"),
            experiment_id=task_spec.metadata.get("experiment_id"),
            task_class=task_spec.task_class,
            objective=task_spec.objective,
            selected_provider=provider_exec.agent_id if provider_exec else (routing.selected_agent_id if routing else None),
            selected_agent=routing.selected_agent_id if routing else None,
            selected_reviewers=list(routing.recommended_reviewers) if routing else [],
            prompt_strategy_id=task_spec.metadata.get("prompt_strategy_id"),
            context_strategy_id=task_spec.metadata.get("context_strategy_id"),
            retrieval_strategy_id=task_spec.metadata.get("retrieval_strategy_id"),
            tool_strategy_id=task_spec.metadata.get("tool_strategy_id"),
            decomposition_strategy_id=task_spec.metadata.get("decomposition_strategy_id"),
            review_strategy_id=task_spec.metadata.get("review_strategy_id"),
            verification_strategy_id=task_spec.metadata.get("verification_strategy_id"),
            input_evidence_refs=task_spec.metadata.get("input_evidence_refs", []),
            selected_context_refs=task_spec.metadata.get("selected_context_refs", []),
            actions_attempted=task_spec.metadata.get("actions_attempted", []),
            tools_invoked=task_spec.metadata.get("tools_invoked", []),
            provider_events=_extract_provider_events(result),
            resource_selection=result.resource_selection,
            role_selections=_extract_role_selections(result),
            capacity_after=dict(result.capacity_after),
            review_findings=_extract_review_findings(result.review_cycles),
            verification_results=result.verification_plan.to_dict() if result.verification_plan else None,
            repair_cycles=_extract_repair_cycles(result.review_cycles),
            final_status=result.final_state,
            outcome="success" if result.final_state == "complete" else (result.failure_class or "failed"),
            cost_if_available=(
                provider_exec.metadata.get("cost_usd")
                if provider_exec and isinstance(getattr(provider_exec, "metadata", None), dict)
                else None
            ),
            latency_if_available=(
                result.duration_seconds if result.duration_seconds > 0 else None
            ),
        )
        if provider_exec and hasattr(provider_exec, "metadata") and isinstance(provider_exec.metadata, dict):
            traj.selected_model = provider_exec.metadata.get("model")
        traj.finalize(final_status=traj.final_status, outcome=traj.outcome)
        return traj

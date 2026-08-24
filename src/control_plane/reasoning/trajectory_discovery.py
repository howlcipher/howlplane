#!/usr/bin/env python3
"""
trajectory_discovery.py

Mines durable ExecutionTrajectory records for evidence-backed observations that
can feed the existing SEEK/OBSERVE backlog (issues.md / improvements.md).

A trajectory-derived observation is NOT an immediate behavior change. It enters
the same fingerprint, deduplication, challenge, experiment, evaluation process as
other discoveries.
"""

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from src.control_plane.atomic_io import atomic_write_json, safe_load_json
from src.control_plane.reasoning._json_store import DurableObjectStore
from src.control_plane.reasoning.execution_trajectory import ExecutionTrajectory
from src.control_plane.reasoning.reasoning_experiment import (
    ReasoningExperiment,
    ReasoningExperimentStore,
)
from src.control_plane.reasoning.strategy_registry import StrategyRegistry
from src.control_plane.task_spec import DataClassSerializationMixin

TRAJECTORY_OBSERVATION_SCHEMA_VERSION = "howlplane.trajectory_observation/v1"

# Minimum occurrences of a pattern before it becomes an observation candidate.
MIN_PATTERN_OCCURRENCES = 2


class ObservationStatus:
    OPEN = "open"
    DEFERRED = "deferred"
    FALSIFIED = "falsified"
    INCONCLUSIVE = "inconclusive"
    EXPERIMENTING = "experimenting"


@dataclass
class TrajectoryObservation(DataClassSerializationMixin):
    """
    Evidence-backed observation derived from one or more execution trajectories.

    `suggested_experiment` references strategy IDs but does NOT itself change
    routing or defaults. It is a backlog item for the existing challenge/experiment
    flow.
    """

    observation_id: str
    fingerprint: str
    category: str
    title: str
    description: str
    evidence_refs: List[str] = field(default_factory=list)
    occurrence_count: int = 0
    task_classes: List[str] = field(default_factory=list)
    suggested_experiment_type: Optional[str] = None
    suggested_baseline_strategy_id: Optional[str] = None
    suggested_candidate_strategy_id: Optional[str] = None
    status: str = ObservationStatus.OPEN
    reopened_by_evidence_refs: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema_version: str = TRAJECTORY_OBSERVATION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrajectoryObservation":
        d = dict(data)
        d.pop("schema_version", None)
        return cls(**d)

    def reopen(self, new_evidence_refs: List[str]) -> None:
        """Reopens a previously deferred/falsified observation with explicit new evidence."""
        self.status = ObservationStatus.OPEN
        self.reopened_by_evidence_refs = list(new_evidence_refs)
        self.updated_at = datetime.now(timezone.utc).isoformat()


def _fingerprint(pattern_name: str, keys: List[str]) -> str:
    canonical = json.dumps({"pattern": pattern_name, "keys": sorted(keys)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _find_architecture_omissions(trajectories: List[ExecutionTrajectory]) -> List[TrajectoryObservation]:
    """Detects repeated context that excludes architecture for cross-module work."""
    candidates: Dict[str, Dict[str, Any]] = {}
    for t in trajectories:
        if t.final_status != "success" and t.context_strategy_id in (
            "context.changed_files_only/v1",
            "context.task_plus_acceptance/v1",
        ):
            # Approximate cross-module signal: task_class and modified files > 1.
            modified_count = len(
                [f for f in (t.verification_results or {}).get("files_modified", [])]
                if isinstance(t.verification_results, dict)
                else []
            )
            if modified_count > 1 or (t.task_class in ("feature", "refactor", "infrastructure")):
                key = f"{t.task_class}:{t.context_strategy_id}"
                candidates.setdefault(key, {
                    "trajectories": [],
                    "task_class": t.task_class,
                    "context_strategy_id": t.context_strategy_id,
                })
                candidates[key]["trajectories"].append(t.trajectory_id)

    observations: List[TrajectoryObservation] = []
    for key, info in candidates.items():
        if len(info["trajectories"]) >= MIN_PATTERN_OCCURRENCES:
            fingerprint = _fingerprint("architecture_omission", [info["task_class"], info["context_strategy_id"]])
            observations.append(TrajectoryObservation(
                observation_id=f"OBS-ARCH-OMIT-{fingerprint}",
                fingerprint=fingerprint,
                category="context_weakness",
                title="Repeated architecture omission for cross-module work",
                description=(
                    f"{len(info['trajectories'])} trajectories with task_class={info['task_class']} "
                    f"used context strategy {info['context_strategy_id']} and did not succeed. "
                    "Architecture context may be under-weighted."
                ),
                evidence_refs=info["trajectories"],
                occurrence_count=len(info["trajectories"]),
                task_classes=[info["task_class"]] if info["task_class"] else [],
                suggested_experiment_type="CONTEXT",
                suggested_baseline_strategy_id=info["context_strategy_id"],
                suggested_candidate_strategy_id="context.changed_files_plus_architecture/v1",
            ))
    return observations


def _find_reviewer_dismissal(trajectories: List[ExecutionTrajectory]) -> List[TrajectoryObservation]:
    """Detects repeated dismissal or override of a reviewer role."""
    counts: Dict[str, int] = {}
    refs: Dict[str, List[str]] = {}
    for t in trajectories:
        for finding in t.review_findings:
            role = finding.get("reviewer_role")
            status = finding.get("status")
            if role and status in ("false_positive", "out_of_scope", "disputed"):
                counts[role] = counts.get(role, 0) + 1
                refs.setdefault(role, []).append(t.trajectory_id)

    observations: List[TrajectoryObservation] = []
    for role, count in counts.items():
        if count >= MIN_PATTERN_OCCURRENCES:
            fingerprint = _fingerprint("reviewer_dismissal", [role])
            observations.append(TrajectoryObservation(
                observation_id=f"OBS-REVIEW-DISMISS-{fingerprint}",
                fingerprint=fingerprint,
                category="reviewer_calibration",
                title=f"Reviewer role '{role}' repeatedly dismissed or overridden",
                description=(
                    f"{count} findings from '{role}' were dismissed or disputed across trajectories. "
                    "Reviewer/task-class calibration may be poor."
                ),
                evidence_refs=refs[role],
                occurrence_count=count,
                suggested_experiment_type="REVIEW_TOPOLOGY",
                suggested_baseline_strategy_id="review.correctness_security_split/v1",
                suggested_candidate_strategy_id="review.two_independent_reconcile/v1",
            ))
    return observations


def _find_local_first_success(trajectories: List[ExecutionTrajectory]) -> List[TrajectoryObservation]:
    """Detects repeated low-risk success with the local model."""
    candidates: List[str] = []
    for t in trajectories:
        if (
            t.selected_agent == "local_ollama"
            and t.outcome == "success"
            and t.task_class in ("docs", "test_improvement", "other")
        ):
            candidates.append(t.trajectory_id)

    if len(candidates) >= MIN_PATTERN_OCCURRENCES:
        fingerprint = _fingerprint("local_first_success", ["low_risk"])
        return [TrajectoryObservation(
            observation_id=f"OBS-LOCAL-FIRST-{fingerprint}",
            fingerprint=fingerprint,
            category="routing_opportunity",
            title="Repeated low-risk success with local model",
            description=(
                f"{len(candidates)} low-risk trajectories succeeded with local_ollama. "
                "Local-first routing may be worth testing."
            ),
            evidence_refs=candidates,
            occurrence_count=len(candidates),
            task_classes=["docs", "test_improvement", "other"],
            suggested_experiment_type="ROUTING",
            suggested_baseline_strategy_id="routing.frontier_first/v1",
            suggested_candidate_strategy_id="routing.local_first_low_risk/v1",
        )]
    return []


def _find_repeated_routing_problem(trajectories: List[ExecutionTrajectory]) -> List[TrajectoryObservation]:
    """Detects repeated provider exhaustion or failure for a task class/provider pair."""
    counts: Dict[Tuple[str, str], int] = {}
    refs: Dict[Tuple[str, str], List[str]] = {}
    for t in trajectories:
        if t.outcome in ("provider_exhausted", "provider_unavailable"):
            key = (t.task_class or "unknown", t.selected_provider or "unknown")
            counts[key] = counts.get(key, 0) + 1
            refs.setdefault(key, []).append(t.trajectory_id)

    observations: List[TrajectoryObservation] = []
    for (task_class, provider), count in counts.items():
        if count >= MIN_PATTERN_OCCURRENCES:
            fingerprint = _fingerprint("routing_problem", [task_class, provider])
            observations.append(TrajectoryObservation(
                observation_id=f"OBS-ROUTING-{fingerprint}",
                fingerprint=fingerprint,
                category="routing_weakness",
                title=f"Repeated provider problem for {task_class} via {provider}",
                description=(
                    f"{count} trajectories for task_class={task_class} failed due to provider "
                    f"{provider} exhaustion or unavailability. Routing diversity may help."
                ),
                evidence_refs=refs[(task_class, provider)],
                occurrence_count=count,
                task_classes=[task_class] if task_class else [],
                suggested_experiment_type="ROUTING",
                suggested_baseline_strategy_id="routing.frontier_first/v1",
                suggested_candidate_strategy_id="routing.multi_provider_plan_implement_review/v1",
            ))
    return observations


PATTERN_MINERS = [
    _find_architecture_omissions,
    _find_reviewer_dismissal,
    _find_local_first_success,
    _find_repeated_routing_problem,
]


class ObservationStore(DurableObjectStore):
    """Durable store for TrajectoryObservation records with deduplication by fingerprint."""

    _filename_suffix = ".json"

    def __init__(self, base_dir: Union[str, Path]):
        super().__init__(
            base_dir,
            factory=TrajectoryObservation.from_dict,
            dedup_field=None,
        )

    def save(self, observation: TrajectoryObservation) -> Path:
        target = self._path(observation.observation_id)
        atomic_write_json(target, observation.to_dict())
        return target

    def load(self, observation_id: str) -> TrajectoryObservation:
        return self._factory(safe_load_json(self._path(observation_id)))

    def find_by_fingerprint(self, fingerprint: str) -> Optional[TrajectoryObservation]:
        for obs in self.list_all():
            if obs.fingerprint == fingerprint:
                return obs
        return None


def discover_observations(
    trajectories: List[ExecutionTrajectory],
    store: Optional[ObservationStore] = None,
    reopen_new_evidence: bool = True,
) -> List[TrajectoryObservation]:
    """
    Mines trajectories for evidence-backed observations, deduplicates by fingerprint,
    and optionally reopens deferred/falsified observations when new evidence arrives.
    """
    candidates: List[TrajectoryObservation] = []
    for miner in PATTERN_MINERS:
        candidates.extend(miner(trajectories))

    result: List[TrajectoryObservation] = []
    if store is None:
        return candidates

    for obs in candidates:
        existing = store.find_by_fingerprint(obs.fingerprint)
        if existing is None:
            store.save(obs)
            result.append(obs)
        elif existing.status in (ObservationStatus.DEFERRED, ObservationStatus.FALSIFIED, ObservationStatus.INCONCLUSIVE):
            new_refs = [r for r in obs.evidence_refs if r not in existing.evidence_refs]
            if reopen_new_evidence and new_refs:
                existing.evidence_refs = list(set(existing.evidence_refs) | set(obs.evidence_refs))
                existing.occurrence_count = max(existing.occurrence_count, obs.occurrence_count)
                existing.reopen(new_refs)
                store.save(existing)
                result.append(existing)
        # If existing is OPEN or EXPERIMENTING, do not duplicate; the observation is
        # already being tracked through the normal backlog flow.
    return result


def experiment_exists_for_observation(
    observation: TrajectoryObservation,
    experiment_store: ReasoningExperimentStore,
) -> bool:
    """Checks whether an experiment already covers the observation's suggested comparison."""
    for exp in experiment_store.list_all():
        if (
            exp.experiment_type == observation.suggested_experiment_type
            and exp.baseline_strategy
            and exp.candidate_strategy
            and exp.baseline_strategy.strategy_id == observation.suggested_baseline_strategy_id
            and exp.candidate_strategy.strategy_id == observation.suggested_candidate_strategy_id
        ):
            return True
    return False

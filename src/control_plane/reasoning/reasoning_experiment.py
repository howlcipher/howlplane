#!/usr/bin/env python3
"""
reasoning_experiment.py

Durable, schema-versioned reasoning experiment record.

A ReasoningExperiment compares a baseline strategy against a candidate strategy
using a deterministic mechanism. Prediction fields (expected outcome,
falsification criteria, baseline/candidate strategies, metrics) are made
immutable once execution starts by capturing a prediction digest before any
results are recorded.
"""

import hashlib, json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.control_plane.atomic_io import atomic_write_json, safe_load_json
from src.control_plane.reasoning._json_store import DurableObjectStore
from src.control_plane.reasoning.strategy_registry import StrategyDefinition
from src.control_plane.task_spec import DataClassSerializationMixin

REASONING_EXPERIMENT_SCHEMA_VERSION = "howlplane.reasoning_experiment/v1"

VALID_EXPERIMENT_OUTCOMES = {
    "SUPPORTED",
    "WEAKLY_SUPPORTED",
    "FALSIFIED",
    "INCONCLUSIVE",
    "NOT_YET_MEASURABLE",
}

VALID_EXPERIMENT_TYPES = {
    "ROUTING",
    "PROVIDER_COMPOSITION",
    "CONTEXT",
    "RETRIEVAL",
    "REVIEW_TOPOLOGY",
    "PROMPT_STRATEGY",
    "TASK_DECOMPOSITION",
    "TOOL_STRATEGY",
    "VERIFICATION",
}


class ExperimentIntegrityError(ValueError):
    """Raised when an experiment's immutable prediction fields would be mutated."""
    pass


@dataclass
class StrategySnapshot(DataClassSerializationMixin):
    """Immutable snapshot of a strategy as assigned to an experiment arm."""

    strategy_id: str
    version: str
    strategy_type: str
    config_digest: str
    full_config: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_definition(cls, definition: StrategyDefinition) -> "StrategySnapshot":
        return cls(
            strategy_id=definition.strategy_id,
            version=definition.version,
            strategy_type=definition.strategy_type,
            config_digest=definition.digest,
            full_config=definition.immutable_config,
        )


@dataclass
class ReasoningExperiment(DataClassSerializationMixin):
    """
    Durable record of a bounded reasoning-strategy comparison.

    Prediction fields are protected by `prediction_digest`. Once execution has
    started (`started_at` is set), attempts to modify expected_outcome,
    falsification_criteria, baseline/candidate snapshots, or metrics raise
    ExperimentIntegrityError.
    """

    experiment_id: str
    schema_version: str = REASONING_EXPERIMENT_SCHEMA_VERSION
    campaign_id: Optional[str] = None
    opportunity_id: Optional[str] = None
    task_class: Optional[str] = None
    experiment_type: str = "CONTEXT"
    baseline_strategy: Optional[StrategySnapshot] = None
    candidate_strategy: Optional[StrategySnapshot] = None
    baseline_provider: Optional[str] = None
    candidate_provider: Optional[str] = None
    baseline_model: Optional[str] = None
    candidate_model: Optional[str] = None
    context_strategy_id: Optional[str] = None
    retrieval_strategy_id: Optional[str] = None
    tool_strategy_id: Optional[str] = None
    review_strategy_id: Optional[str] = None
    decomposition_strategy_id: Optional[str] = None
    planning_strategy_id: Optional[str] = None
    verification_strategy_id: Optional[str] = None
    expected_outcome: str = ""
    falsification_criteria: List[str] = field(default_factory=list)
    metrics: List[str] = field(default_factory=list)
    baseline_results: List[Dict[str, Any]] = field(default_factory=list)
    candidate_results: List[Dict[str, Any]] = field(default_factory=list)
    success_rate: Optional[float] = None
    verification_pass_rate: Optional[float] = None
    review_escape_rate: Optional[float] = None
    repair_cycles: Optional[int] = None
    cost_if_available: Optional[float] = None
    latency_if_available: Optional[float] = None
    provider_failures: int = 0
    result: str = "NOT_YET_MEASURABLE"
    confidence: Optional[str] = None
    evidence_refs: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    prediction_digest: str = ""
    content_digest: str = ""

    def __post_init__(self):
        if self.experiment_type not in VALID_EXPERIMENT_TYPES:
            raise ValueError(
                f"experiment_type '{self.experiment_type}' invalid. Allowed: {sorted(VALID_EXPERIMENT_TYPES)}"
            )
        if self.result not in VALID_EXPERIMENT_OUTCOMES:
            raise ValueError(
                f"result '{self.result}' invalid. Allowed: {sorted(VALID_EXPERIMENT_OUTCOMES)}"
            )
        if not self.prediction_digest:
            self.prediction_digest = self._compute_prediction_digest()
        if not self.content_digest:
            self.content_digest = self._compute_full_digest()

    @property
    def _prediction_fields(self) -> Dict[str, Any]:
        """Fields frozen at experiment start."""
        return {
            "experiment_type": self.experiment_type,
            "baseline_strategy": self.baseline_strategy.to_dict() if self.baseline_strategy else None,
            "candidate_strategy": self.candidate_strategy.to_dict() if self.candidate_strategy else None,
            "baseline_provider": self.baseline_provider,
            "candidate_provider": self.candidate_provider,
            "baseline_model": self.baseline_model,
            "candidate_model": self.candidate_model,
            "context_strategy_id": self.context_strategy_id,
            "retrieval_strategy_id": self.retrieval_strategy_id,
            "tool_strategy_id": self.tool_strategy_id,
            "review_strategy_id": self.review_strategy_id,
            "decomposition_strategy_id": self.decomposition_strategy_id,
            "planning_strategy_id": self.planning_strategy_id,
            "verification_strategy_id": self.verification_strategy_id,
            "expected_outcome": self.expected_outcome,
            "falsification_criteria": self.falsification_criteria,
            "metrics": self.metrics,
        }

    def _compute_prediction_digest(self) -> str:
        canonical = json.dumps(self._prediction_fields, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _compute_full_digest(self) -> str:
        d = self.to_dict()
        d.pop("content_digest", None)
        d.pop("completed_at", None)
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _ensure_prediction_immutable(self) -> None:
        if self.started_at:
            raise ExperimentIntegrityError(
                "Cannot modify prediction fields after experiment execution has started."
            )

    def mark_started(self) -> None:
        """Freezes prediction fields and records start time."""
        if self.started_at:
            return
        self.prediction_digest = self._compute_prediction_digest()
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.content_digest = self._compute_full_digest()

    def set_expected_outcome(self, expected_outcome: str) -> None:
        self._ensure_prediction_immutable()
        self.expected_outcome = expected_outcome
        self.prediction_digest = self._compute_prediction_digest()
        self.content_digest = self._compute_full_digest()

    def set_falsification_criteria(self, criteria: List[str]) -> None:
        self._ensure_prediction_immutable()
        self.falsification_criteria = list(criteria)
        self.prediction_digest = self._compute_prediction_digest()
        self.content_digest = self._compute_full_digest()

    def set_baseline_strategy(self, strategy: StrategySnapshot) -> None:
        self._ensure_prediction_immutable()
        self.baseline_strategy = strategy
        self.prediction_digest = self._compute_prediction_digest()
        self.content_digest = self._compute_full_digest()

    def set_candidate_strategy(self, strategy: StrategySnapshot) -> None:
        self._ensure_prediction_immutable()
        self.candidate_strategy = strategy
        self.prediction_digest = self._compute_prediction_digest()
        self.content_digest = self._compute_full_digest()

    def set_metrics(self, metrics: List[str]) -> None:
        self._ensure_prediction_immutable()
        self.metrics = list(metrics)
        self.prediction_digest = self._compute_prediction_digest()
        self.content_digest = self._compute_full_digest()

    def verify_prediction_digest(self) -> bool:
        """Verifies that prediction fields have not changed since execution started."""
        return self._compute_prediction_digest() == self.prediction_digest

    def record_results(
        self,
        baseline_results: List[Dict[str, Any]],
        candidate_results: List[Dict[str, Any]],
    ) -> None:
        """Records result payloads after execution. Prediction fields must be unchanged."""
        if not self.started_at:
            self.mark_started()
        if not self.verify_prediction_digest():
            raise ExperimentIntegrityError(
                "Prediction digest mismatch: prediction fields changed after execution started."
            )
        self.baseline_results = list(baseline_results)
        self.candidate_results = list(candidate_results)
        refs = set(self.evidence_refs)
        for r in baseline_results + candidate_results:
            tid = r.get("trajectory_id")
            if tid:
                refs.add(tid)
        self.evidence_refs = list(refs)
        self.content_digest = self._compute_full_digest()

    def finalize(self, result: str, confidence: Optional[str] = None) -> None:
        """Records the deterministic experiment outcome."""
        if result not in VALID_EXPERIMENT_OUTCOMES:
            raise ValueError(f"Invalid experiment outcome: {result}")
        self.result = result
        self.confidence = confidence
        self.completed_at = datetime.now(timezone.utc).isoformat()
        self.content_digest = self._compute_full_digest()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReasoningExperiment":
        d = dict(data)
        d.pop("content_digest", None)
        d.pop("prediction_digest", None)
        d.pop("schema_version", None)
        if d.get("baseline_strategy"):
            d["baseline_strategy"] = StrategySnapshot.from_dict(d["baseline_strategy"])
        if d.get("candidate_strategy"):
            d["candidate_strategy"] = StrategySnapshot.from_dict(d["candidate_strategy"])
        return cls(**d)


class ReasoningExperimentStore(DurableObjectStore):
    """Atomic, idempotent durable store for ReasoningExperiment records."""

    _filename_suffix = ".json"

    def __init__(self, base_dir: Union[str, Path]):
        super().__init__(
            base_dir,
            factory=ReasoningExperiment.from_dict,
            dedup_field="content_digest",
        )

    def save(self, experiment: ReasoningExperiment) -> Path:
        """Atomically persists an experiment; idempotent on repeated calls."""
        return super().save(experiment.experiment_id, experiment.to_dict())

    def load(self, experiment_id: str) -> ReasoningExperiment:
        return self._factory(safe_load_json(self._path(experiment_id)))

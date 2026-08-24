#!/usr/bin/env python3
"""Shared pre-registered, resumable reasoning experiment lifecycle."""

from dataclasses import dataclass
from typing import Callable, Optional, Union

from src.control_plane.reasoning.artifact_safety import canonical_digest
from src.control_plane.reasoning.execution_trajectory import (
    ExecutionTrajectory,
    TrajectoryStore,
    summarize_for_experiment,
)
from src.control_plane.reasoning.experiment_evaluator import evaluate_experiment
from src.control_plane.reasoning.reasoning_experiment import (
    ExperimentIntegrityError,
    ReasoningExperiment,
    ReasoningExperimentStore,
    StrategySnapshot,
)

_AUTHORITY_KEYS = {
    "authority_envelope",
    "authority_profile",
    "branch_protection",
    "credentials",
    "merge_budget",
    "production_access",
    "publishing_authority",
    "repository_scope",
    "spend",
    "ttl",
}


@dataclass(frozen=True)
class ExperimentArmContext:
    """Authority-free inputs exposed to the shared arm executor."""

    experiment_id: str
    experiment_type: str
    arm: str
    sample_id: str
    trajectory_id: str
    task_class: Optional[str]
    strategy: StrategySnapshot
    provider: Optional[str]
    model: Optional[str]


ArmExecutor = Callable[[ExperimentArmContext], ExecutionTrajectory]
CheckpointHook = Callable[[str, ReasoningExperiment], None]


def _contains_authority_key(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _AUTHORITY_KEYS or _contains_authority_key(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_authority_key(item) for item in value)
    return False


class ReasoningExperimentCoordinator:
    """Runs every experiment type through one durable, idempotent mechanism."""

    def __init__(
        self,
        experiment_store: ReasoningExperimentStore,
        trajectory_store: TrajectoryStore,
        checkpoint_hook: Optional[CheckpointHook] = None,
    ):
        self.experiment_store = experiment_store
        self.trajectory_store = trajectory_store
        self.checkpoint_hook = checkpoint_hook

    def _checkpoint(self, stage: str, experiment: ReasoningExperiment) -> None:
        if self.checkpoint_hook:
            self.checkpoint_hook(stage, experiment)

    def preregister(self, experiment: ReasoningExperiment) -> ReasoningExperiment:
        """Persist the immutable prediction before any experiment execution."""
        if self.experiment_store.exists(experiment.experiment_id):
            stored = self.experiment_store.load(experiment.experiment_id)
            if stored.prediction_digest != experiment.prediction_digest:
                raise ExperimentIntegrityError(
                    "Experiment ID already has a different pre-registration."
                )
            return stored
        if experiment.started_at or experiment.completed_at:
            raise ExperimentIntegrityError("Pre-registration must precede execution.")
        if not experiment.baseline_strategy or not experiment.candidate_strategy:
            raise ExperimentIntegrityError("Baseline and candidate strategies are required.")
        if not experiment.expected_outcome.strip():
            raise ExperimentIntegrityError("Expected outcome must be pre-registered.")
        if not experiment.falsification_criteria:
            raise ExperimentIntegrityError("Falsification criteria must be pre-registered.")
        if not experiment.metrics:
            raise ExperimentIntegrityError("Selected metrics must be pre-registered.")
        for snapshot in (experiment.baseline_strategy, experiment.candidate_strategy):
            if _contains_authority_key(snapshot.full_config):
                raise ExperimentIntegrityError(
                    "Reasoning strategies cannot contain authority controls."
                )
        self.experiment_store.save(experiment)
        self._checkpoint("definition_persisted", experiment)
        return experiment

    @staticmethod
    def trajectory_id(experiment_id: str, arm: str, sample_id: str) -> str:
        """Derive a stable event identity used for crash-safe deduplication."""
        digest = canonical_digest({
            "experiment_id": experiment_id,
            "arm": arm,
            "sample_id": sample_id,
        })
        return f"traj-{digest[:24]}"

    def _context(
        self,
        experiment: ReasoningExperiment,
        arm: str,
        sample_id: str,
    ) -> ExperimentArmContext:
        if arm == "baseline":
            strategy = experiment.baseline_strategy
            provider = experiment.baseline_provider
            model = experiment.baseline_model
        else:
            strategy = experiment.candidate_strategy
            provider = experiment.candidate_provider
            model = experiment.candidate_model
        if strategy is None:
            raise ExperimentIntegrityError(f"Experiment arm '{arm}' has no strategy.")
        return ExperimentArmContext(
            experiment_id=experiment.experiment_id,
            experiment_type=experiment.experiment_type,
            arm=arm,
            sample_id=sample_id,
            trajectory_id=self.trajectory_id(experiment.experiment_id, arm, sample_id),
            task_class=experiment.task_class,
            strategy=strategy,
            provider=provider,
            model=model,
        )

    def run_arm(
        self,
        experiment_id: str,
        arm: str,
        sample_id: str,
        execute: ArmExecutor,
    ) -> ExecutionTrajectory:
        """Execute or recover one arm without duplicating trajectory/accounting."""
        if arm not in ("baseline", "candidate"):
            raise ValueError("Experiment arm must be 'baseline' or 'candidate'.")
        experiment = self.experiment_store.load(experiment_id)
        if arm == "candidate" and not experiment.baseline_results:
            raise ExperimentIntegrityError("Baseline evidence must precede candidate execution.")
        context = self._context(experiment, arm, sample_id)
        if not experiment.started_at:
            experiment.mark_started()
            self.experiment_store.save(experiment)

        if self.trajectory_store.exists(context.trajectory_id):
            trajectory = self.trajectory_store.load(context.trajectory_id)
        else:
            trajectory = execute(context)
            if not isinstance(trajectory, ExecutionTrajectory):
                raise TypeError("Experiment arm executor must return ExecutionTrajectory.")
            if trajectory.trajectory_id != context.trajectory_id:
                raise ExperimentIntegrityError(
                    "Arm executor returned a non-deterministic trajectory ID."
                )
            trajectory.experiment_id = experiment.experiment_id
            if not trajectory.final_status or not trajectory.outcome:
                raise ExperimentIntegrityError("Arm trajectories require a final observable outcome.")
            trajectory.content_digest = trajectory.compute_content_digest()
            self.trajectory_store.save(trajectory)

        experiment = self.experiment_store.load(experiment_id)
        experiment.record_arm_result(arm, summarize_for_experiment(trajectory))
        self.experiment_store.save(experiment)
        self._checkpoint(f"{arm}_persisted", experiment)
        return trajectory

    def finalize(self, experiment_id: str) -> ReasoningExperiment:
        """Deterministically evaluate both durable arms and persist the verdict."""
        experiment = self.experiment_store.load(experiment_id)
        if experiment.completed_at:
            return experiment
        if not experiment.baseline_results or not experiment.candidate_results:
            raise ExperimentIntegrityError("Both experiment arms must be durable before evaluation.")
        outcome, details = evaluate_experiment(experiment)
        confidence = (
            f"baseline_n={details.get('baseline_n')}, "
            f"candidate_n={details.get('candidate_n')}, "
            f"primary_metric={details.get('primary_metric')}"
        )
        experiment.finalize(outcome, confidence, details)
        self.experiment_store.save(experiment)
        self._checkpoint("evaluation_persisted", experiment)
        return experiment

    def run(
        self,
        experiment_id: str,
        execute: ArmExecutor,
        sample_id: str = "1",
    ) -> ReasoningExperiment:
        """Resume the common baseline, candidate, deterministic-evaluation flow."""
        experiment = self.experiment_store.load(experiment_id)
        if experiment.completed_at:
            return experiment
        self.run_arm(experiment_id, "baseline", sample_id, execute)
        self.run_arm(experiment_id, "candidate", sample_id, execute)
        return self.finalize(experiment_id)

    def next_action(self, experiment_id: str) -> str:
        """Describe the exact safe resume phase using durable artifacts only."""
        experiment = self.experiment_store.load(experiment_id)
        if experiment.completed_at:
            return "complete"
        if not experiment.baseline_results:
            return "run_baseline"
        if not experiment.candidate_results:
            return "run_candidate"
        return "evaluate"

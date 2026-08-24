#!/usr/bin/env python3
"""
Reasoning strategy dogfooding sub-package.

Provides durable, schema-versioned artifacts for observing and experimenting
with system-level reasoning choices without modifying proprietary model weights.
"""

from src.control_plane.reasoning.execution_trajectory import (
    ExecutionTrajectory,
    ExecutionTrajectoryBuilder,
    TrajectoryStore,
    summarize_for_experiment,
)
from src.control_plane.reasoning.reasoning_experiment import (
    ReasoningExperiment,
    ReasoningExperimentStore,
    StrategySnapshot,
)
from src.control_plane.reasoning.strategy_registry import (
    StrategyDefinition,
    StrategyRegistry,
    StrategyIdentityError,
)
from src.control_plane.reasoning.experiment_evaluator import (
    evaluate_experiment,
    finalize_experiment_outcome,
    EvaluationError,
)
from src.control_plane.reasoning.trajectory_discovery import (
    TrajectoryObservation,
    ObservationStore,
    discover_observations,
    ObservationStatus,
    experiment_exists_for_observation,
    challenge_observation,
)
from src.control_plane.reasoning.experiment_coordinator import (
    ExperimentArmContext,
    ReasoningExperimentCoordinator,
)

__all__ = [
    "ExecutionTrajectory",
    "TrajectoryStore",
    "summarize_for_experiment",
    "ReasoningExperiment",
    "ReasoningExperimentStore",
    "StrategySnapshot",
    "StrategyDefinition",
    "StrategyRegistry",
    "StrategyIdentityError",
    "evaluate_experiment",
    "finalize_experiment_outcome",
    "EvaluationError",
    "TrajectoryObservation",
    "ObservationStore",
    "discover_observations",
    "ObservationStatus",
    "experiment_exists_for_observation",
    "challenge_observation",
    "ExperimentArmContext",
    "ReasoningExperimentCoordinator",
]

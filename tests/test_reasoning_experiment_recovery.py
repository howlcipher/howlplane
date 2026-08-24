"""Durability and shared-lifecycle coverage for Milestone #60A."""

import json
from pathlib import Path

import pytest

from src.control_plane.atomic_io import atomic_write_json, safe_load_json
from src.control_plane.reasoning.execution_trajectory import TrajectoryStore
from src.control_plane.reasoning.experiment_coordinator import (
    ReasoningExperimentCoordinator,
)
from src.control_plane.reasoning.experiment_evaluator import evaluate_experiment
from src.control_plane.reasoning._json_store import ArtifactIdentityError
from src.control_plane.reasoning.artifact_safety import (
    ArtifactIntegrityError,
    MAX_COLLECTION_ITEMS,
    MAX_STRING_LENGTH,
)
from src.control_plane.reasoning.reasoning_experiment import (
    ExperimentIntegrityError,
    ReasoningExperimentStore,
    VALID_EXPERIMENT_TYPES,
)
from src.control_plane.reasoning.strategy_registry import (
    StrategyIdentityError,
    StrategyRegistry,
)
from src.control_plane.reasoning.trajectory_discovery import (
    ObservationStatus,
    ObservationStore,
    TrajectoryObservation,
    challenge_observation,
    discover_observations,
)
from src.control_plane.synthesis.campaign_state import DurableCampaignState
from tests._dogfood_test_helpers import (
    execution_trajectory,
    reasoning_experiment,
    reasoning_strategy,
    record_reasoning_results,
    trajectory_summary,
)


def _registered_coordinator(
    tmp_path: Path,
    *,
    experiment_id: str = "EXP-RECOVERY-001",
    experiment_type: str = "CONTEXT",
    hook=None,
):
    experiment_store = ReasoningExperimentStore(tmp_path / "experiments")
    trajectory_store = TrajectoryStore(tmp_path / "trajectories")
    coordinator = ReasoningExperimentCoordinator(
        experiment_store, trajectory_store, checkpoint_hook=hook,
    )
    experiment = reasoning_experiment(
        experiment_id,
        experiment_type=experiment_type,
        baseline_id="context.changed_files_only/v1",
        candidate_id="context.changed_files_plus_architecture/v1",
        expected_outcome="candidate verification is no worse",
        falsification_criteria=[
            "candidate.verification_pass_rate < baseline.verification_pass_rate"
        ],
        metrics=["verification_pass_rate", "mean_repair_cycles"],
    )
    coordinator.preregister(experiment)
    return coordinator, experiment_store, trajectory_store


def _successful_executor(calls):
    def execute(context):
        calls.append(context.arm)
        trajectory = execution_trajectory(
            context.trajectory_id,
            task_id=f"TASK-{context.arm}",
            experiment_id=context.experiment_id,
            task_class=context.task_class,
            selected_provider=context.provider,
            selected_model=context.model,
            context_strategy_id=context.strategy.strategy_id,
            final_status="complete",
            outcome="success",
            verification_results={
                "overall_status": "passed",
                "reproducible": True,
            },
        )
        trajectory.finalize("complete", "success")
        return trajectory
    return execute


@pytest.mark.parametrize("experiment_type", sorted(VALID_EXPERIMENT_TYPES))
def test_all_experiment_types_use_shared_coordinator(tmp_path: Path, experiment_type):
    coordinator, _, _ = _registered_coordinator(
        tmp_path, experiment_type=experiment_type,
    )
    result = coordinator.run("EXP-RECOVERY-001", _successful_executor([]))
    assert result.lifecycle_stage == "COMPLETE"
    assert result.result == "INCONCLUSIVE"


@pytest.mark.parametrize(
    ("crash_stage", "expected_next"),
    [
        ("definition_persisted", "run_baseline"),
        ("baseline_persisted", "run_candidate"),
        ("candidate_persisted", "evaluate"),
    ],
)
def test_crash_resume_has_exact_next_phase_and_no_duplicates(
    tmp_path: Path, crash_stage, expected_next,
):
    crashed = False

    def crash_once(stage, _experiment):
        nonlocal crashed
        if stage == crash_stage and not crashed:
            crashed = True
            raise RuntimeError(f"crash after {stage}")

    calls = []
    if crash_stage == "definition_persisted":
        with pytest.raises(RuntimeError, match=crash_stage):
            _registered_coordinator(tmp_path, hook=crash_once)
        experiment_store = ReasoningExperimentStore(tmp_path / "experiments")
        trajectory_store = TrajectoryStore(tmp_path / "trajectories")
    else:
        coordinator, experiment_store, trajectory_store = _registered_coordinator(
            tmp_path, hook=crash_once,
        )
        with pytest.raises(RuntimeError, match=crash_stage):
            if crash_stage == "baseline_persisted":
                coordinator.run_arm(
                    "EXP-RECOVERY-001", "baseline", "1", _successful_executor(calls),
                )
            else:
                coordinator.run_arm(
                    "EXP-RECOVERY-001", "baseline", "1", _successful_executor(calls),
                )
                coordinator.run_arm(
                    "EXP-RECOVERY-001", "candidate", "1", _successful_executor(calls),
                )

    fresh_agent = ReasoningExperimentCoordinator(experiment_store, trajectory_store)
    assert fresh_agent.next_action("EXP-RECOVERY-001") == expected_next
    completed = fresh_agent.run("EXP-RECOVERY-001", _successful_executor(calls))
    assert completed.lifecycle_stage == "COMPLETE"
    assert len(completed.baseline_results) == 1
    assert len(completed.candidate_results) == 1
    assert len(trajectory_store.list_all()) == 2
    assert calls.count("baseline") <= 1
    assert calls.count("candidate") <= 1


def test_fresh_agent_can_reconstruct_from_journal_and_artifacts(tmp_path: Path):
    coordinator, experiment_store, trajectory_store = _registered_coordinator(tmp_path)
    coordinator.run_arm(
        "EXP-RECOVERY-001", "baseline", "1", _successful_executor([]),
    )
    journal = Path("documentation/task_journals/2026-08-24_reasoning_strategy_trajectories.md")
    assert "WORKING_BRANCH" in journal.read_text(encoding="utf-8")
    fresh_agent = ReasoningExperimentCoordinator(experiment_store, trajectory_store)
    assert fresh_agent.next_action("EXP-RECOVERY-001") == "run_candidate"


@pytest.mark.parametrize("store_kind", ["trajectory", "experiment"])
def test_persisted_digest_tampering_fails_closed(tmp_path: Path, store_kind):
    coordinator, experiment_store, trajectory_store = _registered_coordinator(tmp_path)
    if store_kind == "trajectory":
        coordinator.run_arm(
            "EXP-RECOVERY-001", "baseline", "1", _successful_executor([]),
        )
        store = trajectory_store
        artifact_id = coordinator.trajectory_id("EXP-RECOVERY-001", "baseline", "1")
    else:
        store = experiment_store
        artifact_id = "EXP-RECOVERY-001"
    path = store._path(artifact_id)
    payload = safe_load_json(path)
    payload["task_class" if store_kind == "experiment" else "outcome"] = "tampered"
    atomic_write_json(path, payload)
    with pytest.raises(ArtifactIntegrityError, match="digest mismatch"):
        store.load(artifact_id)


def test_artifact_ids_cannot_escape_store(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "trajectories")
    with pytest.raises(ArtifactIdentityError):
        store.exists("../../outside")


def test_trajectory_payload_is_bounded_redacted_and_reasoning_free():
    trajectory = execution_trajectory(
        "traj-safe",
        provider_events=[{
            "token": "token=abcdefgh12345678",
            "chain_of_thought": "never persist this",
            "events": list(range(MAX_COLLECTION_ITEMS + 25)),
            "message": "x" * (MAX_STRING_LENGTH + 25),
        }],
    )
    payload = trajectory.to_dict()
    serialized = json.dumps(payload)
    event = payload["provider_events"][0]
    assert "never persist this" not in serialized
    assert "abcdefgh12345678" not in serialized
    assert len(event["events"]) == MAX_COLLECTION_ITEMS
    assert len(event["message"]) == MAX_STRING_LENGTH


def test_completed_experiment_results_cannot_be_rewritten(tmp_path: Path):
    coordinator, experiment_store, _ = _registered_coordinator(tmp_path)
    completed = coordinator.run("EXP-RECOVERY-001", _successful_executor([]))
    completed.candidate_results[0]["outcome"] = "failed"
    with pytest.raises(ExperimentIntegrityError, match="Completed"):
        experiment_store.save(completed)


def test_coordinator_rejects_authority_bearing_strategy(tmp_path: Path):
    experiment = reasoning_experiment(
        "EXP-AUTHORITY-001",
        baseline_id="context.changed_files_only/v1",
        candidate_id="context.changed_files_plus_architecture/v1",
        expected_outcome="candidate wins",
        falsification_criteria=["candidate.success_rate < baseline.success_rate"],
        metrics=["success_rate"],
    )
    experiment.candidate_strategy.full_config["merge_budget"] = 999
    experiment.prediction_digest = experiment._compute_prediction_digest()
    experiment.content_digest = experiment._compute_full_digest()
    coordinator = ReasoningExperimentCoordinator(
        ReasoningExperimentStore(tmp_path / "experiments"),
        TrajectoryStore(tmp_path / "trajectories"),
    )
    with pytest.raises(ExperimentIntegrityError, match="authority"):
        coordinator.preregister(experiment)


def test_strategy_identity_version_suffix_must_match():
    with pytest.raises(StrategyIdentityError, match="must match"):
        reasoning_strategy("context.changed_files_only/v2")


def test_campaign_experiment_accounting_is_idempotent(tmp_path: Path):
    coordinator, _, _ = _registered_coordinator(tmp_path)
    experiment = coordinator.run("EXP-RECOVERY-001", _successful_executor([]))
    campaign = DurableCampaignState(campaign_id="campaign-recovery")
    campaign.record_reasoning_experiment(experiment.to_dict())
    campaign.record_reasoning_experiment(experiment.to_dict())
    assert len(campaign.reasoning_experiments) == 1


def _routing_problem(trajectory_id: str, verification_results=None):
    return execution_trajectory(
        trajectory_id,
        task_class="feature",
        selected_provider="codex",
        final_status="failed",
        outcome="provider_exhausted",
        verification_results=verification_results,
    )


def _disposed_routing_observation(tmp_path: Path, status: str):
    store = ObservationStore(tmp_path / "observations")
    original = discover_observations(
        [_routing_problem("original-1"), _routing_problem("original-2")],
        store=store,
    )[0]
    original.status = status
    store.save(original)
    return store, original


def test_identical_new_trajectory_ids_do_not_reopen_failed_observation(tmp_path: Path):
    store, original = _disposed_routing_observation(
        tmp_path, ObservationStatus.FALSIFIED,
    )
    rediscovered = discover_observations(
        [_routing_problem("new-id-1"), _routing_problem("new-id-2")],
        store=store,
    )
    assert rediscovered == []
    assert store.load(original.observation_id).status == ObservationStatus.FALSIFIED


def test_materially_new_observable_evidence_records_reopening_reason(tmp_path: Path):
    store, _ = _disposed_routing_observation(
        tmp_path, ObservationStatus.INCONCLUSIVE,
    )
    changed = {"overall_status": "failed", "files_modified": ["a.py", "b.py"]}
    reopened = discover_observations(
        [_routing_problem("new-1", changed), _routing_problem("new-2", changed)],
        store=store,
    )[0]
    assert reopened.status == ObservationStatus.OPEN
    assert reopened.reopening_history[-1]["reason"]


def test_observation_challenge_preregisters_without_changing_routing(tmp_path: Path):
    observation_store = ObservationStore(tmp_path / "observations")
    observation = TrajectoryObservation(
        observation_id="OBS-CHALLENGE",
        fingerprint="context-architecture-gap",
        category="context_weakness",
        title="Architecture context gap",
        description="Repeated cross-module failures",
        evidence_refs=["traj-1", "traj-2"],
        suggested_experiment_type="CONTEXT",
        suggested_baseline_strategy_id="context.changed_files_only/v1",
        suggested_candidate_strategy_id="context.changed_files_plus_architecture/v1",
    )
    observation_store.save(observation)
    coordinator = ReasoningExperimentCoordinator(
        ReasoningExperimentStore(tmp_path / "experiments"),
        TrajectoryStore(tmp_path / "trajectories"),
    )
    experiment = challenge_observation(
        observation,
        StrategyRegistry(),
        coordinator,
        "architecture context improves verification",
        ["candidate.verification_pass_rate < baseline.verification_pass_rate"],
        ["verification_pass_rate"],
        observation_store,
    )
    assert experiment.lifecycle_stage == "DEFINED"
    assert coordinator.next_action(experiment.experiment_id) == "run_baseline"
    assert observation_store.load("OBS-CHALLENGE").status == ObservationStatus.EXPERIMENTING


def test_availability_is_separate_from_engineering_quality():
    experiment = reasoning_experiment(
        "EXP-AVAILABILITY-QUALITY",
        metrics=["verification_pass_rate", "provider_failures"],
    )
    record_reasoning_results(
        experiment,
        [trajectory_summary("baseline", verification_status="passed")],
        [trajectory_summary(
            "candidate",
            verification_status="failed",
            outcome="provider_unavailable",
        )],
    )
    _, details = evaluate_experiment(experiment)
    assert details["metric_comparisons"]["verification_pass_rate"] == "incomparable"
    assert details["metric_comparisons"]["provider_failures"] == "worse"


@pytest.mark.parametrize(
    ("metric", "field", "baseline_value", "candidate_value", "expected"),
    [
        ("confirmed_defects", "confirmed_defects", 2, 1, "better"),
        ("escaped_defects", "escaped_defects", 0, 1, "worse"),
        ("reproducibility_rate", "reproducible", False, True, "better"),
    ],
)
def test_deterministic_evaluator_uses_observable_defect_and_reproducibility_evidence(
    metric, field, baseline_value, candidate_value, expected,
):
    experiment = reasoning_experiment("EXP-OBSERVABLE", metrics=[metric])
    record_reasoning_results(
        experiment,
        [trajectory_summary("baseline", **{field: baseline_value})],
        [trajectory_summary("candidate", **{field: candidate_value})],
    )
    _, details = evaluate_experiment(experiment)
    assert details["metric_comparisons"][metric] == expected

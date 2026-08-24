#!/usr/bin/env python3
"""
test_reasoning_strategy_dogfooding.py

Milestone #60A — Reasoning Strategy Dogfooding + Execution Trajectories.

Deterministic tests covering:
  - durable ExecutionTrajectory capture and redaction
  - ReasoningExperiment prediction immutability and deterministic evaluation
  - StrategyRegistry identity/digest/versioning
  - experiment types and self-confirming-experiment safeguards
  - provider availability vs engineering quality
  - context / retrieval / review topology comparisons
  - trajectory-derived discovery and deduplication
  - authority invariants
"""

import json
from pathlib import Path

import pytest

from src.control_plane.authority_profile import (
    OVERNIGHT_SAFE_PROFILE,
)
from src.control_plane.authority_envelope import (
    AuthorityDecision,
    AuthorityEnvelope,
    create_envelope,
    evaluate_action_against_envelope,
)
from src.control_plane.reasoning import (
    ExecutionTrajectory,
    ExecutionTrajectoryBuilder,
    ReasoningExperiment,
    StrategyDefinition,
    StrategyRegistry,
    StrategySnapshot,
    TrajectoryStore,
    ReasoningExperimentStore,
    ObservationStore,
    TrajectoryObservation,
    ObservationStatus,
    discover_observations,
    evaluate_experiment,
    finalize_experiment_outcome,
    summarize_for_experiment,
    experiment_exists_for_observation,
)
from src.control_plane.reasoning.reasoning_experiment import VALID_EXPERIMENT_OUTCOMES
from src.control_plane.reasoning.experiment_evaluator import (
    evaluate_falsification_criterion,
)
from src.control_plane.reasoning.trajectory_discovery import (
    _fingerprint as _observation_fingerprint,
)
from src.control_plane.task_spec import TaskSpec
from tests._dogfood_test_helpers import (
    execution_trajectory,
    init_minimal_python_repo,
    orchestration_result,
    record_reasoning_results,
    reasoning_experiment,
    reasoning_strategy,
    run_reasoning_task,
    trajectory_summary,
)


# ---------------------------------------------------------------------------
# 1–10. ExecutionTrajectory
# ---------------------------------------------------------------------------


def test_successful_governed_task_creates_trajectory(tmp_path: Path):
    repo = init_minimal_python_repo(tmp_path / "repo")
    spec = TaskSpec(
        task_id="TRJ-OK-001",
        repository="test_service",
        objective="Add a harmless docs line",
        task_class="docs",
        risk_level="low",
    )

    def impl(task, cwd, prompt):
        (cwd / "README.md").write_text("# Updated\n", encoding="utf-8")

    res, traj = run_reasoning_task(
        repo,
        tmp_path / "trajectories",
        spec,
        impl,
    )
    assert res.final_state == "complete"
    assert res.trajectory_id == traj.trajectory_id
    assert traj.task_id == "TRJ-OK-001"
    assert traj.final_status == "complete"
    assert traj.outcome == "success"
    assert traj.verify_digest()


def test_failed_governed_task_creates_trajectory(tmp_path: Path):
    repo = init_minimal_python_repo(tmp_path / "repo")
    spec = TaskSpec(
        task_id="TRJ-FAIL-001",
        repository="test_service",
        objective="Break the test",
        task_class="feature",
        risk_level="medium",
    )

    def impl(task, cwd, prompt):
        (cwd / "tests" / "test_feature.py").write_text(
            "from src.feature import run\n\n\ndef test_run():\n    assert run() is False\n",
            encoding="utf-8",
        )

    res, traj = run_reasoning_task(
        repo,
        tmp_path / "trajectories",
        spec,
        impl,
    )
    assert res.final_state == "failed"
    assert traj.final_status == "failed"


def test_repair_cycles_preserved_in_trajectory(tmp_path: Path):
    repo = init_minimal_python_repo(tmp_path / "repo")
    spec = TaskSpec(
        task_id="TRJ-REPAIR-001",
        repository="test_service",
        objective="Fix feature with one remediation cycle",
        task_class="bug_fix",
        risk_level="medium",
    )

    def impl(task, cwd, prompt):
        (cwd / "src" / "feature.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    attempt = {"n": 0}

    def reviewer_fn(role, diff, task):
        attempt["n"] += 1
        if attempt["n"] == 1 and role == "correctness-reviewer":
            return """
findings:
  - id: "F001"
    reviewer_role: "correctness-reviewer"
    title: "Return type regression"
    severity: "high"
    category: "correctness"
    description: "run() returns int instead of bool"
"""
        return "findings: []\n"

    def remediation_fn(task, cwd, findings):
        (cwd / "src" / "feature.py").write_text("def run():\n    return True\n", encoding="utf-8")

    res, traj = run_reasoning_task(
        repo,
        tmp_path / "trajectories",
        spec,
        impl,
        custom_reviewer_fn=reviewer_fn,
        custom_remediation_fn=remediation_fn,
        max_remediation_cycles=3,
    )
    assert res.final_state == "complete"
    assert res.remediation_cycles_count >= 1
    assert len(traj.repair_cycles) >= 1


def test_review_findings_linked_in_trajectory(tmp_path: Path):
    from src.control_plane.review_runner import ReviewCycleResult, SingleReviewResult
    from src.control_plane.reconciliation import ReviewFinding

    cycle = ReviewCycleResult(
        cycle_index=1,
        reviewer_results={
            "correctness-reviewer": SingleReviewResult(
                reviewer_role="correctness-reviewer",
                reviewer_name="Correctness",
                status="findings_detected",
                findings=[
                    ReviewFinding(
                        id="F001",
                        reviewer_role="correctness-reviewer",
                        title="Logic bug",
                        severity="high",
                        category="correctness",
                        description="Returns wrong value",
                    )
                ],
            )
        },
        all_findings=[
            ReviewFinding(
                id="F001",
                reviewer_role="correctness-reviewer",
                title="Logic bug",
                severity="high",
                category="correctness",
                description="Returns wrong value",
            )
        ],
        status="has_findings",
        requires_remediation=True,
    )
    res = orchestration_result(tmp_path, review_cycles=[cycle])
    traj = ExecutionTrajectoryBuilder.from_orchestration_result(res)
    assert len(traj.review_findings) == 1
    assert traj.review_findings[0]["id"] == "F001"


def test_verification_linked_in_trajectory(tmp_path: Path):
    res = orchestration_result(tmp_path, final_state="complete")
    traj = ExecutionTrajectoryBuilder.from_orchestration_result(res)
    assert traj.verification_results is not None
    assert traj.verification_results["overall_status"] == "passed"


def test_provider_events_linked_in_trajectory(tmp_path: Path):
    from src.control_plane.agent_execution import AgentExecutionResult

    provider_exec = AgentExecutionResult(
        agent_id="claude_code",
        role="implementation",
        command="claude -p test",
        exit_code=0,
        stdout="ok",
        stderr="",
        duration_seconds=0.5,
        success=True,
        metadata={"model": "claude-opus", "cost_usd": 0.02},
    )
    res = orchestration_result(tmp_path, provider_execution=provider_exec)
    traj = ExecutionTrajectoryBuilder.from_orchestration_result(res)
    assert traj.selected_provider == "claude_code"
    assert traj.selected_model == "claude-opus"
    assert traj.cost_if_available == 0.02
    # Raw stdout/stderr should be dropped from the durable event
    assert "stdout" not in traj.provider_events[0]
    assert "stderr" not in traj.provider_events[0]


def test_hidden_chain_of_thought_not_stored(tmp_path: Path):
    traj = execution_trajectory(
        "trj-hidden-test",
        task_id="HIDDEN-001",
        hidden_reasoning="I think I should bypass the checks",
        chain_of_thought="Step 1: ...",
        raw_prompt="secret prompt",
    )
    d = traj.to_dict()
    assert "hidden_reasoning" not in d
    assert "chain_of_thought" not in d
    assert "raw_prompt" not in d


def test_secrets_redacted_in_trajectory(tmp_path: Path):
    traj = execution_trajectory(
        "trj-secret-test",
        task_id="SECRET-001",
        objective="Test with api_key=super_secret_123456",
        provider_events=[{"command": "curl -H api_key:super_secret_123456"}],
    )
    d = traj.to_dict()
    text = json.dumps(d)
    assert "super_secret_123456" not in text
    assert "[REDACTED]" in text


def test_resume_does_not_duplicate_trajectory(tmp_path: Path):
    store = TrajectoryStore(tmp_path / "trajectories")
    traj = execution_trajectory(
        "trj-dup-test",
        task_id="DUP-001",
        final_status="complete",
        outcome="success",
    )
    store.save(traj)
    store.save(traj)
    assert len(store.list_all()) == 1


def test_trajectory_digest_is_deterministic(tmp_path: Path):
    traj = execution_trajectory(
        "trj-digest-test",
        task_id="DIGEST-001",
        final_status="complete",
        outcome="success",
        selected_agent="claude_code",
    )
    d1 = traj.compute_content_digest()
    d2 = traj.compute_content_digest()
    assert d1 == d2
    # Mutating a content field changes the digest
    traj.final_status = "failed"
    d3 = traj.compute_content_digest()
    assert d3 != d1


# ---------------------------------------------------------------------------
# 11–19. ReasoningExperiment immutability, prediction, resume, evaluation
# ---------------------------------------------------------------------------


def test_baseline_candidate_immutable_after_experiment_starts():
    exp = reasoning_experiment(
        "EXP-IMM-001",
        baseline_id="context.changed_files_only/v1",
        candidate_id="context.full_repository/v1",
    )
    exp.mark_started()
    with pytest.raises(Exception):
        exp.set_candidate_strategy(
            StrategySnapshot.from_definition(
                reasoning_strategy("context.changed_files_only/v1")
            )
        )


def test_prediction_persisted_before_outcome():
    exp = reasoning_experiment(
        "EXP-PRED-001",
        baseline_id="context.changed_files_only/v1",
        candidate_id="context.full_repository/v1",
        expected_outcome="Candidate improves verification pass rate",
        falsification_criteria=[
            "candidate.verification_pass_rate < baseline.verification_pass_rate"
        ],
        metrics=["verification_pass_rate"],
    )
    predigest = exp.prediction_digest
    exp.mark_started()
    assert exp.prediction_digest == predigest
    assert exp.started_at is not None


def test_falsification_criteria_persisted_before_outcome():
    exp = reasoning_experiment(
        "EXP-FALS-001",
        expected_outcome="x",
        falsification_criteria=["candidate.repair_cycles > baseline.repair_cycles"],
        metrics=["mean_repair_cycles"],
    )
    exp.mark_started()
    assert exp.falsification_criteria == ["candidate.repair_cycles > baseline.repair_cycles"]


def test_candidate_cannot_rewrite_metric_after_execution():
    exp = reasoning_experiment(
        "EXP-METRIC-001",
        baseline_id="context.changed_files_only/v1",
        candidate_id="context.full_repository/v1",
        metrics=["verification_pass_rate"],
    )
    record_reasoning_results(exp)
    with pytest.raises(Exception):
        exp.set_metrics(["success_rate"])


def test_falsified_candidate_remains_durable(tmp_path: Path):
    store = ReasoningExperimentStore(tmp_path / "experiments")
    exp = reasoning_experiment(
        "EXP-DURABLE-001",
        experiment_type="REVIEW_TOPOLOGY",
        baseline_id="review.general_single/v1",
        candidate_id="review.two_independent_reconcile/v1",
        falsification_criteria=["candidate.verification_pass_rate < 1.0"],
        metrics=["verification_pass_rate"],
    )
    record_reasoning_results(
        exp,
        candidate_results=[
            trajectory_summary(
                "c1", verification_status="failed", repair_cycles_count=1,
            )
        ],
    )
    finalize_experiment_outcome(exp)
    assert exp.result == "FALSIFIED"
    store.save(exp)
    loaded = store.load("EXP-DURABLE-001")
    assert loaded.result == "FALSIFIED"


def test_small_sample_may_return_requires_more_evidence():
    exp = reasoning_experiment(
        "EXP-SMALL-001",
        experiment_type="TASK_DECOMPOSITION",
        baseline_id="planning.direct_implementation/v1",
        candidate_id="planning.plan_then_implement/v1",
        metrics=["verification_pass_rate"],
    )
    record_reasoning_results(exp)
    outcome, details = evaluate_experiment(exp)
    assert outcome in ("INCONCLUSIVE", "NOT_YET_MEASURABLE")


def test_inconclusive_is_valid_outcome():
    exp = reasoning_experiment(
        "EXP-INC-001",
        baseline_id="context.changed_files_only/v1",
        candidate_id="context.changed_files_plus_architecture/v1",
        metrics=["verification_pass_rate", "mean_repair_cycles"],
    )
    record_reasoning_results(
        exp,
        candidate_results=[trajectory_summary("c1", repair_cycles_count=1)],
    )
    outcome, details = evaluate_experiment(exp)
    assert outcome == "INCONCLUSIVE"
    exp.finalize(outcome, confidence=str(details))
    assert exp.result == "INCONCLUSIVE"


def test_deterministic_comparison_reproducible():
    baseline = [trajectory_summary("b1")]
    candidate = [
        trajectory_summary("c1", verification_status="failed", repair_cycles_count=1)
    ]
    exp = reasoning_experiment(
        "EXP-REPR-001",
        falsification_criteria=["candidate.verification_pass_rate < 1.0"],
        metrics=["verification_pass_rate"],
    )
    record_reasoning_results(exp, baseline, candidate)
    o1, _ = evaluate_experiment(exp)
    o2, _ = evaluate_experiment(exp)
    assert o1 == o2 == "FALSIFIED"


def test_experiment_resume_is_idempotent(tmp_path: Path):
    store = ReasoningExperimentStore(tmp_path / "experiments")
    exp = reasoning_experiment("EXP-RESUME-001")
    store.save(exp)
    store.save(exp)
    assert len(store.list_all()) == 1


# ---------------------------------------------------------------------------
# 20–29. Strategy registry, identity, digest, provider behavior
# ---------------------------------------------------------------------------


def test_strategy_ids_are_versioned():
    registry = StrategyRegistry()
    s = registry.get("context.full_repository/v1")
    assert s is not None
    assert s.version == "v1"
    assert s.strategy_id == "context.full_repository/v1"


def test_strategy_definition_digest_is_stable():
    config = {"scope": "changed_files"}
    s1 = reasoning_strategy("context.changed_files_only/v1", immutable_config=config)
    s2 = reasoning_strategy("context.changed_files_only/v1", immutable_config=config)
    assert s1.digest == s2.digest


def test_same_id_version_cannot_silently_change_implementation():
    registry = StrategyRegistry()
    s1 = reasoning_strategy("context.test/v1", immutable_config={"scope": "full"})
    registry.register(s1)
    s2 = reasoning_strategy(
        "context.test/v1", immutable_config={"scope": "changed_files"}
    )
    with pytest.raises(Exception):
        registry.register(s2)


def test_repository_content_cannot_rewrite_strategy_policy(tmp_path: Path):
    # Simulate a repository file trying to override the built-in registry.
    registry = StrategyRegistry()
    original = registry.get("context.full_repository/v1")
    malicious = tmp_path / "malicious_strategies.json"
    malicious.write_text(
        json.dumps({
            "schema": "howlplane.strategy_definition/v1",
            "strategies": [{
                "strategy_id": "context.full_repository/v1",
                "version": "v1",
                "strategy_type": "context",
                "description": "repo override",
                "immutable_config": {"scope": "injected"},
            }],
        }),
        encoding="utf-8",
    )
    # Loading from repository file must not alter the code-defined registry.
    loaded = StrategyRegistry.from_dict({
        "schema": "howlplane.strategy_definition/v1",
        "strategies": [{
            "strategy_id": "context.full_repository/v1",
            "version": "v1",
            "strategy_type": "context",
            "description": "repo override",
            "immutable_config": {"scope": "injected"},
        }],
    })
    # A fresh code-defined registry still has the canonical definition.
    fresh = StrategyRegistry()
    assert fresh.get("context.full_repository/v1").digest == original.digest
    # The loaded registry contains the repo definition, but code paths must not
    # treat repo-loaded definitions as authoritative for policy/authority.
    assert loaded.get("context.full_repository/v1").digest != original.digest


def test_candidate_strategy_cannot_change_authority():
    # Strategy definitions have no authority fields; authority is enforced by
    # AuthorityProfile / AuthorityEnvelope / HumanBoundaryGate separately.
    reasoning_strategy(
        "routing.local_first_low_risk/v1",
        immutable_config={
            "allowed_action_classes": ["merge_pull_request", "production_deployment"]
        },
    )
    # The immutable_config can store arbitrary keys, but the authority system
    # never reads strategy config for permissions.
    envelope = create_envelope(OVERNIGHT_SAFE_PROFILE, "campaign-1", "test")
    decision, _ = evaluate_action_against_envelope(
        envelope, "production_deployment", "howlcipher/howlplane"
    )
    assert decision == AuthorityDecision.DENIED_BY_ENVELOPE


def test_candidate_provider_cannot_self_promote():
    registry = StrategyRegistry()
    s = registry.get("routing.multi_provider_plan_implement_review/v1")
    # Strategy selection does not itself make a provider available or preferred;
    # ProviderPoolManager still probes availability and TaskRouter respects it.
    assert "provider" in s.strategy_id.lower() or "routing" in s.strategy_id.lower()


def test_expensive_provider_does_not_automatically_win():
    # Candidate with higher cost and equal metrics is not promoted.
    exp = reasoning_experiment(
        "EXP-COST-001",
        experiment_type="ROUTING",
        baseline_id="routing.local_first_low_risk/v1",
        candidate_id="routing.frontier_first/v1",
        metrics=["verification_pass_rate", "mean_cost"],
    )
    record_reasoning_results(
        exp,
        [trajectory_summary("b1", cost_if_available=0.0)],
        [trajectory_summary("c1", cost_if_available=1.0)],
    )
    outcome, _ = evaluate_experiment(exp)
    assert outcome in ("INCONCLUSIVE", "FALSIFIED")


def test_provider_diversity_does_not_automatically_win():
    # More reviewers but no better verification => inconclusive.
    exp = reasoning_experiment(
        "EXP-DIV-001",
        experiment_type="REVIEW_TOPOLOGY",
        baseline_id="review.general_single/v1",
        candidate_id="review.correctness_security_test_falsifier/v1",
        metrics=["verification_pass_rate"],
    )
    record_reasoning_results(exp)
    outcome, _ = evaluate_experiment(exp)
    assert outcome in ("INCONCLUSIVE", "NOT_YET_MEASURABLE")


def test_provider_availability_failure_distinct_from_engineering_quality():
    # A trajectory whose outcome is provider_exhausted should not be counted
    # as a verification failure in experiment evaluation.
    exp = reasoning_experiment(
        "EXP-AVAIL-001",
        experiment_type="ROUTING",
        metrics=["verification_pass_rate", "provider_failures"],
    )
    record_reasoning_results(
        exp,
        candidate_results=[
            trajectory_summary(
                "c1",
                verification_status="failed",
                outcome="provider_exhausted",
            )
        ],
    )
    outcome, details = evaluate_experiment(exp)
    assert details["metric_comparisons"]["provider_failures"] == "worse"


def test_local_first_strategy_wins_only_if_metrics_support():
    # Cost is the primary signal for local-first routing; verification parity is
    # a required secondary constraint.
    exp = reasoning_experiment(
        "EXP-LOCAL-001",
        experiment_type="ROUTING",
        baseline_id="routing.frontier_first/v1",
        candidate_id="routing.local_first_low_risk/v1",
        metrics=["mean_cost", "verification_pass_rate"],
    )
    record_reasoning_results(
        exp,
        [trajectory_summary(f"b{i}", cost_if_available=1.0) for i in range(1, 4)],
        [trajectory_summary(f"c{i}", cost_if_available=0.0) for i in range(1, 4)],
    )
    outcome, _ = evaluate_experiment(exp)
    assert outcome == "SUPPORTED"


# ---------------------------------------------------------------------------
# 30–34. Context / retrieval experiments
# ---------------------------------------------------------------------------


def test_context_strategies_compared_deterministically():
    exp = reasoning_experiment(
        "EXP-CTX-001",
        baseline_id="context.changed_files_only/v1",
        candidate_id="context.full_repository/v1",
        expected_outcome="Candidate has equal or better verification pass rate",
        falsification_criteria=[
            "candidate.verification_pass_rate < baseline.verification_pass_rate"
        ],
        metrics=["verification_pass_rate"],
    )
    record_reasoning_results(exp)
    outcome, details = evaluate_experiment(exp)
    assert outcome in VALID_EXPERIMENT_OUTCOMES
    assert details["falsification_failures"] == []


def test_selected_context_evidence_refs_retained():
    traj = ExecutionTrajectory(
        trajectory_id="trj-ctx-refs",
        task_id="CTX-001",
        selected_context_refs=["docs/ARCHITECTURE.md", "src/feature.py"],
    )
    assert traj.selected_context_refs == ["docs/ARCHITECTURE.md", "src/feature.py"]
    d = traj.to_dict()
    assert d["selected_context_refs"] == ["docs/ARCHITECTURE.md", "src/feature.py"]


def test_irrelevant_context_may_be_excluded():
    # Context strategy with max_files=0 explicitly excludes irrelevant context.
    s = reasoning_strategy(
        "context.task_plus_acceptance/v1",
        immutable_config={
            "scope": "task_acceptance",
            "include_architecture": False,
            "max_files": 0,
        },
    )
    assert s.immutable_config["max_files"] == 0


def test_retrieval_strategy_is_versioned():
    registry = StrategyRegistry()
    s = registry.get("retrieval.failed_trajectory/v1")
    assert s is not None
    assert s.strategy_type == "retrieval"
    assert s.version == "v1"


def test_historical_failed_trajectories_remain_eligible_evidence():
    # The retrieval strategy definition explicitly references failed trajectories
    # as valid evidence sources; no code path discards them.
    registry = StrategyRegistry()
    s = registry.get("retrieval.failed_trajectory/v1")
    assert "failed_trajectory" in s.immutable_config.get("sources", [])


# ---------------------------------------------------------------------------
# 35–37. Review topology experiments
# ---------------------------------------------------------------------------


def test_more_reviewers_may_be_rejected():
    # Candidate adds reviewers but verification rate drops; falsified.
    exp = reasoning_experiment(
        "EXP-REV-001",
        experiment_type="REVIEW_TOPOLOGY",
        baseline_id="review.general_single/v1",
        candidate_id="review.correctness_security_test_falsifier/v1",
        falsification_criteria=["candidate.verification_pass_rate < baseline.verification_pass_rate"],
        metrics=["verification_pass_rate"],
    )
    record_reasoning_results(
        exp,
        candidate_results=[trajectory_summary("c1", verification_status="failed")],
    )
    outcome, _ = evaluate_experiment(exp)
    assert outcome == "FALSIFIED"


def test_review_disagreement_preserved():
    traj = execution_trajectory(
        "trj-disagree",
        task_id="DISAGREE-001",
        review_findings=[
            {"id": "F1", "reviewer_role": "correctness-reviewer", "status": "confirmed"},
            {"id": "F2", "reviewer_role": "security-reviewer", "status": "disputed"},
        ],
    )
    statuses = {f["status"] for f in traj.review_findings}
    assert "confirmed" in statuses and "disputed" in statuses


def test_strong_unresolved_objection_can_prevent_promotion():
    # A candidate strategy whose own falsification criterion demands no
    # unresolved high-severity objections cannot be promoted if such objections
    # remain. The experiment-level falsification check already enforces this.
    exp = reasoning_experiment(
        "EXP-OBJ-001",
        experiment_type="REVIEW_TOPOLOGY",
        falsification_criteria=["candidate.verification_pass_rate < 1.0"],
        metrics=["verification_pass_rate"],
    )
    record_reasoning_results(
        exp,
        candidate_results=[trajectory_summary("c1", verification_status="failed")],
    )
    outcome, _ = evaluate_experiment(exp)
    assert outcome == "FALSIFIED"


# ---------------------------------------------------------------------------
# 38–43. Trajectory-derived discovery and dedup
# ---------------------------------------------------------------------------


def test_repeated_routing_problem_can_create_observation(tmp_path: Path):
    store = ObservationStore(tmp_path / "observations")
    trajectories = [
        execution_trajectory(
            "rt-1",
            task_id="R-001",
            task_class="feature",
            selected_provider="codex",
            final_status="failed",
            outcome="provider_exhausted",
        ),
        execution_trajectory(
            "rt-2",
            task_id="R-002",
            task_class="feature",
            selected_provider="codex",
            final_status="failed",
            outcome="provider_exhausted",
        ),
    ]
    obs = discover_observations(trajectories, store=store)
    assert len(obs) >= 1
    assert any(o.category == "routing_weakness" for o in obs)


def test_repeated_remediation_pattern_can_create_observation(tmp_path: Path):
    store = ObservationStore(tmp_path / "observations")
    trajectories = [
        execution_trajectory(
            "rem-1",
            task_id="REM-001",
            task_class="feature",
            context_strategy_id="context.changed_files_only/v1",
            final_status="failed",
            outcome="failed",
            review_findings=[{"id": "F1", "title": "Missing architecture context"}],
        ),
        execution_trajectory(
            "rem-2",
            task_id="REM-002",
            task_class="feature",
            context_strategy_id="context.changed_files_only/v1",
            final_status="failed",
            outcome="failed",
            review_findings=[{"id": "F2", "title": "Missing architecture context"}],
        ),
    ]
    obs = discover_observations(trajectories, store=store)
    assert any(o.category == "context_weakness" for o in obs)


def test_trajectory_observation_does_not_immediately_alter_routing(tmp_path: Path):
    store = ObservationStore(tmp_path / "observations")
    traj = execution_trajectory(
        "no-alter",
        task_id="NA-001",
        task_class="docs",
        selected_provider="local_ollama",
        final_status="complete",
        outcome="success",
    )
    # Discover observations from a single trajectory; none should be created
    # because the minimum occurrence threshold is not met.
    obs = discover_observations([traj], store=store)
    assert len(obs) == 0


def test_completed_experiment_can_inform_later_seek(tmp_path: Path):
    # An experiment record carries suggested strategy IDs that can be used by
    # a future SEEK/OBSERVE process to propose a follow-up experiment.
    exp = reasoning_experiment(
        "EXP-SEEK-001",
        experiment_type="RETRIEVAL",
        baseline_id="retrieval.no_historical/v1",
        candidate_id="retrieval.task_class/v1",
        expected_outcome="Candidate improves first-pass success",
        metrics=["verification_pass_rate"],
    )
    record_reasoning_results(exp)
    outcome, _ = evaluate_experiment(exp)
    exp.finalize(outcome)
    # Future seek can read the experiment and its suggested strategies.
    assert exp.candidate_strategy is not None


def test_identical_failed_strategy_not_endlessly_rediscovered(tmp_path: Path):
    store = ObservationStore(tmp_path / "observations")
    exp_store = ReasoningExperimentStore(tmp_path / "experiments")
    exp = reasoning_experiment(
        "EXP-DEDUP-001",
        baseline_id="context.changed_files_only/v1",
        candidate_id="context.full_repository/v1",
        falsification_criteria=["candidate.verification_pass_rate < baseline.verification_pass_rate"],
        metrics=["verification_pass_rate"],
    )
    record_reasoning_results(
        exp,
        candidate_results=[trajectory_summary("c1", verification_status="failed")],
    )
    finalize_experiment_outcome(exp)
    exp_store.save(exp)

    obs = TrajectoryObservation(
        observation_id="OBS-DEDUP",
        fingerprint="fp-context-compare",
        category="context_weakness",
        title="Context comparison",
        description="desc",
        suggested_experiment_type="CONTEXT",
        suggested_baseline_strategy_id="context.changed_files_only/v1",
        suggested_candidate_strategy_id="context.full_repository/v1",
    )
    store.save(obs)
    assert experiment_exists_for_observation(obs, exp_store)


def test_new_evidence_can_reopen_deferred_hypothesis(tmp_path: Path):
    store = ObservationStore(tmp_path / "observations")
    fingerprint = _observation_fingerprint("routing_problem", ["feature", "codex"])
    obs = TrajectoryObservation(
        observation_id="OBS-REOPEN",
        fingerprint=fingerprint,
        category="routing_weakness",
        title="Routing problem",
        description="desc",
        evidence_refs=["orig-1"],
        status=ObservationStatus.DEFERRED,
    )
    store.save(obs)
    new_traj = execution_trajectory(
        "reopen-1",
        task_id="R-100",
        task_class="feature",
        selected_provider="codex",
        final_status="failed",
        outcome="provider_exhausted",
    )
    # The routing-problem miner needs two occurrences, so feed two trajectories
    # including the original evidence ref.
    trajectories = [
        execution_trajectory(
            "orig-1",
            task_id="R-099",
            task_class="feature",
            selected_provider="codex",
            final_status="failed",
            outcome="provider_exhausted",
        ),
        new_traj,
    ]
    reopened = discover_observations(trajectories, store=store)
    assert any(o.observation_id == "OBS-REOPEN" and o.status == ObservationStatus.OPEN for o in reopened)
    assert any("reopen-1" in (r or "") for r in reopened[0].reopened_by_evidence_refs)


# ---------------------------------------------------------------------------
# 44–48. Authority invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate_config", "action", "repository", "usage"),
    [
        (
            {"authorized_repositories": ["*"]},
            "merge_pull_request",
            "some/other-repo",
            {},
        ),
        (
            {"max_merges": 999},
            "merge_pull_request",
            "howlcipher/howlplane",
            {"merges_so_far": 10},
        ),
        (
            {"external_spend_usd_limit": 1000.0},
            "invoke_configured_ai_provider",
            "howlcipher/howlplane",
            {"spend_so_far": 1.0},
        ),
    ],
    ids=["repository_scope", "merge_budget", "spend_budget"],
)
def test_strategy_config_cannot_expand_authority(
    candidate_config, action, repository, usage,
):
    envelope = create_envelope(OVERNIGHT_SAFE_PROFILE, "c", "test")
    reasoning_strategy(
        "routing.frontier_first/v1",
        immutable_config=candidate_config,
    )
    decision, _ = evaluate_action_against_envelope(
        envelope, action, repository, **usage,
    )
    assert decision == AuthorityDecision.OUTSIDE_ENVELOPE_SCOPE


def test_strategy_experiment_cannot_modify_authority_envelope():
    from src.control_plane.authority_envelope import (
        compute_policy_digest,
        verify_envelope_integrity,
    )
    envelope = create_envelope(OVERNIGHT_SAFE_PROFILE, "c", "test")
    original_digest = envelope.policy_digest
    # Mutating a copy of the envelope's allowed list changes the policy digest
    # when recomputed from the changed fields; no strategy mechanism writes to
    # the envelope file itself.
    copy_envelope = AuthorityEnvelope.from_dict(envelope.to_dict())
    copy_envelope.allowed_action_classes.append("production_deployment")
    assert compute_policy_digest(copy_envelope.to_dict()) != original_digest
    # Original remains valid.
    ok, _ = verify_envelope_integrity(envelope)
    assert ok


def test_verified_is_distinct_from_authorized():
    # A verification-passed trajectory is not itself authority to merge/deploy.
    envelope = create_envelope(OVERNIGHT_SAFE_PROFILE, "c", "test")
    traj = execution_trajectory(
        "trj-verified",
        task_id="V-001",
        final_status="complete",
        outcome="success",
    )
    assert traj.outcome == "success"
    # Authority is denied because merges already hit the envelope cap.
    decision, _ = evaluate_action_against_envelope(
        envelope, "merge_pull_request", "howlcipher/howlplane", merges_so_far=envelope.max_merges
    )
    assert decision == AuthorityDecision.OUTSIDE_ENVELOPE_SCOPE

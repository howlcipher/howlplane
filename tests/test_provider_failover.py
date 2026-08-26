#!/usr/bin/env python3
"""
tests/test_provider_failover.py

Deterministic tests for bounded provider failover during ordinary governed
implementation. These tests exercise the orchestrator's implementation-attempt
loop with fake backends and temporary Git repositories, verifying rollback,
evidence preservation, progress messaging, and final identity/reviewer behavior.
"""

from datetime import datetime, timedelta, timezone
import json
from io import StringIO
from pathlib import Path
import subprocess
from typing import Any, Callable, Dict, List, Optional
import pytest

from src.control_plane.agent_execution import (
    AgentBackend,
    AgentBackendRegistry,
    AgentExecutionResult,
    FakeAgentBackend,
    LAUNCH_OUTCOME_KEY,
    LAUNCH_OUTCOME_LAUNCHED,
    TIMEOUT_SOURCE_HARNESS,
    TIMEOUT_SOURCE_KEY,
)
from src.control_plane.agent_registry import AgentProfile, AgentRegistry
from src.control_plane.git_baseline import (
    GitBaseline,
    capture_baseline,
    capture_delta,
    restore_repository_to_baseline,
)
from src.control_plane.orchestrator import (
    FAILURE_CLASS_ENGINEERING,
    FAILURE_CLASS_PROVIDER_EXHAUSTED,
    GovernedTaskOrchestrator,
    OrchestrationConfig,
    OrchestrationResult,
)
from src.control_plane.progress import TaskProgressTracker, TaskPhase
from src.control_plane.resource_models import ProviderFailureClass
from src.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderPoolManager,
)
from src.infrastructure.config_loader import ProviderResourceSettings
from src.control_plane.task_spec import TaskSpec
from tests._dogfood_test_helpers import init_minimal_python_repo
from tests._git_test_helpers import commit_all, git_in_repo


def _init_test_repo(tmp_path: Path) -> Path:
    """Returns a minimal Python repo with a deterministic test."""
    return init_minimal_python_repo(tmp_path)


def _profile(agent_id: str, name: str, provider: str, **overrides) -> AgentProfile:
    """Builds a deterministic subscription-included CLI resource profile."""
    fields = dict(
        interface="headless_cli",
        capabilities=["code_generation", "file_editing", "code_review"],
        reasoning_tier="tier_2",
        supports_repository_access=True,
        cost_class="subscription_included",
    )
    fields.update(overrides)
    return AgentProfile(agent_id=agent_id, name=name, provider=provider, **fields)


def _make_registry(extra: Optional[List[AgentProfile]] = None) -> AgentRegistry:
    """Builds a small deterministic registry for failover tests."""
    profiles: List[AgentProfile] = [
        _profile("resource_a", "Resource A", "provider_x"),
        _profile("resource_b", "Resource B", "provider_y"),
        _profile("resource_c", "Resource C", "provider_y"),
    ]
    if extra:
        profiles.extend(extra)
    return AgentRegistry(profiles)


def _make_registry_three_providers() -> AgentRegistry:
    """Registry where every resource has a different provider, enabling independent review."""
    return AgentRegistry([
        _profile("resource_a", "Resource A", "provider_x"),
        _profile("resource_b", "Resource B", "provider_y"),
        _profile("resource_c", "Resource C", "provider_z"),
    ])


def _make_task(task_id: str = "TEST-FAILOVER-01") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        repository="test_repo",
        objective="Implement a tiny feature",
        acceptance_criteria=["Feature works"],
        task_class="feature",
        risk_level="medium",
        reviewer_requirements=["correctness-reviewer"],
    )


class _FakeBackendResolver:
    """Maps resource IDs to FakeAgentBackend instances with test behaviors."""

    def __init__(self, behaviors: Dict[str, Dict[str, Any]]):
        self.behaviors = behaviors
        self.backends: Dict[str, FakeAgentBackend] = {}
        self.calls: Dict[str, List[Dict[str, Any]]] = {}

    def __call__(self, resource_id: str) -> AgentBackend:
        if resource_id not in self.backends:
            self.backends[resource_id] = self._build(resource_id)
        return self.backends[resource_id]

    def _build(self, resource_id: str) -> FakeAgentBackend:
        behavior = self.behaviors.get(resource_id, {"success": True})
        side_effect = behavior.get("side_effect")

        def wrapped_side_effect(task, cwd, prompt):
            self.calls.setdefault(resource_id, []).append({
                "task_id": task.task_id if task else None,
                "cwd": str(cwd),
                "prompt": prompt,
            })
            if side_effect:
                side_effect(task, cwd, prompt)

        return FakeAgentBackend(
            agent_id=resource_id,
            default_exit_code=0 if behavior.get("success", True) else 1,
            default_stderr=behavior.get("stderr", ""),
            default_stdout=behavior.get("stdout", ""),
            side_effect=wrapped_side_effect,
            default_timed_out=behavior.get("timed_out", False),
            default_metadata=behavior.get("metadata"),
        )


def _run_failover_task(
    repo: Path,
    resolver: _FakeBackendResolver,
    task: Optional[TaskSpec] = None,
    max_attempts: int = 3,
    reviewer_fn: Optional[Callable[[str, str, TaskSpec], str]] = None,
    policy_allow_paid: bool = False,
    progress_stream: Optional[StringIO] = None,
    registry: Optional[AgentRegistry] = None,
    pool_hook: Optional[Callable[[ProviderPoolManager], None]] = None,
    pool: Optional[ProviderPoolManager] = None,
) -> OrchestrationResult:
    """Runs the orchestrator with the given fake backend resolver."""
    task = task or _make_task()
    registry = registry or _make_registry()
    if pool is None:
        resources = {
            profile.resource_id: ProviderResourceSettings(enabled=True)
            for profile in registry.list_resources()
        }
        pool = ProviderPoolManager(
            registry=registry,
            backend_resolver=resolver,
            probe_on_start=False,
            policy=None,
            resources=resources,
            operating_mode="connected",
        )
    if not policy_allow_paid:
        pool.policy.allow_paid_api = False
    if pool_hook is not None:
        pool_hook(pool)

    config = OrchestrationConfig(
        provider_pool=pool,
        backend_resolver=resolver,
        custom_reviewer_fn=reviewer_fn or (lambda role, diff, task: "findings: []\n"),
        acquire_locks=False,
        enable_howlframe_audit=False,
        max_provider_failover_attempts=max_attempts,
        progress_mode="human",
        progress_stream=progress_stream,
        trajectory_store_dir=str(repo / ".task_runs" / task.task_id / "trajectories"),
    )
    orch = GovernedTaskOrchestrator(target_repo=repo, config=config)
    return orch.run(task)


def _read_file(repo: Path, rel_path: str) -> str:
    return (repo / rel_path).read_text(encoding="utf-8")


def _edit_feature_to_false(_task, cwd: Path, _prompt) -> None:
    (cwd / "src" / "feature.py").write_text(
        "def run():\n    return False\n", encoding="utf-8"
    )


def _edit_feature_to_true(_task, cwd: Path, _prompt) -> None:
    (cwd / "src" / "feature.py").write_text(
        "def run():\n    return True\n", encoding="utf-8"
    )


def _capture_stderr(func: Callable[[], OrchestrationResult]) -> str:
    """Captures the human-mode progress output emitted during a run."""
    import sys
    old_stderr = sys.stderr
    stream = StringIO()
    sys.stderr = stream
    try:
        result = func()
    finally:
        sys.stderr = old_stderr
    return stream.getvalue(), result


_TIMEOUT_STDERR = "Error: timeout waiting for response\n"


def _resolver_from_plan(
    plan: Dict[str, Dict[str, Any]],
    overrides: Dict[str, Dict[str, Any]],
) -> _FakeBackendResolver:
    """Builds a resolver from a base plan with per-resource keys merged in."""
    merged = {
        resource_id: {**behavior, **overrides.get(resource_id, {})}
        for resource_id, behavior in plan.items()
    }
    merged.update({
        resource_id: extra
        for resource_id, extra in overrides.items()
        if resource_id not in plan
    })
    return _FakeBackendResolver(merged)


def _three_hop_resolver(**overrides) -> _FakeBackendResolver:
    """resource_a and resource_b both transport-fail; resource_c succeeds."""
    return _resolver_from_plan({
        "resource_a": {"success": False, "stderr": _TIMEOUT_STDERR},
        "resource_b": {"success": False, "stderr": _TIMEOUT_STDERR},
        "resource_c": {"success": True, "side_effect": _edit_feature_to_true},
    }, overrides)


def _expire_cooldown_for(pool_holder: Dict[str, Any], resource_id: str) -> Callable:
    """Back-dates a resource's cooldown, as a long next attempt would in real time."""
    def _side_effect(_task, _cwd, _prompt) -> None:
        state = pool_holder["pool"].get_resource_status(resource_id)
        state.retry_after = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
    return _side_effect


def _capture_pool(pool_holder: Dict[str, Any]) -> Callable[[ProviderPoolManager], None]:
    """Exposes the orchestrator's pool to a test's backend side effects."""
    def _hook(pool: ProviderPoolManager) -> None:
        pool_holder["pool"] = pool
    return _hook


def _timeout_then_success_resolver(**overrides) -> _FakeBackendResolver:
    """resource_a fails with an AGY-style transport timeout; resource_b succeeds.

    `overrides` merges extra keys into a resource's plan, e.g.
    `_timeout_then_success_resolver(resource_a={"side_effect": fn})`.
    """
    return _resolver_from_plan({
        "resource_a": {"success": False, "stderr": _TIMEOUT_STDERR},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    }, overrides)


def _all_fail_resolver(*resource_ids: str) -> _FakeBackendResolver:
    """Every named resource fails with an AGY-style transport timeout."""
    return _FakeBackendResolver({
        resource_id: {"success": False, "stderr": _TIMEOUT_STDERR}
        for resource_id in resource_ids
    })


def _assert_completed_on_third_hop(res: OrchestrationResult) -> None:
    """Asserts bounded failover walked a -> b -> c and finished on resource_c."""
    assert res.final_state == "complete"
    assert res.executing_provider == "resource_c"
    assert [a["resource_id"] for a in res.implementation_attempts] == [
        "resource_a", "resource_b", "resource_c",
    ]


def _run_all_fail(tmp_path: Path, **kwargs) -> OrchestrationResult:
    """Runs a three-resource pool in which no implementation attempt succeeds."""
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _all_fail_resolver("resource_a", "resource_b", "resource_c")
    return _run_failover_task(repo, resolver, **kwargs)


def _run_timeout_failover(tmp_path: Path, **kwargs) -> OrchestrationResult:
    """Runs the canonical timeout-then-failover scenario in a fresh repo."""
    repo = _init_test_repo(tmp_path / "repo")
    return _run_failover_task(repo, _timeout_then_success_resolver(), **kwargs)


# ---------------------------------------------------------------------------
# Scenario 1: first implementation resource succeeds, no failover
# ---------------------------------------------------------------------------
def test_first_resource_succeeds_no_failover(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": True, "side_effect": _edit_feature_to_true},
    })
    res = _run_failover_task(repo, resolver)

    assert res.final_state == "complete"
    assert res.executing_provider == "resource_a"
    assert len(res.implementation_attempts) == 1
    assert res.implementation_attempts[0]["success"] is True
    assert res.implementation_attempts[0]["resource_id"] == "resource_a"


# ---------------------------------------------------------------------------
# Scenario 2: first resource availability-fails with zero edits, second selected
# ---------------------------------------------------------------------------
def test_first_resource_fails_zero_edits_second_selected(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _timeout_then_success_resolver()
    res = _run_failover_task(repo, resolver)

    assert res.final_state == "complete"
    assert res.executing_provider == "resource_b"
    attempts = res.implementation_attempts
    assert len(attempts) == 2
    assert attempts[0]["success"] is False
    assert attempts[0]["resource_id"] == "resource_a"
    assert attempts[0]["failure_class"] == ProviderFailureClass.TRANSPORT_UNAVAILABLE.value
    assert attempts[1]["success"] is True
    assert attempts[1]["resource_id"] == "resource_b"


# ---------------------------------------------------------------------------
# Scenario 3: timeout after modifying a tracked file, patch preserved, repo restored
# ---------------------------------------------------------------------------
def test_timeout_partial_work_preserved_and_repo_restored(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")

    def a_side_effect(task, cwd, prompt):
        target = Path(cwd) / "src" / "feature.py"
        target.write_text("def run():\n    return 'partial from a'\n", encoding="utf-8")

    resolver = _timeout_then_success_resolver(
        resource_a={"side_effect": a_side_effect},
    )
    res = _run_failover_task(repo, resolver)

    assert res.final_state == "complete"
    assert res.executing_provider == "resource_b"

    # Resource B must observe the pre-attempt baseline, not A's edit.
    b_calls = resolver.calls.get("resource_b", [])
    assert b_calls
    final_content = _read_file(repo, "src/feature.py")
    assert final_content == "def run():\n    return True\n"

    # A's partial patch is preserved as evidence.
    run_dir = Path(res.run_dir)
    a_attempt = run_dir / "implementation" / "attempts" / "01-resource_a"
    assert (a_attempt / "partial_work.patch").is_file()
    partial = (a_attempt / "partial_work.patch").read_text(encoding="utf-8")
    assert "partial from a" in partial

    # B's final patch does not contain A's edit.
    final_diff = (run_dir / "diff.patch").read_text(encoding="utf-8")
    assert "partial from a" not in final_diff


# ---------------------------------------------------------------------------
# Scenario 4: first resource adds a file then times out, file preserved and removed
# ---------------------------------------------------------------------------
def test_added_file_preserved_as_evidence_and_removed_before_failover(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")

    def a_side_effect(task, cwd, prompt):
        target = Path(cwd) / "src" / "added_by_a.py"
        target.write_text("x = 1\n", encoding="utf-8")

    resolver = _timeout_then_success_resolver(
        resource_a={"side_effect": a_side_effect},
    )
    res = _run_failover_task(repo, resolver)

    assert res.final_state == "complete"

    a_attempt = Path(res.run_dir) / "implementation" / "attempts" / "01-resource_a"
    assert (a_attempt / "partial_work.patch").is_file()
    partial = (a_attempt / "partial_work.patch").read_text(encoding="utf-8")
    assert "added_by_a.py" in partial

    # The added file must not remain in the working tree after rollback.
    assert not (repo / "src" / "added_by_a.py").exists()


# ---------------------------------------------------------------------------
# Scenario 5: pre-existing modified file survives rollback byte-for-byte
# ---------------------------------------------------------------------------
def test_pre_existing_modified_file_survives_rollback(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    # Create a pre-existing modification to a file that is not part of the test contract.
    pre_existing = repo / "src" / "other.py"
    pre_existing.write_text("value = 'pre-existing'\n", encoding="utf-8")
    original_content = pre_existing.read_text(encoding="utf-8")

    def a_side_effect(task, cwd, prompt):
        target = Path(cwd) / "src" / "other.py"
        target.write_text("value = 'partial from a'\n", encoding="utf-8")

    def b_side_effect(task, cwd, prompt):
        # B makes its intended edit in a separate file so the pre-existing
        # modified file is not overwritten; this lets us assert rollback
        # preserved the pre-existing content.
        (Path(cwd) / "src" / "added_by_b.py").write_text("y = 2\n", encoding="utf-8")

    resolver = _timeout_then_success_resolver(
        resource_a={"side_effect": a_side_effect},
        resource_b={"side_effect": b_side_effect},
    )
    res = _run_failover_task(repo, resolver)

    assert res.final_state == "complete"
    # Pre-existing modification must survive A's failed attempt rollback byte-for-byte.
    assert _read_file(repo, "src/other.py") == original_content
    assert (repo / "src" / "added_by_b.py").exists()


# ---------------------------------------------------------------------------
# Scenario 6: pre-existing untracked file survives rollback
# ---------------------------------------------------------------------------
def test_pre_existing_untracked_file_survives_rollback(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    untracked = repo / "notes.txt"
    untracked.write_text("keep me\n", encoding="utf-8")

    resolver = _timeout_then_success_resolver()
    res = _run_failover_task(repo, resolver)

    assert res.final_state == "complete"
    assert untracked.read_text(encoding="utf-8") == "keep me\n"


# ---------------------------------------------------------------------------
# Scenario 7: .task_runs/ survives rollback and is never attributed to implementation
# ---------------------------------------------------------------------------
def test_task_runs_survives_rollback_and_not_attributed(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    task_runs = repo / ".task_runs"
    old_task = task_runs / "OLD-TASK"
    old_task.mkdir(parents=True, exist_ok=True)
    (old_task / "task.yaml").write_text("task_id: OLD-TASK\n", encoding="utf-8")

    resolver = _timeout_then_success_resolver()
    res = _run_failover_task(repo, resolver)

    assert res.final_state == "complete"
    assert (old_task / "task.yaml").exists()

    # The implementation delta should not attribute .task_runs changes.
    final_delta = res.final_delta
    assert final_delta is not None
    for path in final_delta.files_added + final_delta.files_modified:
        assert not path.startswith(".task_runs"), path


# ---------------------------------------------------------------------------
# Scenario 8: engineering failure does not trigger provider failover
# ---------------------------------------------------------------------------
def test_engineering_failure_does_not_failover(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {
            "success": False,
            "stderr": "SyntaxError: invalid syntax at opcode.go:10\n",
        },
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
    res = _run_failover_task(repo, resolver, max_attempts=3)

    assert res.final_state == "failed"
    assert res.failure_class == FAILURE_CLASS_ENGINEERING
    assert res.executing_provider == "resource_a"
    # Resource B should never have been invoked.
    assert "resource_b" not in resolver.calls
    assert len(res.implementation_attempts) == 1


# ---------------------------------------------------------------------------
# Scenario 9: all eligible availability attempts fail, bounded terminal failure
# ---------------------------------------------------------------------------
def test_all_availability_attempts_fail_bounded(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_c": {"success": False, "stderr": "Error: timeout waiting for response\n"},
    })
    res = _run_failover_task(repo, resolver, max_attempts=3)

    assert res.final_state == "failed"
    assert res.failure_class == FAILURE_CLASS_PROVIDER_EXHAUSTED
    attempts = res.implementation_attempts
    assert len(attempts) == 3
    assert {a["resource_id"] for a in attempts} == {"resource_a", "resource_b", "resource_c"}


# ---------------------------------------------------------------------------
# Scenario 10: failed resource is not immediately reselected while unavailable/cooling down
# ---------------------------------------------------------------------------
def test_failed_resource_not_immediately_reselected(tmp_path: Path):
    res = _run_timeout_failover(tmp_path)

    assert res.final_state == "complete"
    attempts = res.implementation_attempts
    assert [a["resource_id"] for a in attempts] == ["resource_a", "resource_b"]


# ---------------------------------------------------------------------------
# Scenario 11: subscription/paid-API policy remains enforced
# ---------------------------------------------------------------------------
def test_paid_api_policy_remains_enforced(tmp_path: Path):
    registry = _make_registry([
        _profile("paid_api", "Paid API", "provider_z",
                 interface="api", cost_class="paid_api"),
    ])
    res = _run_timeout_failover(tmp_path, registry=registry)

    assert res.final_state == "complete"
    attempts = res.implementation_attempts
    assert all(a["resource_id"] != "paid_api" for a in attempts)


# ---------------------------------------------------------------------------
# Scenario 12: task state remains valid across implementation attempts
# ---------------------------------------------------------------------------
def test_task_state_valid_across_attempts(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _timeout_then_success_resolver()
    task = _make_task()
    res = _run_failover_task(repo, resolver, task=task)

    assert res.final_state == "complete"
    assert task.current_state == "complete"


# ---------------------------------------------------------------------------
# Scenario 13: successful second provider becomes actual implementation identity
# ---------------------------------------------------------------------------
def test_second_provider_becomes_actual_identity(tmp_path: Path):
    res = _run_timeout_failover(tmp_path)

    assert res.final_state == "complete"
    assert res.executing_provider == "resource_b"
    assert res.provider_execution is not None
    assert res.provider_execution.agent_id == "resource_b"


# ---------------------------------------------------------------------------
# Scenario 14: initial routing identity remains preserved in history
# ---------------------------------------------------------------------------
def test_initial_routing_identity_preserved(tmp_path: Path):
    res = _run_timeout_failover(tmp_path)

    routing = res.routing_decision
    assert routing is not None
    assert routing.selected_agent_id == "resource_a"
    assert routing.selected_agent_name == "Resource A"
    # The trajectory/attempt metadata should keep both.
    assert any(a["resource_id"] == "resource_a" for a in res.implementation_attempts)
    assert any(a["resource_id"] == "resource_b" for a in res.implementation_attempts)


# ---------------------------------------------------------------------------
# Scenario 15: reviewer assignment is recomputed after implementation provider changes
# ---------------------------------------------------------------------------
def test_reviewer_assignment_recomputed_after_failover(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _timeout_then_success_resolver()
    registry = _make_registry_three_providers()
    res = _run_failover_task(repo, resolver, registry=registry)

    routing = res.routing_decision
    assert routing is not None
    mapping = routing.metadata.get("reviewer_resource_mapping", {})
    assert "correctness-reviewer" in mapping
    # With resource_a (the initial implementer) unavailable, the reviewer must
    # be a different resource and different provider from resource_b.
    assert mapping["correctness-reviewer"] == "resource_c"
    assert routing.metadata.get("review_diversity_achieved") is True


# ---------------------------------------------------------------------------
# Scenario 16: independent review is not falsely claimed after failover
# ---------------------------------------------------------------------------
def test_independent_review_truthfully_reported(tmp_path: Path):
    # Use only two resources from the same provider so diversity is impossible.
    registry = AgentRegistry([
        _profile("resource_a", "Resource A", "provider_y"),
        _profile("resource_b", "Resource B", "provider_y"),
    ])
    res = _run_timeout_failover(tmp_path, registry=registry)

    assert res.final_state == "complete"
    routing = res.routing_decision
    assert routing.metadata.get("review_diversity_achieved") is False


# ---------------------------------------------------------------------------
# Scenario 17: verification plan survives failover
# ---------------------------------------------------------------------------
def test_verification_plan_survives_failover(tmp_path: Path):
    res = _run_timeout_failover(tmp_path)

    assert res.final_state == "complete"
    assert res.verification_plan is not None
    assert len(res.verification_plan.steps) > 0


# ---------------------------------------------------------------------------
# Scenario 18: verification executes after successful failover
# ---------------------------------------------------------------------------
def test_verification_executes_after_successful_failover(tmp_path: Path):
    res = _run_timeout_failover(tmp_path)

    assert res.final_state == "complete"
    assert res.verification_plan is not None
    assert res.verification_plan.overall_status == "passed"


# ---------------------------------------------------------------------------
# Scenario 19: progress prints IMPLEMENTATION FAILED rather than IMPLEMENTATION COMPLETE
# ---------------------------------------------------------------------------
def test_progress_prints_implementation_failed(tmp_path: Path):
    stderr, res = _capture_stderr(lambda: _run_timeout_failover(tmp_path))

    assert res.final_state == "complete"
    assert "IMPLEMENTATION FAILED" in stderr
    assert "IMPLEMENTATION COMPLETE" not in stderr
    # There should be no false COMPLETE for the failed attempt.
    complete_lines = [line for line in stderr.splitlines() if "IMPLEMENTATION COMPLETE" in line]
    assert len(complete_lines) == 0


# ---------------------------------------------------------------------------
# Scenario 20: progress emits a clear failover event
# ---------------------------------------------------------------------------
def test_progress_emits_failover_event(tmp_path: Path):
    stderr, res = _capture_stderr(lambda: _run_timeout_failover(tmp_path))

    assert res.final_state == "complete"
    assert "FAILOVER" in stderr
    assert "resource_a -> resource_b" in stderr
    assert ProviderFailureClass.TRANSPORT_UNAVAILABLE.value in stderr


# ---------------------------------------------------------------------------
# Scenario 21: trajectory records source, target, failure class, attempts, outcome
# ---------------------------------------------------------------------------
def test_trajectory_records_failover_chain(tmp_path: Path):
    res = _run_timeout_failover(tmp_path)

    assert res.final_state == "complete"
    assert res.trajectory_id is not None
    traj_path = Path(res.run_dir) / "trajectories" / f"{res.trajectory_id}.json"
    assert traj_path.is_file()
    traj = json.loads(traj_path.read_text(encoding="utf-8"))

    assert traj["initial_implementation_resource"] == "resource_a"
    assert traj["final_implementation_resource"] == "resource_b"
    assert len(traj["failover_events"]) == 1
    ev = traj["failover_events"][0]
    assert ev["source_resource"] == "resource_a"
    assert ev["target_resource"] == "resource_b"
    assert ev["failure_class"] == ProviderFailureClass.TRANSPORT_UNAVAILABLE.value

    attempts = traj["implementation_attempts"]
    assert len(attempts) == 2
    assert attempts[0]["attempt"] == 1
    assert attempts[1]["attempt"] == 2
    assert traj["final_status"] == "complete"
    assert traj["outcome"] == "success"


# ---------------------------------------------------------------------------
# Acceptance canary: resource A edits and fails, resource B asserts clean baseline and succeeds
# ---------------------------------------------------------------------------
def test_acceptance_canary_failover_success(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")

    def a_side_effect(task, cwd, prompt):
        target = Path(cwd) / "src" / "feature.py"
        target.write_text("def run():\n    return 'wrong'\n", encoding="utf-8")

    def b_side_effect(task, cwd, prompt):
        target = Path(cwd) / "src" / "feature.py"
        # B must observe a clean baseline (the original implementation).
        content = target.read_text(encoding="utf-8")
        assert "wrong" not in content
        assert "return True" in content
        target.write_text("def run():\n    return True\n", encoding="utf-8")

    # Use three different providers so an independent reviewer remains available
    # after resource_a becomes unavailable and resource_b implements the change.
    registry = _make_registry_three_providers()
    resolver = _FakeBackendResolver({
        "resource_a": {
            "success": False,
            "stderr": "Error: timeout waiting for response\n",
            "side_effect": a_side_effect,
        },
        "resource_b": {
            "success": True,
            "side_effect": b_side_effect,
        },
    })
    res = _run_failover_task(repo, resolver, registry=registry)

    assert res.final_state == "complete"
    assert res.executing_provider == "resource_b"
    assert _read_file(repo, "src/feature.py") == "def run():\n    return True\n"

    run_dir = Path(res.run_dir)
    a_attempt = run_dir / "implementation" / "attempts" / "01-resource_a"
    assert (a_attempt / "partial_work.patch").is_file()
    assert "wrong" in (a_attempt / "partial_work.patch").read_text(encoding="utf-8")

    routing = res.routing_decision
    assert routing is not None
    mapping = routing.metadata.get("reviewer_resource_mapping", {})
    # Resource C (provider_z) is independent from resource B (provider_y).
    assert mapping.get("correctness-reviewer") == "resource_c"
    assert routing.metadata.get("review_diversity_achieved") is True

    assert res.verification_plan is not None
    assert res.verification_plan.overall_status == "passed"


# ---------------------------------------------------------------------------
# Direct rollback helper tests
# ---------------------------------------------------------------------------
def test_restore_repository_to_baseline_rollback_only_task_changes(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    # Pre-existing modification
    (repo / "src" / "feature.py").write_text(
        "def run():\n    return 'pre-existing'\n", encoding="utf-8"
    )
    baseline = capture_baseline(repo)

    # Attempt changes
    (repo / "src" / "feature.py").write_text("def run():\n    return 'attempt'\n", encoding="utf-8")
    (repo / "src" / "added.py").write_text("x = 1\n", encoding="utf-8")

    delta = capture_delta(repo, baseline)
    ok, reason = restore_repository_to_baseline(repo, baseline, delta)
    assert ok, reason

    assert _read_file(repo, "src/feature.py") == "def run():\n    return 'pre-existing'\n"
    assert not (repo / "src" / "added.py").exists()


def test_restore_repository_to_baseline_preserves_untracked(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    untracked = repo / "notes.txt"
    untracked.write_text("keep\n", encoding="utf-8")
    baseline = capture_baseline(repo)

    (repo / "src" / "feature.py").write_text("def run():\n    return False\n", encoding="utf-8")
    delta = capture_delta(repo, baseline)
    ok, reason = restore_repository_to_baseline(repo, baseline, delta)
    assert ok, reason

    assert untracked.read_text(encoding="utf-8") == "keep\n"


# ---------------------------------------------------------------------------
# HOWLFRAM-SLOPFIX-03 regression: bounded failover must reach a third provider
#
# Live behavior: agy transport-failed (300s cooldown), codex then ran for 600s
# and also failed. By selection time agy's cooldown had lapsed, so the pool
# re-offered agy -- the deterministic router's top-ranked candidate -- and the
# orchestrator terminated on "already attempted" instead of trying claude_code
# or devin_cli, both still eligible with one attempt of three unused.
# ---------------------------------------------------------------------------
def test_second_availability_failure_reaches_third_provider(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_failover_task(repo, _three_hop_resolver(), max_attempts=3)

    _assert_completed_on_third_hop(res)


def test_recovered_first_provider_does_not_dead_end_failover(tmp_path: Path):
    """The exact live condition: attempt 1's cooldown lapses during attempt 2."""
    repo = _init_test_repo(tmp_path / "repo")
    pool_holder: Dict[str, Any] = {}
    resolver = _three_hop_resolver(
        resource_b={"side_effect": _expire_cooldown_for(pool_holder, "resource_a")},
    )
    res = _run_failover_task(
        repo, resolver, max_attempts=3, pool_hook=_capture_pool(pool_holder),
    )

    # resource_a is healthy and top-ranked again at selection time, but spent.
    assert pool_holder["pool"].get_resource_status("resource_a").retry_after is None
    _assert_completed_on_third_hop(res)
    second = res.implementation_attempts[1]["next_selection"]
    assert second["selected_resource_id"] == "resource_c"
    assert {
        exclusion["resource_id"]: exclusion["reason"]
        for exclusion in second["exclusions"]
    }["resource_a"] == "ALREADY_ATTEMPTED"


def test_rollback_precedes_each_failover_hop(tmp_path: Path):
    """Baseline is restored before provider B and again before provider C."""
    repo = _init_test_repo(tmp_path / "repo")
    observed: List[str] = []

    def _record_then_edit(marker: str):
        def _side_effect(_task, cwd: Path, _prompt) -> None:
            observed.append(_read_file(cwd, "src/feature.py"))
            (cwd / "src" / "feature.py").write_text(
                f"def run():\n    return '{marker}'\n", encoding="utf-8"
            )
        return _side_effect

    def _record_then_succeed(_task, cwd: Path, prompt) -> None:
        observed.append(_read_file(cwd, "src/feature.py"))
        _edit_feature_to_true(_task, cwd, prompt)

    resolver = _three_hop_resolver(
        resource_a={"side_effect": _record_then_edit("a")},
        resource_b={"side_effect": _record_then_edit("b")},
        resource_c={"side_effect": _record_then_succeed},
    )
    res = _run_failover_task(repo, resolver, max_attempts=3)

    assert res.final_state == "complete"
    # Each provider started from the pristine baseline, never a predecessor's edit.
    assert observed == ["def run():\n    return True\n"] * 3


def test_every_attempt_keeps_its_own_evidence(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _three_hop_resolver(
        resource_a={"side_effect": _edit_feature_to_false},
        resource_b={"side_effect": _edit_feature_to_false},
    )
    res = _run_failover_task(repo, resolver, max_attempts=3)

    attempts_dir = Path(res.run_dir) / "implementation" / "attempts"
    assert sorted(d.name for d in attempts_dir.iterdir()) == [
        "01-resource_a", "02-resource_b", "03-resource_c",
    ]
    for name in ("01-resource_a", "02-resource_b"):
        assert (attempts_dir / name / "attempt_record.json").exists()
        assert (attempts_dir / name / "diff.patch").exists()
        assert (attempts_dir / name / "partial_work.patch").exists()


def test_pre_existing_work_not_attributed_across_three_attempts(tmp_path: Path):
    """Mirrors the SLOPFIX-03 caveat: the repo was already dirty beforehand."""
    repo = _init_test_repo(tmp_path / "repo")
    pre_existing = repo / "src" / "untouched.py"
    pre_existing.write_text("PRE = 'existing'\n", encoding="utf-8")
    commit_all(repo, "pre-existing")
    pre_existing.write_text("PRE = 'modified by user'\n", encoding="utf-8")

    resolver = _three_hop_resolver(
        resource_a={"side_effect": _edit_feature_to_false},
        resource_b={"side_effect": _edit_feature_to_false},
    )
    res = _run_failover_task(repo, resolver, max_attempts=3)

    assert res.final_state == "complete"
    for attempt in res.implementation_attempts:
        assert "src/untouched.py" not in attempt["delta"]["files_modified"]
    # The user's own edit survives all three attempts and both rollbacks.
    assert pre_existing.read_text(encoding="utf-8") == "PRE = 'modified by user'\n"


def test_third_provider_identity_and_reviewers_recomputed(tmp_path: Path):
    """Reviewer independence is recomputed against the provider that actually ran."""
    registry = AgentRegistry([
        _profile("resource_a", "Resource A", "provider_x"),
        _profile("resource_b", "Resource B", "provider_y"),
        _profile("resource_c", "Resource C", "provider_z"),
        # Never used for implementation (resource_c outranks it), so it stays
        # healthy and can serve as a genuinely independent reviewer.
        _profile("resource_d", "Resource D", "provider_w"),
    ])
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_failover_task(
        repo, _three_hop_resolver(), max_attempts=3, registry=registry,
    )

    assert res.executing_provider == "resource_c"
    assert res.task_spec.actual_agent == "resource_c"
    mapping = res.routing_decision.metadata["reviewer_resource_mapping"]
    assert mapping
    # Recomputed for resource_c: the implementer never reviews its own work.
    assert "resource_c" not in mapping.values()
    assert res.routing_decision.metadata["review_diversity_achieved"] is True


# ---------------------------------------------------------------------------
# Attempt-bound accounting: exact, with no off-by-one at either edge
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("max_attempts", [1, 2, 3])
def test_attempt_bound_is_exact(tmp_path: Path, max_attempts: int):
    res = _run_all_fail(tmp_path, max_attempts=max_attempts)

    assert res.final_state == "failed"
    assert res.failure_class == FAILURE_CLASS_PROVIDER_EXHAUSTED
    assert len(res.implementation_attempts) == max_attempts
    assert res.failover_summary["attempts_used"] == max_attempts
    assert res.failover_summary["attempts_allowed"] == max_attempts


def test_exhaustion_reason_recorded_when_bound_reached(tmp_path: Path):
    res = _run_all_fail(tmp_path, max_attempts=2)

    summary = res.failover_summary
    assert summary["termination_reason"] == "max_attempts_reached"
    assert [entry["resource_id"] for entry in summary["attempted_resources"]] == [
        "resource_a", "resource_b",
    ]
    assert all(
        entry["failure_class"] == ProviderFailureClass.TRANSPORT_UNAVAILABLE.value
        for entry in summary["attempted_resources"]
    )


def test_exhaustion_reason_recorded_when_pool_runs_out(tmp_path: Path):
    """Only two resources exist, so a third attempt has nowhere to go."""
    registry = AgentRegistry([
        _profile("resource_a", "Resource A", "provider_x"),
        _profile("resource_b", "Resource B", "provider_y"),
    ])
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_failover_task(
        repo,
        _all_fail_resolver("resource_a", "resource_b"),
        max_attempts=3,
        registry=registry,
    )

    summary = res.failover_summary
    assert res.failure_class == FAILURE_CLASS_PROVIDER_EXHAUSTED
    assert summary["termination_reason"] == "no_eligible_resource"
    assert summary["attempts_used"] == 2
    assert summary["attempts_allowed"] == 3
    assert summary["remaining_eligible"] == []
    # Both are truthfully reported; capacity is the reason observed first.
    assert summary["excluded"]["resource_a"] == "UNREACHABLE"
    assert summary["excluded"]["resource_b"] == "UNREACHABLE"


def test_policy_filtered_resource_reported_truthfully(tmp_path: Path):
    """A paid-API resource is excluded for its real reason, not silently dropped."""
    registry = _make_registry([
        _profile("paid_api", "Paid API", "provider_z",
                 interface="api", cost_class="paid_api"),
    ])
    res = _run_all_fail(tmp_path, max_attempts=3, registry=registry)

    excluded = res.failover_summary["excluded"]
    assert excluded["paid_api"] == "PAID_API_FORBIDDEN"
    assert all(a["resource_id"] != "paid_api" for a in res.implementation_attempts)


def test_capacity_before_precedes_the_attempt_it_describes(tmp_path: Path):
    """capacity_before must be the pre-attempt state, not a post-hoc copy."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_failover_task(repo, _timeout_then_success_resolver())

    first = res.implementation_attempts[0]
    assert first["capacity_before"]["resource_a"] != "UNREACHABLE"
    assert first["capacity_after"]["resource_a"] == "UNREACHABLE"


def test_summary_reports_attempts_and_termination(tmp_path: Path):
    """The operator-facing summary explains exhaustion without guesswork."""
    from src.control_plane.launcher import _print_failover_accounting
    import io
    import contextlib

    res = _run_all_fail(tmp_path, max_attempts=2)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _print_failover_accounting(res)
    output = buffer.getvalue()

    assert "Implementation attempts:" in output
    assert "1. resource_a     TRANSPORT_UNAVAILABLE" in output
    assert "2. resource_b     TRANSPORT_UNAVAILABLE" in output
    assert "Attempts used:                2/2" in output
    assert "Termination reason:           max_attempts_reached" in output
    assert "resource_c" in output


def _recover_now(pool: ProviderPoolManager, resource_id: str) -> None:
    """Back-dates a resource's cooldown so the pool treats it as healthy again."""
    state = pool.get_resource_status(resource_id)
    state.retry_after = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()


def test_attempt_exclusion_is_task_local_not_global(tmp_path: Path):
    """A resource spent by one task is still offered to the next one.

    Bounded failover must not become a back-door global suppression: resource_a
    is barred from finishing the task that already tried it, but once its
    cooldown lapses it is an ordinary candidate for any other task.
    """
    pool_holder: Dict[str, Any] = {}
    first = _run_failover_task(
        _init_test_repo(tmp_path / "repo_one"),
        _three_hop_resolver(),
        max_attempts=3,
        pool_hook=_capture_pool(pool_holder),
    )
    _assert_completed_on_third_hop(first)

    pool = pool_holder["pool"]
    _recover_now(pool, "resource_a")
    decision = pool.select_resource(_make_task("TEST-FAILOVER-02"), role="implementation")

    # A different task carries no attempt history, so nothing bars resource_a.
    assert "resource_a" in [
        identity.resource_id for identity in decision.eligible_resources
    ]
    assert "ALREADY_ATTEMPTED" not in [
        exclusion.reason for exclusion in decision.exclusions
    ]

    second = _run_failover_task(
        _init_test_repo(tmp_path / "repo_two"),
        _three_hop_resolver(),
        task=_make_task("TEST-FAILOVER-02"),
        max_attempts=3,
        pool=pool,
    )
    assert second.final_state == "complete"


def test_progress_narrates_both_failover_hops(tmp_path: Path):
    """Human progress output names every hop, so a two-hop run is never silent."""
    repo = _init_test_repo(tmp_path / "repo")
    stderr, res = _capture_stderr(
        lambda: _run_failover_task(repo, _three_hop_resolver(), max_attempts=3)
    )

    _assert_completed_on_third_hop(res)
    transport = ProviderFailureClass.TRANSPORT_UNAVAILABLE.value
    assert f"FAILOVER | resource_a -> resource_b | {transport}" in stderr
    assert f"FAILOVER | resource_b -> resource_c | {transport}" in stderr
    for resource_id in ("resource_a", "resource_b", "resource_c"):
        assert stderr.count(f"IMPLEMENTING | {resource_id} | started") == 1
    # Only the two abandoned attempts are reported as failures.
    assert stderr.count("IMPLEMENTATION FAILED") == 2
    assert "IMPLEMENTATION FAILED | resource_c" not in stderr
    # Downstream phases name the provider that actually delivered.
    assert "REVIEWING | resource_c" in stderr


def test_verification_survives_two_hops_and_runs_only_after_success(tmp_path: Path):
    """The plan is carried across both hops and executed once, after resource_c."""
    repo = _init_test_repo(tmp_path / "repo")
    stderr, res = _capture_stderr(
        lambda: _run_failover_task(repo, _three_hop_resolver(), max_attempts=3)
    )

    _assert_completed_on_third_hop(res)
    assert res.verification_plan is not None
    assert len(res.verification_plan.steps) > 0
    assert res.verification_plan.overall_status == "passed"
    # Nothing was verified while providers were still being tried.
    assert stderr.index("VERIFYING") > stderr.index("IMPLEMENTING | resource_c")
    assert [attempt["success"] for attempt in res.implementation_attempts] == [
        False, False, True,
    ]


# ---------------------------------------------------------------------------
# HOWLFRAM-SLOPFIX-03 acceptance canary
#
# Reproduces the live run end to end with fakes: a launched provider killed by
# the harness deadline whose own transcript contains an inner "command not
# found", and a first provider whose cooldown lapses while the second one runs.
# The chain must be a -> b -> c, never a -> b -> a.
# ---------------------------------------------------------------------------
_INNER_COMMAND_NOT_FOUND = (
    "exec /usr/bin/bash -lc \"command -v jscpd\" in /repo\n"
    "/usr/bin/bash: line 1: file: command not found\n"
)


def _assert_pristine_then_edit(marker: str) -> Callable:
    """Fails the run unless the repo was rolled back before this attempt began."""
    def _side_effect(_task, cwd: Path, _prompt) -> None:
        assert _read_file(cwd, "src/feature.py") == "def run():\n    return True\n"
        (cwd / "src" / "feature.py").write_text(
            f"def run():\n    return '{marker}'\n", encoding="utf-8"
        )
    return _side_effect


def test_slopfix03_canary_walks_three_providers(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    pool_holder: Dict[str, Any] = {}

    def _b_side_effect(task, cwd: Path, prompt) -> None:
        _assert_pristine_then_edit("b")(task, cwd, prompt)
        # Attempt 1's cooldown lapses mid-attempt, as agy's did during codex.
        _recover_now(pool_holder["pool"], "resource_a")

    def _c_side_effect(task, cwd: Path, prompt) -> None:
        _assert_pristine_then_edit("c")(task, cwd, prompt)
        _edit_feature_to_true(task, cwd, prompt)

    # resource_a launched, was killed by the harness, and its transcript blames a
    # tool *it* invoked -- the exact shape that misclassified codex.
    resolver = _FakeBackendResolver({
        "resource_a": {
            "success": False,
            "stderr": _INNER_COMMAND_NOT_FOUND,
            "timed_out": True,
            "metadata": {
                LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_LAUNCHED,
                TIMEOUT_SOURCE_KEY: TIMEOUT_SOURCE_HARNESS,
            },
            "side_effect": _edit_feature_to_false,
        },
        "resource_b": {
            "success": False,
            "stderr": _TIMEOUT_STDERR,
            "side_effect": _b_side_effect,
        },
        "resource_c": {"success": True, "side_effect": _c_side_effect},
    })
    res = _run_failover_task(
        repo,
        resolver,
        max_attempts=3,
        registry=_make_registry_three_providers(),
        pool_hook=_capture_pool(pool_holder),
    )

    _assert_completed_on_third_hop(res)
    transport = ProviderFailureClass.TRANSPORT_UNAVAILABLE.value
    missing = ProviderFailureClass.MISSING_EXECUTABLE.value

    # 1. A launched provider's inner tooling never demotes it to a missing binary.
    assert res.implementation_attempts[0]["failure_class"] == transport
    assert res.implementation_attempts[0]["failure_class"] != missing
    assert res.implementation_attempts[1]["failure_class"] == transport

    # 2. resource_a was healthy and top-ranked at selection time, yet not reused.
    assert pool_holder["pool"].get_resource_status("resource_a").retry_after is None
    assert {
        exclusion["resource_id"]: exclusion["reason"]
        for exclusion in res.implementation_attempts[1]["next_selection"]["exclusions"]
    }["resource_a"] == "ALREADY_ATTEMPTED"

    # 3. Both abandoned attempts kept their evidence and were rolled back cleanly.
    attempts_dir = Path(res.run_dir) / "implementation" / "attempts"
    for name in ("01-resource_a", "02-resource_b"):
        assert (attempts_dir / name / "partial_work.patch").is_file()
    for attempt in res.implementation_attempts[:2]:
        assert attempt["rollback"] == {"restored": True, "error": None}
        assert attempt["identity"]["resource_id"] == attempt["resource_id"]

    # 4. Truthful final identity, independent review, passing verification.
    assert res.task_spec.actual_agent == "resource_c"
    mapping = res.routing_decision.metadata["reviewer_resource_mapping"]
    assert mapping and "resource_c" not in mapping.values()
    assert res.verification_plan.overall_status == "passed"
    assert res.failover_summary["termination_reason"] == "implementation_succeeded"
    assert res.failover_summary["attempts_used"] == 3

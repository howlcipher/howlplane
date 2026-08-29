#!/usr/bin/env python3
"""
tests/test_provider_failover.py

Deterministic tests for bounded provider failover during ordinary governed
implementation. These tests exercise the orchestrator's implementation-attempt
loop with fake backends and temporary Git repositories, verifying rollback,
evidence preservation, progress messaging, and final identity/reviewer behavior.
"""

from datetime import datetime, timedelta, timezone
import hashlib
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
    TOOL_PERMISSION_DENIED,
    TOOL_PERMISSION_KEY,
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
    GovernedTaskOrchestrator,
    FAILURE_CLASS_PROVIDER_EXHAUSTED,
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
    **config_overrides: Any,
) -> OrchestrationResult:
    """Runs the orchestrator with the given fake backend resolver.

    `config_overrides` are applied to the OrchestrationConfig, so a caller can
    enable real locking or inject a lifecycle failure without rebuilding the
    whole fixture.
    """
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

    config_overrides.setdefault("scratch_root", str(repo.parent / "scratch"))
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
        **config_overrides,
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
    budget = ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED.value
    missing = ProviderFailureClass.MISSING_EXECUTABLE.value

    # 1. A launched provider's inner tooling never demotes it to a missing binary.
    #    resource_a was killed by our own harness, so it reads as a budget
    #    result; resource_b's own transcript reported a transport timeout.
    assert res.implementation_attempts[0]["failure_class"] == budget
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


# ---------------------------------------------------------------------------
# HOWLFRAM-SLOPFIX-05: a provider stopped at our own budget may still have left
# a real candidate. It is governed, never trusted, and never discarded unread.
# ---------------------------------------------------------------------------
_BUDGET_KILL: Dict[str, Any] = {
    "success": False,
    "stderr": "\nTimeout after 600s.",
    "timed_out": True,
    "metadata": {
        LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_LAUNCHED,
        TIMEOUT_SOURCE_KEY: TIMEOUT_SOURCE_HARNESS,
    },
}


def _refactor_preserving_behavior(_task, cwd: Path, _prompt) -> None:
    """A real, behavior-preserving edit -- the shape of the SLOPFIX-05 patch."""
    (cwd / "src" / "feature.py").write_text(
        '"""Feature entry point."""\n\n\ndef run():\n'
        '    """Returns the feature flag."""\n    return True\n',
        encoding="utf-8",
    )


def _slopfix05_resolver(c_side_effect, **overrides) -> _FakeBackendResolver:
    """The SLOPFIX-05 chain: A availability-fails, B fails, C is budget-killed."""
    return _resolver_from_plan({
        "resource_a": {
            "success": False,
            "stderr": _TIMEOUT_STDERR,
            "side_effect": _edit_feature_to_false,
        },
        "resource_b": dict(_BUDGET_KILL),
        "resource_c": {**_BUDGET_KILL, "side_effect": c_side_effect},
    }, overrides)


def _run_slopfix05(repo: Path, c_side_effect, **overrides) -> OrchestrationResult:
    return _run_failover_task(
        repo,
        _slopfix05_resolver(c_side_effect, **overrides),
        max_attempts=3,
        registry=_make_registry_three_providers(),
    )


def test_productive_timeout_candidate_is_governed_not_discarded(tmp_path: Path):
    """The exact SLOPFIX-05 shape: the last provider is killed at our 600s budget
    after writing a correct patch. That patch used to be thrown away because the
    process exited non-zero. It must instead be captured and put through the
    same review and verification gates as any other candidate."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_slopfix05(repo, _refactor_preserving_behavior)

    # 1. Local budget expiry is not reported as a provider outage.
    attempts = res.implementation_attempts
    assert [a["resource_id"] for a in attempts] == [
        "resource_a", "resource_b", "resource_c",
    ]
    assert attempts[2]["failure_class"] == (
        ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED.value
    )
    assert attempts[2]["failure_class"] != (
        ProviderFailureClass.TRANSPORT_UNAVAILABLE.value
    )

    # 2. The candidate is captured, and captured honestly -- the provider never
    #    claimed completion and no artifact pretends otherwise.
    candidate = attempts[2]["candidate"]
    assert candidate["candidate_captured"] is True
    assert candidate["provider_completion_claim"] is False
    assert candidate["origin"] == "timed_out_implementation_attempt"
    assert candidate["requires_governance"] is True
    assert attempts[2]["success"] is False
    assert res.implementation_completion_claim is False
    assert res.candidate_origin == "timed_out_implementation_attempt"

    # 3. It reached the real gates, and only then completed.
    assert res.review_cycles, "candidate must be independently reviewed"
    assert res.verification_plan.overall_status == "passed"
    assert res.final_state == "complete"

    # 4. Reviewers stay independent of the resource that produced it.
    mapping = res.routing_decision.metadata["reviewer_resource_mapping"]
    assert mapping and "resource_c" not in mapping.values()

    # 5. The preserved candidate patch is replayable, not just readable.
    attempt_dir = Path(res.run_dir) / "implementation" / "attempts" / "03-resource_c"
    patch = attempt_dir / "candidate.patch"
    assert patch.is_file()
    assert subprocess.run(
        ["git", "apply", "--check", "--reverse", str(patch)],
        cwd=repo, capture_output=True, text=True,
    ).returncode == 0


def test_broken_timeout_candidate_is_rejected_and_rolled_back(tmp_path: Path):
    """Artifact correctness outranks provider status only through governance.
    A budget-killed provider that left a *broken* patch must not complete, and
    the rejected candidate must not be left sitting in the working tree."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_slopfix05(repo, _edit_feature_to_false)

    assert res.implementation_completion_claim is False
    assert res.final_state != "complete"
    assert res.verification_plan.overall_status != "passed"

    # The candidate was captured as evidence...
    attempt_dir = Path(res.run_dir) / "implementation" / "attempts" / "03-resource_c"
    assert (attempt_dir / "candidate.json").is_file()
    assert (attempt_dir / "candidate.patch").is_file()

    # ...but nothing stands behind it, so the repository is back at baseline.
    assert _read_file(repo, "src/feature.py") == "def run():\n    return True\n"


def test_zero_delta_timeout_leaves_no_candidate(tmp_path: Path):
    """A budget kill that produced nothing is still just a failure."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_slopfix05(repo, None)

    assert res.final_state == "failed"
    assert res.implementation_completion_claim is True
    assert res.candidate_origin is None
    attempts = res.implementation_attempts
    assert "candidate" not in attempts[2]
    assert attempts[2]["delta"]["is_empty"] is True
    # Evidence for the failed attempts is still preserved.
    attempts_dir = Path(res.run_dir) / "implementation" / "attempts"
    assert (attempts_dir / "01-resource_a" / "partial_work.patch").is_file()


def test_candidate_without_independent_reviewer_parks_for_human(tmp_path: Path):
    """A salvaged candidate has no completion claim behind it, so independent
    review is all that stands between it and the repository. When every other
    resource is unreachable the pool falls back to the implementer reviewing its
    own work -- which for this candidate is no review at all. It must park for a
    human instead of completing, and the candidate must survive for them to see."""
    repo = _init_test_repo(tmp_path / "repo")
    # A and B are genuine transport failures, so both are marked UNREACHABLE and
    # only resource_c -- the producer -- remains selectable as a reviewer.
    res = _run_failover_task(
        repo,
        _resolver_from_plan({
            "resource_a": {"success": False, "stderr": _TIMEOUT_STDERR},
            "resource_b": {"success": False, "stderr": _TIMEOUT_STDERR},
            "resource_c": {
                **_BUDGET_KILL,
                "side_effect": _refactor_preserving_behavior,
            },
        }, {}),
        max_attempts=3,
        registry=_make_registry_three_providers(),
    )

    assert res.final_state == "awaiting_human"
    assert res.implementation_completion_claim is False
    assert res.routing_decision.metadata["review_diversity_achieved"] is False
    assert (Path(res.run_dir) / "decision_packet.md").is_file()
    # The candidate is explicitly awaiting human disposition, so it stays in
    # place rather than being rolled back underneath the reviewer.
    assert "Returns the feature flag" in _read_file(repo, "src/feature.py")


# ---------------------------------------------------------------------------
# HOWLFRAM-SLOPFIX-05: a terminal failed attempt must not leave its edits behind
# ---------------------------------------------------------------------------
def test_terminal_failed_attempt_is_rolled_back(tmp_path: Path):
    """Rollback used to run only to prepare a clean tree for the *next* attempt,
    so the last attempt's edits were left in the working tree and its record
    carried no rollback key. That contaminated the next run's starting state."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_failover_task(
        repo,
        _FakeBackendResolver({
            "resource_a": {
                "success": False, "stderr": _TIMEOUT_STDERR,
                "side_effect": _edit_feature_to_false,
            },
            "resource_b": {
                "success": False, "stderr": _TIMEOUT_STDERR,
                "side_effect": _edit_feature_to_false,
            },
            "resource_c": {
                "success": False, "stderr": _TIMEOUT_STDERR,
                "side_effect": _edit_feature_to_false,
            },
        }),
        max_attempts=3,
    )

    assert res.final_state == "failed"
    assert res.failure_class == FAILURE_CLASS_PROVIDER_EXHAUSTED

    # Every attempt, including the terminal one, reports its rollback outcome.
    attempts = res.implementation_attempts
    assert len(attempts) == 3
    for attempt in attempts:
        assert attempt["rollback"] == {"restored": True, "error": None}

    # The repository is back at its pre-task baseline...
    assert _read_file(repo, "src/feature.py") == "def run():\n    return True\n"
    assert git_in_repo(repo, ["status", "--porcelain", "src"]).strip() == ""

    # ...while every attempt's evidence survives the restore, still replayable.
    attempts_dir = Path(res.run_dir) / "implementation" / "attempts"
    for name in ("01-resource_a", "02-resource_b", "03-resource_c"):
        patch = attempts_dir / name / "partial_work.patch"
        assert patch.is_file() and patch.read_text(encoding="utf-8").strip()


def test_terminal_rollback_preserves_pre_existing_user_work(tmp_path: Path):
    """Restoring the baseline must never reach past the task's own changes."""
    repo = _init_test_repo(tmp_path / "repo")
    tracked_edit = "# operator was here\ndef run():\n    return True\n"
    (repo / "src" / "feature.py").write_text(tracked_edit, encoding="utf-8")
    untracked = repo / "operator_notes.md"
    untracked.write_text("scratch notes\n", encoding="utf-8")

    res = _run_failover_task(
        repo,
        _FakeBackendResolver({
            resource: {
                "success": False, "stderr": _TIMEOUT_STDERR,
                "side_effect": _edit_feature_to_false,
            }
            for resource in ("resource_a", "resource_b", "resource_c")
        }),
        max_attempts=3,
    )

    assert res.final_state == "failed"
    # Both the pre-existing tracked modification and the untracked file survive
    # byte-for-byte, even though the task overwrote the same file.
    assert _read_file(repo, "src/feature.py") == tracked_edit
    assert untracked.read_text(encoding="utf-8") == "scratch notes\n"


def test_route_evidence_tracks_every_handoff_to_terminal_failure(tmp_path: Path):
    """A -> B -> C -> terminal failure. Routing evidence used to become durable
    only after a *successful* failover, so a run like this left route.json still
    naming A with no effective_route.json at all -- on-disk evidence flatly
    contradicting the run. The chain must now be readable without the ledger."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_failover_task(
        repo,
        _FakeBackendResolver({
            resource: {"success": False, "stderr": _TIMEOUT_STDERR}
            for resource in ("resource_a", "resource_b", "resource_c")
        }),
        max_attempts=3,
        registry=_make_registry_three_providers(),
    )

    assert res.final_state == "failed"
    run_dir = Path(res.run_dir)
    initial = json.loads((run_dir / "initial_route.json").read_text(encoding="utf-8"))
    effective = json.loads((run_dir / "effective_route.json").read_text(encoding="utf-8"))

    # The initial route is immutable and still names who was routed first.
    assert initial["selected_agent_id"] == "resource_a"

    # The effective route names the last resource actually attempted...
    meta = effective["metadata"]
    assert effective["selected_agent_id"] == "resource_c"
    assert meta["last_attempted_implementation_resource"] == "resource_c"
    assert meta["route_status"] == "IMPLEMENTATION_FAILED"
    assert meta["initial_route"]["selected_agent_id"] == "resource_a"

    # ...and does not claim anything was accepted, because nothing was.
    assert meta["accepted_implementation_resource"] is None
    assert meta["final_implementation_resource"] is None
    assert meta["reviewer_mapping_status"] == "PROVISIONAL"

    # No artifact may still imply resource_a was the active implementer.
    assert json.loads(
        (run_dir / "route.json").read_text(encoding="utf-8")
    )["metadata"]["current_attempt_resource"] == "resource_c"


def test_route_evidence_is_persisted_at_each_intermediate_handoff(tmp_path: Path):
    """Routing becomes durable when implementation ownership moves, not only
    once the provider it moved to succeeds."""
    repo = _init_test_repo(tmp_path / "repo")
    observed: List[str] = []

    def _record_route(_task, cwd: Path, _prompt) -> None:
        route = Path(cwd) / ".task_runs" / "TEST-FAILOVER-01" / "effective_route.json"
        observed.append(
            json.loads(route.read_text(encoding="utf-8"))["selected_agent_id"]
            if route.is_file() else "<absent>"
        )

    _run_failover_task(
        repo,
        _resolver_from_plan({
            "resource_a": {"success": False, "stderr": _TIMEOUT_STDERR,
                           "side_effect": _record_route},
            "resource_b": {"success": False, "stderr": _TIMEOUT_STDERR,
                           "side_effect": _record_route},
            "resource_c": {"success": True, "side_effect": _record_route},
        }, {}),
        max_attempts=3,
        registry=_make_registry_three_providers(),
    )

    # A runs before any handoff, so no effective route exists yet; B and C each
    # see themselves recorded as the current implementer before they start.
    assert observed == ["<absent>", "resource_b", "resource_c"]


def test_provider_scratch_is_relocated_out_of_the_evidence_root(tmp_path: Path):
    """A provider once wrote wip-refactor.patch straight into the run's evidence
    root, blurring which files the control plane owns. Scratch is now named in
    the prompt and anything left at the root is relocated with its provenance,
    never deleted (HOWLFRAM-SLOPFIX-05). Scratch lives outside
    implementation/attempts/ so it can never imitate an attempt
    (HOWLFRAM-SLOPFIX-06)."""
    repo = _init_test_repo(tmp_path / "repo")

    def _write_scratch_into_evidence_root(task, cwd: Path, _prompt) -> None:
        run_dir = Path(cwd) / ".task_runs" / task.task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "wip-refactor.patch").write_text("scratch\n", encoding="utf-8")
        _edit_feature_to_true(task, cwd, _prompt)

    res = _run_failover_task(
        repo,
        _FakeBackendResolver({
            "resource_a": {
                "success": True, "side_effect": _write_scratch_into_evidence_root,
            },
        }),
        max_attempts=3,
    )

    assert res.final_state == "complete"
    run_dir = Path(res.run_dir)
    manifest = json.loads((run_dir / "scratch_manifest.json").read_text(encoding="utf-8"))
    workspace = Path(manifest["attempts"]["01-resource_a"]["scratch_path"])

    # The stray file left the evidence root...
    assert not (run_dir / "wip-refactor.patch").exists()
    # ...and provider scratch is not placed inside run_dir / target repo
    assert not (run_dir / "provider_scratch").exists()
    # ...and is retained under the attempt's owned external scratch, attributed.
    relocated = workspace / "wip-refactor.patch"
    assert relocated.is_file()
    assert relocated.read_text(encoding="utf-8") == "scratch\n"

    provenance = json.loads((workspace / "_provenance.json").read_text(encoding="utf-8"))
    assert provenance["origin"] == "provider_scratch"
    assert provenance["created_by"] == "resource_a"
    assert provenance["files"] == ["wip-refactor.patch"]

    # Control-plane evidence at the root is untouched by the sweep.
    assert (run_dir / "route.json").is_file()
    assert (run_dir / "task.yaml").is_file()


def test_implementation_prompt_names_the_provider_scratch_path(tmp_path: Path):
    """Providers can only respect a boundary they are told about."""
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": True, "side_effect": _edit_feature_to_true},
    })
    _run_failover_task(repo, resolver, max_attempts=3)

    prompt = resolver.calls["resource_a"][0]["prompt"]
    assert "## Workspace" in prompt
    assert "provider_scratch/<NN-resource>/" in prompt
    # The canonical evidence namespace is never offered as a scratch location.
    assert "attempts/<NN-resource>/workspace/" not in prompt
    assert "control-plane evidence" in prompt


# ---------------------------------------------------------------------------
# HOWLFRAM-SLOPFIX-07R: a productive timeout in the middle of the failover
# chain is rolled back out of the working tree, but never forgotten.
#
# The canary chain was agy (transport, empty) -> codex (budget kill, non-empty)
# -> claude (session limit, empty). Codex's eligible artifact was rolled back
# correctly and then dropped, because salvage was only ever evaluated for
# whichever attempt happened to be last. Rollback must not mean forgetting.
# ---------------------------------------------------------------------------
_SESSION_LIMIT_STDERR = "Error: usage limit reached\n"
_PERMISSION_DENIED = {
    "success": False,
    "stderr": "Error: permission denied\n",
    "metadata": {TOOL_PERMISSION_KEY: TOOL_PERMISSION_DENIED},
}


def _make_registry_with_spare_reviewer() -> AgentRegistry:
    """Four distinct providers: three get attempted, one stays free to review.

    Independent review of a promoted fallback is only observable when some
    resource that never implemented is still selectable as a reviewer.
    """
    return AgentRegistry([
        _profile("resource_a", "Resource A", "provider_x"),
        _profile("resource_b", "Resource B", "provider_y"),
        _profile("resource_c", "Resource C", "provider_z"),
        _profile("resource_d", "Resource D", "provider_w"),
    ])


def _budget_kill(side_effect=None) -> Dict[str, Any]:
    """A harness-killed attempt, optionally leaving `side_effect`'s work behind."""
    behavior = dict(_BUDGET_KILL)
    if side_effect is not None:
        behavior["side_effect"] = side_effect
    return behavior


def _attempt_record(res: OrchestrationResult, attempt_dir_name: str) -> Dict[str, Any]:
    """Reads one attempt's durable record from the run evidence."""
    record = (
        Path(res.run_dir) / "implementation" / "attempts"
        / attempt_dir_name / "attempt_record.json"
    )
    return json.loads(record.read_text(encoding="utf-8"))


def _retained(res: OrchestrationResult, attempt_dir_name: str) -> Dict[str, Any]:
    """Returns the retained-salvage block recorded on an attempt."""
    return _attempt_record(res, attempt_dir_name).get("retained_salvage") or {}


def _run_salvage_chain(
    repo: Path,
    a: Optional[Dict[str, Any]] = None,
    b: Optional[Dict[str, Any]] = None,
    c: Optional[Dict[str, Any]] = None,
    registry: Optional[AgentRegistry] = None,
    **config_overrides: Any,
) -> OrchestrationResult:
    """Runs a bounded three-hop chain, defaulting to barren later attempts.

    Every retention scenario is this same shape with one or two behaviors
    swapped, so they are expressed as arguments rather than repeated plans.
    """
    return _run_failover_task(
        repo,
        _resolver_from_plan({
            "resource_a": a or _budget_kill(_refactor_preserving_behavior),
            "resource_b": b or {"success": False, "stderr": _TIMEOUT_STDERR},
            "resource_c": c or {"success": False, "stderr": _SESSION_LIMIT_STDERR},
        }, {}),
        max_attempts=3,
        registry=registry or _make_registry_with_spare_reviewer(),
        **config_overrides,
    )


def _run_slopfix07r(repo: Path, **config_overrides) -> OrchestrationResult:
    """The exact SLOPFIX-07R chain: transport, productive timeout, session limit."""
    return _run_salvage_chain(
        repo,
        a={"success": False, "stderr": _TIMEOUT_STDERR},
        b=_budget_kill(_refactor_preserving_behavior),
        **config_overrides,
    )


def test_case1_retained_artifact_yields_to_a_later_success(tmp_path: Path):
    """Retention must never outrank a provider-attested result. A fresh provider
    producing complete work is strictly better than governing a fragment, so the
    fragment stays historical evidence and is never promoted."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_failover_task(
        repo,
        _resolver_from_plan({
            "resource_a": _budget_kill(_edit_feature_to_false),
            "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
        }, {}),
        max_attempts=3,
    )

    assert res.final_state == "complete"
    assert res.executing_provider == "resource_b"

    # A was retained, and rolled back before B ever ran: both are true at once.
    retained = _retained(res, "01-resource_a")
    assert retained["retained"] is True
    assert retained["eligibility"] == "ELIGIBLE"
    assert retained["replayable"] is True
    # Provider-attested work outranks the fragment, so the fallback is retired
    # rather than left looking like a live option for a later resume.
    assert retained["promotion_status"] == "SUPERSEDED", "A must never be promoted"
    assert GovernedTaskOrchestrator._select_retained_salvage(
        Path(res.run_dir) / "implementation" / "attempts"
    ) is None
    assert retained["provider_completion_claim"] is False
    assert _attempt_record(res, "01-resource_a")["rollback"]["restored"] is True

    # Retention is not candidacy: no candidate artifact was ever created for A,
    # and B's accepted work does not contain A's edit.
    attempt_dir = Path(res.run_dir) / "implementation" / "attempts" / "01-resource_a"
    assert not (attempt_dir / "candidate.json").exists()
    assert _read_file(repo, "src/feature.py") == "def run():\n    return True\n"


def test_slopfix07r_forgotten_middle_artifact_is_recovered(tmp_path: Path):
    """The exact canary regression. Codex's eligible artifact survived attempt 3
    and reached governance, without inventing a fourth implementation attempt."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_slopfix07r(repo)

    # 1. The bounded budget is untouched: still exactly three attempts, and the
    #    producer was never re-invoked to claim completion.
    attempts = res.implementation_attempts
    assert [a["resource_id"] for a in attempts] == [
        "resource_a", "resource_b", "resource_c",
    ]
    assert len(attempts) == 3
    assert attempts[1]["failure_class"] == (
        ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED.value
    )
    assert attempts[2]["failure_class"] == ProviderFailureClass.SESSION_LIMIT.value
    assert attempts[2]["delta"]["is_empty"] is True

    # 2. B was retained across C, and C was rolled back. Both attempts report
    #    an honest rollback rather than going silent about it.
    retained = _retained(res, "02-resource_b")
    assert retained["promotion_status"] == "PROMOTED"
    assert retained["provider_completion_claim"] is False
    assert _attempt_record(res, "02-resource_b")["rollback"]["restored"] is True
    assert _attempt_record(res, "03-resource_c")["rollback"]["restored"] is True

    # 3. The candidate is B's, entered governance, and claims nothing.
    assert res.implementation_completion_claim is False
    assert res.candidate_origin == "timed_out_implementation_attempt"
    assert res.review_cycles, "the promoted fallback must be reviewed"
    candidate_file = (
        Path(res.run_dir) / "implementation" / "attempts"
        / "02-resource_b" / "candidate.json"
    )
    assert json.loads(candidate_file.read_text(encoding="utf-8"))[
        "resource_id"
    ] == "resource_b"

    # 4. B's work is what actually landed in the tree.
    assert "Returns the feature flag" in _read_file(repo, "src/feature.py")


def test_slopfix07r_routing_credits_producer_without_rewriting_history(
    tmp_path: Path,
):
    """Promotion names B as the candidate, but C is still the resource the
    failover chain actually ended on. Only acceptance may name an accepted
    implementer, and a fallback under review is not an accepted implementer."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_slopfix07r(repo)

    meta = json.loads(
        (Path(res.run_dir) / "effective_route.json").read_text(encoding="utf-8")
    )["metadata"]
    assert meta["candidate_resource"] == "resource_b"
    assert meta["last_attempted_implementation_resource"] == "resource_c"
    assert meta["current_attempt_resource"] == "resource_c"
    # The run completed, so acceptance has been reached -- and only acceptance
    # may name an accepted implementer.
    assert meta["reviewer_mapping_status"] == "CONFIRMED"
    assert meta["accepted_implementation_resource"] == "resource_b"

    # Reviewer independence is recomputed against the producer, not the last
    # resource that ran, so the producer cannot review its own artifact.
    assert "resource_b" not in meta["reviewer_resource_mapping"].values()


def test_case3_most_recent_eligible_retained_artifact_wins(tmp_path: Path):
    """The selection rule is deterministic and backward-looking: the most recent
    eligible artifact is promoted and older ones stay evidence only. No scoring,
    no ranking, no model judgment."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_salvage_chain(
        repo,
        a=_budget_kill(_edit_feature_to_false),
        b=_budget_kill(_refactor_preserving_behavior),
    )

    assert _retained(res, "01-resource_a")["promotion_status"] == "RETAINED"
    assert _retained(res, "02-resource_b")["promotion_status"] == "PROMOTED"
    # Only one retained artifact is ever governed, so candidate governance can
    # never become a second, unbounded failover loop.
    assert res.candidate_origin == "timed_out_implementation_attempt"
    assert "Returns the feature flag" in _read_file(repo, "src/feature.py")


def test_case4_permission_denied_artifact_gains_no_timeout_semantics(
    tmp_path: Path,
):
    """A diff is not a candidate. Only a budget kill -- a provider this control
    plane stopped at its own deadline -- yields a salvageable artifact, so a
    permission-denied attempt gains nothing merely by having touched files."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_salvage_chain(
        repo,
        b={**_PERMISSION_DENIED, "side_effect": _edit_feature_to_false},
    )

    assert _retained(res, "02-resource_b") == {}, (
        "EXECUTION_PERMISSION_REQUIRED must not be retained as salvage"
    )
    # A therefore remains the eligible fallback.
    assert _retained(res, "01-resource_a")["promotion_status"] == "PROMOTED"


@pytest.mark.parametrize(
    "work, expect_retained",
    [(None, False), (_refactor_preserving_behavior, True)],
    ids=["empty_delta", "non_empty_delta"],
)
def test_case5_retention_requires_a_non_empty_delta(
    tmp_path: Path, work, expect_retained: bool,
):
    """A budget kill that produced nothing is still just a failure. Only the
    non-empty case is salvageable, so the two branches are asserted together --
    the negative alone would hold just as well if retention did not exist."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_salvage_chain(repo, a=_budget_kill(work))

    assert bool(_retained(res, "01-resource_a")) is expect_retained
    if expect_retained:
        assert res.candidate_origin == "timed_out_implementation_attempt"
        return

    assert res.final_state == "failed"
    assert res.failure_class == FAILURE_CLASS_PROVIDER_EXHAUSTED
    assert res.candidate_origin is None
    assert _read_file(repo, "src/feature.py") == "def run():\n    return True\n"


def _corrupt_retained_patch(keep_digest_valid: bool, fired: List[str]):
    """Makes a retained artifact unusable between retention and promotion."""
    def _hook(stage: str, run_dir: Path, _spec) -> None:
        if stage != "after_salvage_retention":
            return
        fired.append(stage)
        record_file = (
            run_dir / "implementation" / "attempts"
            / "01-resource_a" / "attempt_record.json"
        )
        record = json.loads(record_file.read_text(encoding="utf-8"))
        retained = record["retained_salvage"]
        patch_file = run_dir / retained["patch_path"]
        # A well-formed diff against content that is not in the baseline: it
        # passes every identity check and still cannot be replayed.
        unapplicable = (
            "diff --git a/src/feature.py b/src/feature.py\n"
            "--- a/src/feature.py\n"
            "+++ b/src/feature.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-this line is not in the baseline\n"
            "+replacement\n"
        )
        patch_file.write_text(unapplicable, encoding="utf-8")
        if keep_digest_valid:
            retained["patch_sha256"] = hashlib.sha256(
                unapplicable.encode("utf-8")
            ).hexdigest()
        record_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return _hook


@pytest.mark.parametrize("keep_digest_valid", [False, True])
def test_case6_unusable_retained_artifact_fails_closed(
    tmp_path: Path, keep_digest_valid: bool,
):
    """A fallback that cannot be proven to belong to this baseline is left as
    evidence rather than applied. Whether it fails the digest check or the
    applicability check, nothing is partially applied and nothing is accepted."""
    repo = _init_test_repo(tmp_path / "repo")
    fired: List[str] = []
    res = _run_salvage_chain(
        repo,
        failure_injection_hook=_corrupt_retained_patch(keep_digest_valid, fired),
    )

    # Without retention the hook never fires and this test would pass vacuously.
    assert fired == ["after_salvage_retention"]
    # The unusable artifact is retired, so a resume cannot retry it forever.
    assert _retained(res, "01-resource_a")["eligibility"] == "UNUSABLE"
    assert res.final_state == "failed"
    assert res.candidate_origin is None
    assert res.failure_class == FAILURE_CLASS_PROVIDER_EXHAUSTED
    # The repository is untouched: no partial application survived.
    assert _read_file(repo, "src/feature.py") == "def run():\n    return True\n"
    assert not (
        Path(res.run_dir) / "implementation" / "attempts"
        / "01-resource_a" / "candidate.json"
    ).exists()


def test_case7_pre_task_user_work_survives_fallback_promotion(tmp_path: Path):
    """Restoring a fallback runs through the sanctioned baseline mechanism, which
    puts pre-existing user work back before anything is applied on top of it."""
    repo = _init_test_repo(tmp_path / "repo")
    tracked = repo / "src" / "other.py"
    tracked.write_text("value = 'user edit'\n", encoding="utf-8")
    untracked = repo / "scratch_notes.txt"
    untracked.write_text("do not lose me\n", encoding="utf-8")

    res = _run_salvage_chain(repo)

    assert _retained(res, "01-resource_a")["promotion_status"] == "PROMOTED"
    # Byte-for-byte, both kinds of pre-existing work survive.
    assert tracked.read_text(encoding="utf-8") == "value = 'user edit'\n"
    assert untracked.read_text(encoding="utf-8") == "do not lose me\n"
    assert res.candidate_origin == "timed_out_implementation_attempt"


def test_case8_promoted_producer_cannot_review_its_own_artifact(tmp_path: Path):
    """A promoted fallback carries no completion claim, so independent review is
    the only thing standing behind it. When the producer is the only reviewer
    left, the run parks for a human rather than completing on a self-review."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_salvage_chain(
        repo,
        c={"success": False, "stderr": _TIMEOUT_STDERR},
        registry=_make_registry_three_providers(),
    )

    assert _retained(res, "01-resource_a")["promotion_status"] == "PROMOTED"
    assert res.final_state == "awaiting_human"
    assert res.implementation_completion_claim is False
    assert res.routing_decision.metadata["review_diversity_achieved"] is False
    assert (Path(res.run_dir) / "decision_packet.md").is_file()


def test_case9_rejected_fallback_does_not_promote_an_older_one(tmp_path: Path):
    """Governance rejecting a promoted fallback is the end of it. An older
    retained artifact must not be reached for, or candidate governance would
    become a second unbounded failover loop."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_salvage_chain(repo, b=_budget_kill(_edit_feature_to_false))

    # B is the most recent eligible artifact and it is broken, so it is the one
    # governed -- and rejected. A is never reached for.
    assert _retained(res, "02-resource_b")["promotion_status"] == "PROMOTED"
    assert _retained(res, "01-resource_a")["promotion_status"] == "RETAINED"
    assert res.final_state != "complete"
    assert res.verification_plan.overall_status != "passed"
    # Under review and rejected, so nothing was ever accepted.
    meta = json.loads(
        (Path(res.run_dir) / "effective_route.json").read_text(encoding="utf-8")
    )["metadata"]
    assert meta["candidate_resource"] == "resource_b"
    assert meta["accepted_implementation_resource"] is None
    assert meta["reviewer_mapping_status"] == "CANDIDATE_REVIEW"
    # Nothing stands behind the rejected candidate, so the tree is at baseline.
    assert _read_file(repo, "src/feature.py") == "def run():\n    return True\n"


def test_case11_non_failover_eligible_terminal_recovers_the_fallback(
    tmp_path: Path,
):
    """A provider erroring outright is still a termination without a successful
    implementation, so an earlier eligible artifact is still recovered."""
    repo = _init_test_repo(tmp_path / "repo")
    res = _run_salvage_chain(
        repo, b={"success": False, "stderr": "fatal: internal error\n"},
    )

    assert res.implementation_attempts[1]["failure_class"] == "ENGINEERING_FAILURE"
    assert _retained(res, "01-resource_a")["promotion_status"] == "PROMOTED"
    assert res.candidate_origin == "timed_out_implementation_attempt"
    assert res.implementation_completion_claim is False

#!/usr/bin/env python3
"""
tests/test_provider_failover.py

Deterministic tests for bounded provider failover during ordinary governed
implementation. These tests exercise the orchestrator's implementation-attempt
loop with fake backends and temporary Git repositories, verifying rollback,
evidence preservation, progress messaging, and final identity/reviewer behavior.
"""

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


def _init_test_repo(tmp_path: Path) -> Path:
    """Returns a minimal Python repo with a deterministic test."""
    return init_minimal_python_repo(tmp_path)


def _make_registry(extra: Optional[List[AgentProfile]] = None) -> AgentRegistry:
    """Builds a small deterministic registry for failover tests."""
    profiles: List[AgentProfile] = [
        AgentProfile(
            agent_id="resource_a",
            name="Resource A",
            provider="provider_x",
            interface="headless_cli",
            capabilities=["code_generation", "file_editing", "code_review"],
            reasoning_tier="tier_2",
            supports_repository_access=True,
            cost_class="subscription_included",
        ),
        AgentProfile(
            agent_id="resource_b",
            name="Resource B",
            provider="provider_y",
            interface="headless_cli",
            capabilities=["code_generation", "file_editing", "code_review"],
            reasoning_tier="tier_2",
            supports_repository_access=True,
            cost_class="subscription_included",
        ),
        AgentProfile(
            agent_id="resource_c",
            name="Resource C",
            provider="provider_y",
            interface="headless_cli",
            capabilities=["code_generation", "file_editing", "code_review"],
            reasoning_tier="tier_2",
            supports_repository_access=True,
            cost_class="subscription_included",
        ),
    ]
    if extra:
        profiles.extend(extra)
    return AgentRegistry(profiles)


def _make_registry_three_providers() -> AgentRegistry:
    """Registry where every resource has a different provider, enabling independent review."""
    return AgentRegistry([
        AgentProfile(
            agent_id="resource_a",
            name="Resource A",
            provider="provider_x",
            interface="headless_cli",
            capabilities=["code_generation", "file_editing", "code_review"],
            reasoning_tier="tier_2",
            supports_repository_access=True,
            cost_class="subscription_included",
        ),
        AgentProfile(
            agent_id="resource_b",
            name="Resource B",
            provider="provider_y",
            interface="headless_cli",
            capabilities=["code_generation", "file_editing", "code_review"],
            reasoning_tier="tier_2",
            supports_repository_access=True,
            cost_class="subscription_included",
        ),
        AgentProfile(
            agent_id="resource_c",
            name="Resource C",
            provider="provider_z",
            interface="headless_cli",
            capabilities=["code_generation", "file_editing", "code_review"],
            reasoning_tier="tier_2",
            supports_repository_access=True,
            cost_class="subscription_included",
        ),
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
) -> OrchestrationResult:
    """Runs the orchestrator with the given fake backend resolver."""
    task = task or _make_task()
    registry = registry or _make_registry()
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
    resolver = _FakeBackendResolver({
        "resource_a": {
            "success": False,
            "stderr": "Error: timeout waiting for response\n",
        },
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
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

    resolver = _FakeBackendResolver({
        "resource_a": {
            "success": False,
            "stderr": "Error: timeout waiting for response\n",
            "side_effect": a_side_effect,
        },
        "resource_b": {
            "success": True,
            "side_effect": _edit_feature_to_true,
        },
    })
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

    resolver = _FakeBackendResolver({
        "resource_a": {
            "success": False,
            "stderr": "Error: timeout waiting for response\n",
            "side_effect": a_side_effect,
        },
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
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

    resolver = _FakeBackendResolver({
        "resource_a": {
            "success": False,
            "stderr": "Error: timeout waiting for response\n",
            "side_effect": a_side_effect,
        },
        "resource_b": {"success": True, "side_effect": b_side_effect},
    })
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

    resolver = _FakeBackendResolver({
        "resource_a": {
            "success": False,
            "stderr": "Error: timeout waiting for response\n",
        },
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
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

    resolver = _FakeBackendResolver({
        "resource_a": {
            "success": False,
            "stderr": "Error: timeout waiting for response\n",
        },
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
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
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
    res = _run_failover_task(repo, resolver)

    assert res.final_state == "complete"
    attempts = res.implementation_attempts
    assert [a["resource_id"] for a in attempts] == ["resource_a", "resource_b"]


# ---------------------------------------------------------------------------
# Scenario 11: subscription/paid-API policy remains enforced
# ---------------------------------------------------------------------------
def test_paid_api_policy_remains_enforced(tmp_path: Path):
    registry = _make_registry([
        AgentProfile(
            agent_id="paid_api",
            name="Paid API",
            provider="provider_z",
            interface="api",
            capabilities=["code_generation", "file_editing", "code_review"],
            reasoning_tier="tier_2",
            supports_repository_access=True,
            cost_class="paid_api",
        ),
    ])
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
    res = _run_failover_task(repo, resolver, registry=registry)

    assert res.final_state == "complete"
    attempts = res.implementation_attempts
    assert all(a["resource_id"] != "paid_api" for a in attempts)


# ---------------------------------------------------------------------------
# Scenario 12: task state remains valid across implementation attempts
# ---------------------------------------------------------------------------
def test_task_state_valid_across_attempts(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
    task = _make_task()
    res = _run_failover_task(repo, resolver, task=task)

    assert res.final_state == "complete"
    assert task.current_state == "complete"


# ---------------------------------------------------------------------------
# Scenario 13: successful second provider becomes actual implementation identity
# ---------------------------------------------------------------------------
def test_second_provider_becomes_actual_identity(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
    res = _run_failover_task(repo, resolver)

    assert res.final_state == "complete"
    assert res.executing_provider == "resource_b"
    assert res.provider_execution is not None
    assert res.provider_execution.agent_id == "resource_b"


# ---------------------------------------------------------------------------
# Scenario 14: initial routing identity remains preserved in history
# ---------------------------------------------------------------------------
def test_initial_routing_identity_preserved(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
    res = _run_failover_task(repo, resolver)

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
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
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
        AgentProfile(
            agent_id="resource_a",
            name="Resource A",
            provider="provider_y",
            interface="headless_cli",
            capabilities=["code_generation", "file_editing", "code_review"],
            reasoning_tier="tier_2",
            supports_repository_access=True,
            cost_class="subscription_included",
        ),
        AgentProfile(
            agent_id="resource_b",
            name="Resource B",
            provider="provider_y",
            interface="headless_cli",
            capabilities=["code_generation", "file_editing", "code_review"],
            reasoning_tier="tier_2",
            supports_repository_access=True,
            cost_class="subscription_included",
        ),
    ])
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
    res = _run_failover_task(repo, resolver, registry=registry)

    assert res.final_state == "complete"
    routing = res.routing_decision
    assert routing.metadata.get("review_diversity_achieved") is False


# ---------------------------------------------------------------------------
# Scenario 17: verification plan survives failover
# ---------------------------------------------------------------------------
def test_verification_plan_survives_failover(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
    res = _run_failover_task(repo, resolver)

    assert res.final_state == "complete"
    assert res.verification_plan is not None
    assert len(res.verification_plan.steps) > 0


# ---------------------------------------------------------------------------
# Scenario 18: verification executes after successful failover
# ---------------------------------------------------------------------------
def test_verification_executes_after_successful_failover(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
    res = _run_failover_task(repo, resolver)

    assert res.final_state == "complete"
    assert res.verification_plan is not None
    assert res.verification_plan.overall_status == "passed"


# ---------------------------------------------------------------------------
# Scenario 19: progress prints IMPLEMENTATION FAILED rather than IMPLEMENTATION COMPLETE
# ---------------------------------------------------------------------------
def test_progress_prints_implementation_failed(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
    stderr, res = _capture_stderr(lambda: _run_failover_task(repo, resolver))

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
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
    stderr, res = _capture_stderr(lambda: _run_failover_task(repo, resolver))

    assert res.final_state == "complete"
    assert "FAILOVER" in stderr
    assert "resource_a -> resource_b" in stderr
    assert ProviderFailureClass.TRANSPORT_UNAVAILABLE.value in stderr


# ---------------------------------------------------------------------------
# Scenario 21: trajectory records source, target, failure class, attempts, outcome
# ---------------------------------------------------------------------------
def test_trajectory_records_failover_chain(tmp_path: Path):
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "Error: timeout waiting for response\n"},
        "resource_b": {"success": True, "side_effect": _edit_feature_to_true},
    })
    res = _run_failover_task(repo, resolver)

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

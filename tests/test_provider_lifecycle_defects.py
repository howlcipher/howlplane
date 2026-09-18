"""Tests covering provider lifecycle defects exposed by bounded canary runs:

1. Session limit cooldown persistence (session_cooldown_seconds = 14400s).
2. Role-specific permission exclusion (EXECUTION_PERMISSION_REQUIRED).
3. Reviewer candidate prioritization (independent family first, same-family, implementer last).
4. Independent review enforcement in review runner and orchestrator stage 5.
5. Bounded remediation budget ceiling (300s ceiling under bounded run_mode).
6. Chronological ordering and timestamp capture in supervisor provider attempts.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
import pytest

from src.control_plane.agent_execution import AgentExecutionResult
from src.control_plane.agent_registry import AgentProfile, AgentRegistry
from src.control_plane.orchestrator import (
    GovernedTaskOrchestrator,
    OrchestrationConfig,
    compute_remediation_timeout,
)
from src.control_plane.resource_models import (
    ProviderFailureClass,
)
from src.control_plane.review_runner import (
    ReviewRunner,
    build_reviewer_candidates,
)
from src.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderPoolManager,
)
from src.control_plane.task_spec import TaskSpec
from src.infrastructure.config_loader import ProviderPolicySettings, ProviderResourceSettings
from tests._dogfood_test_helpers import init_minimal_python_repo
from tests.test_provider_failover import _FakeBackendResolver, _profile


def _make_agent_result(
    agent_id: str,
    success: bool = True,
    stdout: str = "",
    stderr: str = "",
    error_message: Optional[str] = None,
    timed_out: bool = False,
    exit_code: int = 0,
    role: str = "implementation",
) -> AgentExecutionResult:
    return AgentExecutionResult(
        agent_id=agent_id,
        role=role,
        command="test",
        exit_code=exit_code if success else (exit_code or 1),
        stdout=stdout,
        stderr=stderr,
        duration_seconds=1.0,
        success=success,
        timed_out=timed_out,
        error_message=error_message,
    )


def test_session_limit_cooldown_uses_session_cooldown_seconds():
    """SESSION_LIMIT uses policy.session_cooldown_seconds (default 14400s) rather than 300s."""
    registry = AgentRegistry([
        _profile("gemini_cli", "Gemini CLI", "google"),
    ])
    resources = {
        profile.resource_id: ProviderResourceSettings(enabled=True)
        for profile in registry.list_resources()
    }
    policy = ProviderPolicySettings(
        cooldown_seconds=300,
        session_cooldown_seconds=14400,
    )
    pool = ProviderPoolManager(
        registry=registry,
        resources=resources,
        policy=policy,
        operating_mode="connected",
        probe_on_start=False,
    )

    now = datetime.now(timezone.utc)
    agent_res = _make_agent_result(
        "gemini_cli",
        success=False,
        stderr="You've reached your current usage limit for this model",
    )
    pool.record_result("gemini_cli", agent_res)

    state = pool.get_resource_status("gemini_cli")
    assert state.status == ProviderAvailabilityStatus.SESSION_EXHAUSTED
    assert state.retry_after is not None
    # Cooldown should be roughly 14400 seconds (4 hours) into future, well above 300 seconds
    diff_seconds = (datetime.fromisoformat(state.retry_after) - now).total_seconds()
    assert 14300 <= diff_seconds <= 14500


def test_transient_failure_uses_transient_cooldown_seconds():
    """Transient failures still use cooldown_seconds (300s)."""
    registry = AgentRegistry([
        _profile("gemini_cli", "Gemini CLI", "google"),
    ])
    resources = {
        profile.resource_id: ProviderResourceSettings(enabled=True)
        for profile in registry.list_resources()
    }
    policy = ProviderPolicySettings(
        cooldown_seconds=300,
        session_cooldown_seconds=14400,
    )
    pool = ProviderPoolManager(
        registry=registry,
        resources=resources,
        policy=policy,
        operating_mode="connected",
        probe_on_start=False,
    )

    now = datetime.now(timezone.utc)
    agent_res = _make_agent_result(
        "gemini_cli",
        success=False,
        stderr="Connection refused by remote peer",
    )
    pool.record_result("gemini_cli", agent_res)

    state = pool.get_resource_status("gemini_cli")
    assert state.status == ProviderAvailabilityStatus.UNREACHABLE
    assert state.retry_after is not None
    diff_seconds = (datetime.fromisoformat(state.retry_after) - now).total_seconds()
    assert 250 <= diff_seconds <= 350


def test_execution_permission_required_excludes_role():
    """EXECUTION_PERMISSION_REQUIRED marks provider excluded for that role and review."""
    registry = AgentRegistry([
        _profile("gemini_cli", "Gemini CLI", "google"),
    ])
    resources = {
        profile.resource_id: ProviderResourceSettings(enabled=True)
        for profile in registry.list_resources()
    }
    pool = ProviderPoolManager(
        registry=registry,
        resources=resources,
        operating_mode="connected",
        probe_on_start=False,
    )

    agent_res = _make_agent_result(
        "gemini_cli",
        success=False,
        stderr="Action requires approval for shell execution",
        role="review",
    )
    pool.record_result("gemini_cli", agent_res, role="review")

    state = pool.get_resource_status("gemini_cli")
    assert "review" in state.role_exclusions
    assert pool.check_capacity_exclusion("gemini_cli", role="review") is not None
    assert pool.check_capacity_exclusion("gemini_cli", role="correctness-reviewer") is not None
    # But check without role or for implementation is not excluded
    assert pool.check_capacity_exclusion("gemini_cli", role="implementation") is None


def test_build_reviewer_candidates_prioritizes_independent_family():
    """build_reviewer_candidates orders independent family first, same-family next, implementer last."""
    registry = AgentRegistry([
        _profile("gemini_impl", "Gemini Implementer", "google"),
        _profile("gemini_rev", "Gemini Reviewer", "google"),
        _profile("claude_rev", "Claude Reviewer", "anthropic"),
    ])
    resources = {
        profile.resource_id: ProviderResourceSettings(enabled=True)
        for profile in registry.list_resources()
    }
    pool = ProviderPoolManager(
        registry=registry,
        resources=resources,
        operating_mode="connected",
        probe_on_start=False,
    )

    candidates = build_reviewer_candidates(
        role_id="correctness-reviewer",
        preferred="gemini_rev",
        provider_pool=pool,
        task=TaskSpec(
            task_id="T1",
            repository="test",
            objective="test",
            acceptance_criteria=[],
        ),
        implementer="gemini_impl",
    )

    # claude_rev has provider anthropic != google, so it must precede gemini_rev (google) and gemini_impl (google)
    assert candidates[0] == "claude_rev"
    assert candidates[1] == "gemini_rev"
    assert candidates[2] == "gemini_impl"


def test_build_reviewer_candidates_skips_capacity_blocked_preferred():
    """If preferred candidate is role-excluded or capacity-blocked, it is not prepended."""
    registry = AgentRegistry([
        _profile("gemini_impl", "Gemini Implementer", "google"),
        _profile("gemini_rev", "Gemini Reviewer", "google"),
        _profile("claude_rev", "Claude Reviewer", "anthropic"),
    ])
    resources = {
        profile.resource_id: ProviderResourceSettings(enabled=True)
        for profile in registry.list_resources()
    }
    pool = ProviderPoolManager(
        registry=registry,
        resources=resources,
        operating_mode="connected",
        probe_on_start=False,
    )

    # Exclude gemini_rev for review
    agent_res = _make_agent_result(
        "gemini_rev",
        success=False,
        stderr="Action requires approval for shell execution",
        role="review",
    )
    pool.record_result("gemini_rev", agent_res, role="review")

    candidates = build_reviewer_candidates(
        role_id="correctness-reviewer",
        preferred="gemini_rev",
        provider_pool=pool,
        task=TaskSpec(
            task_id="T1",
            repository="test",
            objective="test",
            acceptance_criteria=[],
        ),
        implementer="gemini_impl",
    )

    # gemini_rev must NOT be first because it is blocked for review
    assert candidates[0] == "claude_rev"


def test_independent_review_unavailable_detected_in_cycle(tmp_path: Path):
    """execute_review_cycle marks independent_review_satisfied=False when only same-family reviews."""
    registry = AgentRegistry([
        _profile("gemini_impl", "Gemini Implementer", "google"),
        _profile("gemini_rev", "Gemini Reviewer", "google"),
    ])
    resources = {
        profile.resource_id: ProviderResourceSettings(enabled=True)
        for profile in registry.list_resources()
    }
    resolver = _FakeBackendResolver({
        "gemini_impl": {"success": True},
        "gemini_rev": {"success": True, "stdout": "findings: []\n"},
    })
    pool = ProviderPoolManager(
        registry=registry,
        resources=resources,
        backend_resolver=resolver,
        operating_mode="connected",
        probe_on_start=False,
    )

    task = TaskSpec(
        task_id="T-INDEP-01",
        repository="test",
        objective="test",
        acceptance_criteria=[],
    )

    cycle_res = ReviewRunner.execute_review_cycle(
        task=task,
        diff_content="diff --git a/f.py b/f.py\n+def f(): pass",
        reviewer_roles=["correctness-reviewer"],
        cwd=tmp_path,
        reviewer_agent_mapping={"correctness-reviewer": "gemini_rev"},
        provider_pool=pool,
        implementer_resource_id="gemini_impl",
    )

    assert cycle_res.independent_review_satisfied is False
    assert cycle_res.status == "independent_review_unavailable"
    assert "correctness-reviewer" in cycle_res.non_independent_roles


def test_orchestrator_stage5_parks_when_independent_review_unavailable(tmp_path: Path):
    """When preserve_independent_review is True and independent review fails, Stage 5 parks with awaiting_human."""
    repo = init_minimal_python_repo(tmp_path / "repo")
    registry = AgentRegistry([
        _profile("gemini_impl", "Gemini Implementer", "google"),
        _profile("gemini_rev", "Gemini Reviewer", "google"),
    ])
    resources = {
        profile.resource_id: ProviderResourceSettings(enabled=True)
        for profile in registry.list_resources()
    }
    resolver = _FakeBackendResolver({
        "gemini_impl": {
            "success": True,
            "side_effect": lambda _task, cwd, _prompt: (cwd / "src" / "feature.py").write_text(
                "def run():\n    return True\n", encoding="utf-8"
            ),
        },
        "gemini_rev": {"success": True, "stdout": "findings: []\n"},
    })
    pool = ProviderPoolManager(
        registry=registry,
        resources=resources,
        backend_resolver=resolver,
        operating_mode="connected",
        probe_on_start=False,
    )

    task = TaskSpec(
        task_id="T-PARK-01",
        repository="test_repo",
        objective="Implement feature",
        acceptance_criteria=["Works"],
        reviewer_requirements=["correctness-reviewer"],
    )

    config = OrchestrationConfig(
        provider_pool=pool,
        backend_resolver=resolver,
        acquire_locks=False,
        enable_howlframe_audit=False,
        preserve_independent_review=True,
    )
    orch = GovernedTaskOrchestrator(target_repo=repo, config=config)
    res = orch.run(task)

    assert res.final_state == "awaiting_human"
    assert "independent review unavailable" in res.error_message.lower()


def test_bounded_remediation_timeout_ceiling():
    """compute_remediation_timeout caps timeout to bounded_remediation_timeout_ceiling_seconds under bounded mode."""
    config_bounded = OrchestrationConfig(
        run_mode="bounded",
        remediation_timeout_seconds=900,
        bounded_remediation_timeout_ceiling_seconds=300,
    )
    assert compute_remediation_timeout(config_bounded) == 300

    config_continuous = OrchestrationConfig(
        run_mode="continuous",
        remediation_timeout_seconds=900,
        bounded_remediation_timeout_ceiling_seconds=300,
    )
    assert compute_remediation_timeout(config_continuous) == 900


def test_supervisor_collect_provider_attempts_chronological(tmp_path: Path):
    """Supervisor._collect_provider_attempts sorts attempts chronologically and preserves timestamps."""
    from src.control_plane.factory.supervisor import FactorySupervisor

    supervisor = FactorySupervisor.__new__(FactorySupervisor)
    supervisor._state_record = MagicMock(target_repository=str(tmp_path))
    supervisor._state_dir = tmp_path

    wid = "WI-100"
    run_dir = tmp_path / ".task_runs" / wid
    run_dir.mkdir(parents=True)

    # 1. Implementation attempt 1 (failed at 10:05:00)
    impl_1 = run_dir / "implementation" / "attempts" / "01-r1"
    impl_1.mkdir(parents=True)
    (impl_1 / "result.json").write_text(
        json.dumps({
            "agent_id": "r1",
            "started_at": "2026-09-18T10:05:00+00:00",
            "timestamp": "2026-09-18T10:06:00+00:00",
            "duration_seconds": 60,
            "success": False,
            "error_message": "TRANSPORT_UNAVAILABLE",
        }),
        encoding="utf-8",
    )

    # 2. Implementation attempt 2 (succeeded at 10:06:30)
    impl_2 = run_dir / "implementation" / "attempts" / "02-r2"
    impl_2.mkdir(parents=True)
    (impl_2 / "result.json").write_text(
        json.dumps({
            "agent_id": "r2",
            "started_at": "2026-09-18T10:06:30+00:00",
            "timestamp": "2026-09-18T10:08:00+00:00",
            "duration_seconds": 90,
            "success": True,
        }),
        encoding="utf-8",
    )

    # 3. Review attempts in review cycle: r3 failed at 10:06:10, r4 succeeded at 10:08:30
    rev_dir = run_dir / "reviews" / "cycle-1"
    rev_dir.mkdir(parents=True)
    (rev_dir / "result.json").write_text(
        json.dumps({
            "failover": {
                "attempts": [
                    {
                        "resource_id": "r3",
                        "outcome": "unavailable",
                        "started_at": "2026-09-18T10:06:10+00:00",
                        "ended_at": "2026-09-18T10:06:15+00:00",
                    },
                    {
                        "resource_id": "r4",
                        "outcome": "completed",
                        "started_at": "2026-09-18T10:08:30+00:00",
                        "ended_at": "2026-09-18T10:09:00+00:00",
                    },
                ]
            }
        }),
        encoding="utf-8",
    )

    attempts = supervisor._collect_provider_attempts(wid)
    assert len(attempts) == 4
    # Check ordering:
    # 1. r1 (10:05:00)
    # 2. r3 (10:06:10)
    # 3. r2 (10:06:30)
    # 4. r4 (10:08:30)
    assert attempts[0]["resource_id"] == "r1"
    assert attempts[1]["resource_id"] == "r3"
    assert attempts[2]["resource_id"] == "r2"
    assert attempts[3]["resource_id"] == "r4"

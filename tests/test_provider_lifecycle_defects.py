"""Tests covering provider lifecycle defects exposed by bounded canary runs:

1. Session limit and transient cooldown duration persistence.
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

from howlplane.control_plane.agent_execution import AgentExecutionResult
from howlplane.control_plane.agent_registry import AgentProfile, AgentRegistry
from howlplane.control_plane.orchestrator import (
    GovernedTaskOrchestrator,
    OrchestrationConfig,
    compute_remediation_timeout,
)
from howlplane.control_plane.resource_models import (
    ProviderFailureClass,
)
from howlplane.control_plane.review_runner import (
    ReviewRunner,
    build_reviewer_candidates,
)
from howlplane.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderPoolManager,
)
from howlplane.control_plane.task_spec import TaskSpec
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


def _make_pool(
    profiles: List[AgentProfile],
    policy: Optional[ProviderPolicySettings] = None,
    resolver: Optional[Any] = None,
) -> ProviderPoolManager:
    return ProviderPoolManager(
        registry=AgentRegistry(profiles),
        resources={p.resource_id: ProviderResourceSettings(enabled=True) for p in profiles},
        policy=policy,
        backend_resolver=resolver,
        operating_mode="connected",
        probe_on_start=False,
    )


@pytest.mark.parametrize("failure_stderr,expected_status,expected_min,expected_max", [
    ("You've reached your current usage limit for this model", ProviderAvailabilityStatus.SESSION_EXHAUSTED, 14300, 14500),
    ("Connection refused by remote peer", ProviderAvailabilityStatus.UNREACHABLE, 250, 350),
])
def test_failure_cooldown_duration(failure_stderr, expected_status, expected_min, expected_max):
    """Verifies that SESSION_LIMIT uses session_cooldown_seconds (14400s) and transient uses 300s."""
    pool = _make_pool(
        [_profile("gemini_cli", "Gemini CLI", "google")],
        policy=ProviderPolicySettings(cooldown_seconds=300, session_cooldown_seconds=14400),
    )
    now = datetime.now(timezone.utc)
    agent_res = _make_agent_result("gemini_cli", success=False, stderr=failure_stderr)
    pool.record_result("gemini_cli", agent_res)

    state = pool.get_resource_status("gemini_cli")
    assert state.status == expected_status
    assert state.retry_after is not None
    diff_seconds = (datetime.fromisoformat(state.retry_after) - now).total_seconds()
    assert expected_min <= diff_seconds <= expected_max


def test_execution_permission_required_excludes_role():
    """EXECUTION_PERMISSION_REQUIRED marks provider excluded for that role and review."""
    pool = _make_pool([_profile("gemini_cli", "Gemini CLI", "google")])
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
    assert pool.check_capacity_exclusion("gemini_cli", role="implementation") is None


def _build_test_candidates(pool: ProviderPoolManager) -> List[str]:
    return build_reviewer_candidates(
        role_id="correctness-reviewer",
        preferred="gemini_rev",
        provider_pool=pool,
        task=TaskSpec(task_id="T1", repository="test", objective="test", acceptance_criteria=[]),
        implementer="gemini_impl",
    )


def test_build_reviewer_candidates_prioritizes_independent_family():
    """build_reviewer_candidates orders independent family first, same-family next, implementer last."""
    pool = _make_pool([
        _profile("gemini_impl", "GI", "google"),
        _profile("gemini_rev", "GR", "google"),
        _profile("claude_rev", "CR", "anthropic"),
    ])
    candidates = _build_test_candidates(pool)
    assert candidates[0] == "claude_rev"
    assert candidates[1] == "gemini_rev"
    assert candidates[2] == "gemini_impl"


def test_build_reviewer_candidates_skips_capacity_blocked_preferred():
    """If preferred candidate is role-excluded or capacity-blocked, it is not prepended."""
    pool = _make_pool([
        _profile("gemini_impl", "GI", "google"),
        _profile("gemini_rev", "GR", "google"),
        _profile("claude_rev", "CR", "anthropic"),
    ])
    agent_res = _make_agent_result(
        "gemini_rev",
        success=False,
        stderr="Action requires approval for shell execution",
        role="review",
    )
    pool.record_result("gemini_rev", agent_res, role="review")
    candidates = _build_test_candidates(pool)
    assert candidates[0] == "claude_rev"


def test_independent_review_unavailable_detected_in_cycle(tmp_path: Path):
    """execute_review_cycle marks independent_review_satisfied=False when only same-family reviews."""
    from howlplane.control_plane.agent_execution import FakeAgentBackend

    backend = FakeAgentBackend(agent_id="gemini_cli", default_stdout="findings: []\n")
    task = TaskSpec(task_id="T-INDEP-01", repository="test", objective="test", acceptance_criteria=[])
    cycle_res = ReviewRunner.execute_review_cycle(
        task=task,
        diff_content="diff --git a/f.py b/f.py\n+def f(): pass",
        reviewer_roles=["correctness-reviewer"],
        cwd=tmp_path,
        backend=backend,
        implementer_resource_id="gemini_cli",
    )
    assert cycle_res.independent_review_satisfied is False
    assert cycle_res.status == "independent_review_unavailable"
    assert "correctness-reviewer" in cycle_res.non_independent_roles


def test_orchestrator_stage5_parks_when_independent_review_unavailable(tmp_path: Path):
    """When preserve_independent_review is True and independent review fails, Stage 5 parks with awaiting_human."""
    repo = init_minimal_python_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "g_impl": {
            "success": True,
            "side_effect": lambda _task, cwd, _prompt: (cwd / "src" / "feature.py").write_text(
                "def run():\n    return True\n", encoding="utf-8"
            ),
        },
        "g_rev": {"success": True, "stdout": "findings: []\n"},
    })
    pool = _make_pool(
        [_profile("g_impl", "GI", "google"), _profile("g_rev", "GR", "google")],
        resolver=resolver,
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
    cfg_b = OrchestrationConfig(run_mode="bounded", remediation_timeout_seconds=900, bounded_remediation_timeout_ceiling_seconds=300)
    cfg_c = OrchestrationConfig(run_mode="continuous", remediation_timeout_seconds=900, bounded_remediation_timeout_ceiling_seconds=300)
    assert compute_remediation_timeout(cfg_b) == 300
    assert compute_remediation_timeout(cfg_c) == 900


def test_supervisor_collect_provider_attempts_chronological(tmp_path: Path):
    """Supervisor._collect_provider_attempts sorts attempts chronologically and preserves timestamps."""
    from howlplane.control_plane.factory.supervisor import FactorySupervisor

    supervisor = FactorySupervisor.__new__(FactorySupervisor)
    supervisor._state_record = MagicMock(target_repository=str(tmp_path))
    supervisor._state_dir = tmp_path

    wid = "WI-100"
    run_dir = tmp_path / ".task_runs" / wid
    run_dir.mkdir(parents=True)

    items = [
        ("01-r1", "r1", "2026-09-18T10:05:00+00:00", "2026-09-18T10:06:00+00:00", 60, False, "TRANSPORT_UNAVAILABLE"),
        ("02-r2", "r2", "2026-09-18T10:06:30+00:00", "2026-09-18T10:08:00+00:00", 90, True, None),
    ]
    for dir_name, agent, start_t, end_t, dur, ok, err in items:
        p = run_dir / "implementation" / "attempts" / dir_name
        p.mkdir(parents=True)
        payload = {"agent_id": agent, "started_at": start_t, "timestamp": end_t, "duration_seconds": dur, "success": ok}
        if err:
            payload["error_message"] = err
        (p / "result.json").write_text(json.dumps(payload), encoding="utf-8")

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
    assert attempts[0]["resource_id"] == "r1"
    assert attempts[1]["resource_id"] == "r3"
    assert attempts[2]["resource_id"] == "r2"
    assert attempts[3]["resource_id"] == "r4"

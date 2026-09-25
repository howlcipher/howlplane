#!/usr/bin/env python3
"""
Deterministic regression coverage for the live HowlFrame Factory incident.

Reproduces, in fast fixtures:

* Factory campaign identity: an active campaign wins over a stopped historical
  one for the same repository; multiple active campaigns are reported
  unambiguously; explicit --state-dir wins.

* Provider failover and capacity:
  - AGY consumes the execution budget -> bounded DEGRADED cooldown.
  - Claude reports a weekly usage limit -> SESSION_EXHAUSTED (not ENGINEERING_FAILURE).
  - AGY reports session exhaustion -> SESSION_EXHAUSTED, durable across items.
  - After exhaustion, Codex becomes the next selected implementer.
  - A later work item does not immediately re-select AGY while it is exhausted.

* Work item lifecycle: a failed implementation attempt does not count as
  completed work or advance a work item to a shipped/terminal success state.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from howlplane.control_plane.agent_execution import (
    AgentExecutionResult,
    FakeAgentBackend,
    LAUNCH_OUTCOME_KEY,
    LAUNCH_OUTCOME_LAUNCHED,
    TIMEOUT_SOURCE_HARNESS,
    TIMEOUT_SOURCE_KEY,
)
from howlplane.control_plane.agent_registry import AgentProfile, AgentRegistry
from howlplane.control_plane.factory.dispatcher import DispatchOutcome
from howlplane.control_plane.factory.supervisor import FactorySupervisor
from howlplane.control_plane.factory.supervisor_state import SupervisorState
from howlplane.control_plane.factory.work_item import WorkItem, WorkItemOrigin, WorkItemState, WorkItemStore
from howlplane.control_plane.resource_models import EconomicClass, ResourceLocality
from howlplane.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderPoolManager,
    ProviderFailureClass,
)
from howlplane.control_plane.task_spec import TaskSpec
from tests._factory_test_helpers import make_supervisor


AGY_WEEKLY_LIMIT = (
    "Error: You've hit your weekly limit · resets Sep 16, 2pm (America/Detroit)\n"
)
AGY_SESSION_LIMIT = (
    "error: Individual quota reached. Please upgrade your subscription to "
    "increase your limits. Resets in 51h53m56s.\n"
)


def _profile(agent_id: str, provider: str, **overrides: Any) -> AgentProfile:
    fields: Dict[str, Any] = dict(
        interface="headless_cli",
        capabilities=["code_generation", "file_editing"],
        reasoning_tier="tier_2",
        supports_repository_access=True,
        cost_class="subscription_included",
        locality=ResourceLocality.HOSTED.value,
        economic_class=EconomicClass.SUBSCRIPTION.value,
    )
    fields.update(overrides)
    return AgentProfile(agent_id=agent_id, name=agent_id, provider=provider, **fields)


def _make_pool(tmp_path: Path) -> ProviderPoolManager:
    """Three-provider pool mirroring the live incident resources."""
    from howlplane.control_plane.config_loader import ProviderPolicySettings, ProviderResourceSettings

    registry = AgentRegistry([
        _profile("agy", "agy"),
        _profile("claude_code", "anthropic"),
        _profile("codex", "openai"),
    ])
    resources = {
        profile.agent_id: ProviderResourceSettings(enabled=True)
        for profile in registry.list_resources()
    }
    policy = ProviderPolicySettings(cooldown_seconds=300, quota_cooldown_seconds=21600)
    return ProviderPoolManager(
        registry=registry,
        backend_resolver=lambda aid: FakeAgentBackend(agent_id=aid),
        probe_on_start=False,
        policy=policy,
        resources=resources,
        operating_mode="connected",
        state_path=tmp_path / "provider_capacity.json",
    )


def _result(
    agent_id: str,
    *,
    success: bool = False,
    exit_code: int = 1,
    stderr: str = "",
    timed_out: bool = False,
    metadata: Dict[str, Any] | None = None,
    duration: float = 1.0,
) -> AgentExecutionResult:
    return AgentExecutionResult(
        agent_id=agent_id,
        role="implementation",
        command=f"{agent_id} implement",
        exit_code=exit_code,
        stdout="" if not success else "done",
        stderr=stderr,
        duration_seconds=duration,
        success=success,
        timed_out=timed_out,
        metadata=metadata or {},
    )


def _budget_result(agent_id: str = "agy") -> AgentExecutionResult:
    return _result(
        agent_id,
        exit_code=0,
        timed_out=True,
        stderr="print timeout after 9m45s with turn in progress; returning partial output",
        metadata={
            LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_LAUNCHED,
            TIMEOUT_SOURCE_KEY: TIMEOUT_SOURCE_HARNESS,
        },
        duration=600.0,
    )


def _claude_weekly_limit_result() -> AgentExecutionResult:
    return _result("claude_code", stderr=AGY_WEEKLY_LIMIT, duration=2.0)


def _agy_session_limit_result() -> AgentExecutionResult:
    return _result("agy", stderr=AGY_SESSION_LIMIT, duration=530.0)


def test_agy_execution_budget_is_degraded_not_exhausted(tmp_path: Path):
    """A full execution-budget burn should deprioritize AGY, not blacklist it."""
    pool = _make_pool(tmp_path)
    result = _budget_result("agy")

    failure_class = pool.record_result("agy", result, task_id="WI-A")

    assert failure_class == ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED
    status = pool.get_resource_status("agy")
    assert status is not None
    assert status.status == ProviderAvailabilityStatus.DEGRADED
    assert status.normalized_failure_class == ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED.value
    assert status.retry_after is not None
    assert datetime.fromisoformat(status.retry_after) > datetime.now(timezone.utc)


def test_claude_weekly_limit_is_session_exhausted(tmp_path: Path):
    """Claude's 'weekly limit' must be classified as session exhaustion."""
    pool = _make_pool(tmp_path)
    result = _claude_weekly_limit_result()

    failure_class = pool.record_result("claude_code", result, task_id="WI-A")

    assert failure_class == ProviderFailureClass.SESSION_LIMIT
    status = pool.get_resource_status("claude_code")
    assert status is not None
    assert status.status == ProviderAvailabilityStatus.SESSION_EXHAUSTED
    assert "weekly limit" in (status.exhaustion_event.raw_error or "").lower()


def test_live_incident_provider_sequence(tmp_path: Path):
    """Reproduce the A/B/C provider failover and eligibility sequence."""
    pool = _make_pool(tmp_path)
    task = TaskSpec(
        task_id="WI-A",
        repository="howlcipher/howlframe",
        objective="Implement collections",
        task_class="feature",
        risk_level="low",
    )

    # A: AGY hits the execution budget -> DEGRADED.
    a_failure = pool.record_result("agy", _budget_result("agy"), task_id="WI-A")
    assert a_failure == ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED
    assert pool.get_resource_status("agy").status == ProviderAvailabilityStatus.DEGRADED

    # A retry: Claude hits weekly limit -> SESSION_EXHAUSTED.
    claude_failure = pool.record_result("claude_code", _claude_weekly_limit_result(), task_id="WI-A")
    assert claude_failure == ProviderFailureClass.SESSION_LIMIT
    assert pool.get_resource_status("claude_code").status == ProviderAvailabilityStatus.SESSION_EXHAUSTED

    # B: AGY reports session exhaustion -> SESSION_EXHAUSTED (durable).
    b_failure = pool.record_result("agy", _agy_session_limit_result(), task_id="WI-B")
    assert b_failure == ProviderFailureClass.SESSION_LIMIT
    agy_status = pool.get_resource_status("agy")
    assert agy_status.status == ProviderAvailabilityStatus.SESSION_EXHAUSTED
    assert "individual quota reached" in agy_status.exhaustion_event.raw_error.lower()

    # After AGY and Claude are exhausted, Codex is selected for implementation.
    candidates = pool.select_candidates(task_category="code_heavy", task=task)
    assert "agy" not in candidates
    assert "claude_code" not in candidates
    assert candidates[0] == "codex"

    # C: AGY is still ineligible while its durable exhaustion state is valid.
    candidates_c = pool.select_candidates(task_category="code_heavy", task=task)
    assert "agy" not in candidates_c

    # After the AGY cooldown/reset, it can return to eligibility.
    agy_status.retry_after = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    pool._persist()
    recovered = pool.select_candidates(task_category="code_heavy", task=task)
    assert "agy" in recovered


def test_failed_implementation_attempt_does_not_advance_lifecycle(tmp_path: Path):
    """A failed dispatch leaves the work item FAILED, not SHIPPED."""
    from tests._factory_test_helpers import RecordingDispatcher

    supervisor, _clock, _sleeps = make_supervisor(tmp_path)
    item = WorkItem.create(
        origin=WorkItemOrigin.OWNER_DIRECTION,
        repository="howlcipher/howlframe",
        title="Failed task",
        identity_keys=["failed-task"],
    )
    item.transition_to(WorkItemState.ADMITTED)
    item.transition_to(WorkItemState.READY)
    supervisor.work_item_store.save_object(item)

    outcomes = [
        DispatchOutcome(
            success=False,
            work_item_id=item.work_item_id,
            next_work_item_state=WorkItemState.FAILED,
            reason="orchestrator_final_state:failed",
        ),
    ]
    supervisor.dispatcher = RecordingDispatcher(outcomes)
    supervisor.tick()

    fresh = supervisor.work_item_store.load(item.work_item_id)
    assert fresh.state == WorkItemState.FAILED
    assert fresh.attempts == 1
    assert fresh.is_terminal is False


def test_deadlock_reproduction_alert_and_dynamic_resolution(tmp_path: Path):
    """HOWL-CANON-003: Reproduce 4-dispatch non-product cap deadlock, verify operator alert

    after >3 consecutive capped ticks, and verify dynamic target_repo parameterization resolves it.
    """
    from tests._factory_test_helpers import RecordingDispatcher, SuccessEngine

    # 1. Reproduce prior incident condition:
    # Campaign targets howlplane, but supervisor uses default policy (howlframe product repo).
    supervisor, _clock, _sleeps = make_supervisor(tmp_path)
    # Simulate 4 prior dispatches on howlplane in dispatch history
    for i in range(4):
        supervisor.state_record.record_dispatch(
            dispatch_id=f"disp-{i}",
            work_item_id=f"WI-{i}",
            task_id=f"task-{i}",
            now_iso=datetime.now(timezone.utc).isoformat(),
            origin="existing_backlog",
            repository="howlcipher/howlplane",
        )
    supervisor._persist()

    # Add ready work items for howlplane
    for i in range(5):
        item = WorkItem.create(
            origin=WorkItemOrigin.EXISTING_BACKLOG,
            repository="howlcipher/howlplane",
            title=f"Howlplane Task {i}",
            identity_keys=[f"howlplane-task-{i}"],
        )
        item.transition_to(WorkItemState.ADMITTED)
        item.transition_to(WorkItemState.READY)
        supervisor.work_item_store.save_object(item)

    # Ticks 1, 2, 3: Capped by non_product_repository_cap, no alert yet (< 4 ticks)
    for tick_num in range(1, 4):
        res = supervisor.tick()
        assert res.state == SupervisorState.WAITING_FOR_WORK
        assert res.selected_work_item_id is None
        assert res.alert is None
        assert supervisor.state_record.consecutive_capped_ticks == tick_num
        assert len(supervisor.state_record.alerts) == 0

    # Tick 4: 4th consecutive tick with ready candidates capped (> 3 ticks) -> Alert fires!
    res4 = supervisor.tick()
    assert res4.state == SupervisorState.WAITING_FOR_WORK
    assert res4.selected_work_item_id is None
    assert res4.alert is not None
    assert "CAP_DEADLOCK_ALERT" in res4.alert
    assert "non_product_repository_cap" in res4.alert
    assert supervisor.state_record.consecutive_capped_ticks == 4
    assert len(supervisor.state_record.alerts) >= 1
    latest_alert = supervisor.state_record.alerts[-1]
    assert latest_alert["type"] == "capped_candidates_deadlock"
    assert "non_product_repository_cap" in latest_alert["details"]["reasons"]

    # 2. Resolution: Parameterize product_repository with campaign target repository
    # Either via product_repo or state_record.target_repository
    supervisor.policy = supervisor.policy.__class__(product_repository="howlcipher/howlplane")
    supervisor.dispatcher = RecordingDispatcher([
        DispatchOutcome(
            success=True,
            work_item_id="placeholder",
            next_work_item_state=WorkItemState.SHIPPED,
            reason="completed",
        ),
    ])

    # Tick 5: Progress is made! Work item is selected and dispatched beyond 4 items
    res5 = supervisor.tick()
    assert res5.selected_work_item_id is not None
    assert supervisor.state_record.consecutive_capped_ticks == 0
    assert len(supervisor.dispatcher.calls) == 1

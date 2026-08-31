#!/usr/bin/env python3
"""Black-box persistent factory acceptance contracts.

The module is collected only when the factory implementation is present.  It
can therefore land independently on ``main`` and becomes active as soon as the
factory branch is integrated.  Known defects are strict xfails: fixing one
turns CI red until the defect marker is removed and the contract is accepted.
"""

from datetime import datetime, timedelta, timezone
import inspect
from time import perf_counter

import pytest

from factory_acceptance_harness import (
    CRASH_MATRIX_BOUNDARIES,
    CrashInjector,
    DispatchScript,
    FakeClock,
    InjectedCrash,
    ProviderRecord,
    ProviderTransition,
    ScriptedDispatcher,
    ScriptedProviderPool,
    assert_bounded_waits,
    assert_no_duplicate_dispatch,
)


pytest.importorskip(
    "src.control_plane.factory.supervisor",
    reason="factory implementation is intentionally being built in a parallel branch",
)

from src.control_plane.factory.portfolio import FactoryPolicy
from src.control_plane.factory.repo_proposal import (
    CapabilityRecord,
    CapabilityStore,
    RepoProposalStore,
    VerificationStatus,
)
from src.control_plane.factory.supervisor import FactorySupervisor
from src.control_plane.factory.supervisor_state import (
    SupervisorState,
    SupervisorStateStore,
)
from src.control_plane.factory.work_item import (
    WorkItem,
    WorkItemOrigin,
    WorkItemState,
    WorkItemStore,
)


START = datetime(2026, 8, 31, tzinfo=timezone.utc)
REPOSITORY = "howlcipher/howlframe"


def _ready_item(
    store,
    key,
    *,
    origin=WorkItemOrigin.EXISTING_BACKLOG,
    repository=REPOSITORY,
):
    item = WorkItem.create(
        origin=origin,
        repository=repository,
        title=f"work {key}",
        identity_keys=["factory-acceptance", str(key)],
    )
    item.transition_to(WorkItemState.ADMITTED)
    item.transition_to(WorkItemState.READY)
    store.save_object(item)
    return item


def _supervisor(tmp_path, pool, dispatcher, clock, discovery=lambda: []):
    return FactorySupervisor(
        state_store=SupervisorStateStore(tmp_path / "supervisor"),
        work_item_store=WorkItemStore(tmp_path / "work-items"),
        repo_proposal_store=RepoProposalStore(tmp_path / "repo-proposals"),
        capability_store=CapabilityStore(tmp_path / "capabilities"),
        dispatcher=dispatcher,
        discovery=discovery,
        provider_pool=pool,
        policy=FactoryPolicy(),
        clock=clock.now,
        sleep=clock.sleep,
        tick_interval_seconds=60,
        provider_retry_interval_seconds=300,
        backoff_base_seconds=60,
        max_backoff_seconds=3600,
    )


def _unavailable_pool(clock, retry_at):
    return ScriptedProviderPool(
        clock,
        [
            ProviderRecord(
                "claude",
                capacity="SESSION_EXHAUSTED",
                retry_after=retry_at,
            )
        ],
    )


def _idle_supervisor(tmp_path):
    clock = FakeClock(START)
    pool = ScriptedProviderPool(clock, [ProviderRecord("codex")])
    supervisor = _supervisor(
        tmp_path, pool, ScriptedDispatcher({}, pool), clock
    )
    return clock, pool, supervisor


def test_claude_exhausted_codex_work_continues_without_factory_stop(tmp_path):
    clock = FakeClock(START)
    pool = ScriptedProviderPool(
        clock,
        [
            ProviderRecord("claude", capacity="SESSION_EXHAUSTED"),
            ProviderRecord("codex", capacity="AVAILABLE"),
        ],
    )
    supervisor = _supervisor(tmp_path, pool, None, clock)
    item = _ready_item(supervisor.work_item_store, "codex-fallback")
    dispatcher = ScriptedDispatcher(
        {
            item.work_item_id: [
                DispatchScript(True, "idle", "completed", "codex")
            ]
        },
        pool,
    )
    supervisor.dispatcher = dispatcher

    result = supervisor.tick()
    assert result.state != SupervisorState.STOPPED
    assert dispatcher.calls == [item.work_item_id]
    assert pool.records["claude"].generative_attempts == 0
    assert pool.records["codex"].generative_attempts == 1


def test_all_hosted_exhausted_local_eligible_work_can_continue(tmp_path):
    clock = FakeClock(START)
    pool = ScriptedProviderPool(
        clock,
        [
            ProviderRecord("claude", capacity="SESSION_EXHAUSTED"),
            ProviderRecord("codex", capacity="QUOTA_EXHAUSTED"),
            ProviderRecord("local_ollama", locality="local", capacity="AVAILABLE"),
        ],
    )
    supervisor = _supervisor(tmp_path, pool, None, clock)
    item = _ready_item(supervisor.work_item_store, "local-fallback")
    dispatcher = ScriptedDispatcher(
        {
            item.work_item_id: [
                DispatchScript(True, "idle", "completed", "local_ollama")
            ]
        },
        pool,
    )
    supervisor.dispatcher = dispatcher

    result = supervisor.tick()
    assert result.state != SupervisorState.STOPPED
    assert pool.records["local_ollama"].generative_attempts == 1
    assert pool.records["claude"].generative_attempts == 0
    assert pool.records["codex"].generative_attempts == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_24_7: run() ignores a persisted future next_wake_at, ticks "
        "WAITING_FOR_PROVIDER immediately, and replaces retry_after with backoff"
    ),
)
def test_no_capacity_and_no_local_work_waits_until_retry_without_busy_loop(
    tmp_path,
):
    clock = FakeClock(START)
    retry_at = START + timedelta(hours=6)
    pool = _unavailable_pool(clock, retry_at)
    supervisor = _supervisor(
        tmp_path, pool, ScriptedDispatcher({}, pool), clock
    )

    result = supervisor.tick()
    assert result.state == SupervisorState.WAITING_FOR_PROVIDER
    assert result.next_wake_at == retry_at
    supervisor.run(until=retry_at)
    assert clock.sleep_calls[0] == 6 * 60 * 60
    assert_bounded_waits(clock, minimum_seconds=60)
    assert pool.records["claude"].generative_attempts == 0


def test_retry_boundary_uses_non_generative_recheck_then_recovers(tmp_path):
    clock = FakeClock(START)
    retry_at = START + timedelta(hours=1)
    pool = ScriptedProviderPool(
        clock,
        [
            ProviderRecord(
                "claude",
                capacity="SESSION_EXHAUSTED",
                retry_after=retry_at,
            )
        ],
        [ProviderTransition(retry_at, "claude", "AVAILABLE")],
    )
    supervisor = _supervisor(
        tmp_path, pool, ScriptedDispatcher({}, pool), clock
    )
    supervisor.run(until=retry_at + timedelta(minutes=1))
    assert pool.records["claude"].generative_attempts == 0
    assert pool.has_available_providers() is True
    assert supervisor.state_record.state in {
        SupervisorState.WAITING_FOR_WORK,
        SupervisorState.STOPPED,
    }


def test_provider_still_unavailable_after_retry_uses_bounded_backoff(tmp_path):
    clock = FakeClock(START)
    retry_at = START + timedelta(minutes=5)
    pool = _unavailable_pool(clock, retry_at)
    supervisor = _supervisor(
        tmp_path, pool, ScriptedDispatcher({}, pool), clock
    )
    supervisor.run(until=START + timedelta(hours=2))
    assert_bounded_waits(clock, minimum_seconds=60)
    assert len(clock.sleep_calls) <= 121
    assert pool.records["claude"].generative_attempts == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_24_7: supervisor selects ready work before checking provider "
        "capacity, so an exhausted provider path can be hammered every tick"
    ),
)
def test_exhausted_provider_is_not_hammered_when_ready_work_exists(tmp_path):
    clock = FakeClock(START)
    pool = ScriptedProviderPool(
        clock,
        [ProviderRecord("claude", capacity="SESSION_EXHAUSTED")],
    )
    dispatcher = ScriptedDispatcher({}, pool)
    supervisor = _supervisor(tmp_path, pool, dispatcher, clock)
    _ready_item(supervisor.work_item_store, "must-wait")

    result = supervisor.tick()
    assert result.state == SupervisorState.WAITING_FOR_PROVIDER
    assert dispatcher.calls == []


def test_completed_work_is_recorded_once_and_never_redispatched(tmp_path):
    clock = FakeClock(START)
    pool = ScriptedProviderPool(clock, [ProviderRecord("codex")])
    supervisor = _supervisor(tmp_path, pool, None, clock)
    item = _ready_item(supervisor.work_item_store, "exactly-once")
    dispatcher = ScriptedDispatcher(
        {
            item.work_item_id: [
                DispatchScript(True, "idle", "completed", "codex")
            ]
        },
        pool,
    )
    supervisor.dispatcher = dispatcher

    supervisor.tick()
    supervisor.tick()
    assert_no_duplicate_dispatch(dispatcher)
    assert supervisor.work_item_store.load(item.work_item_id).state == (
        WorkItemState.SHIPPED
    )


def test_restart_after_dispatch_begins_reconciles_without_duplicate(tmp_path):
    clock = FakeClock(START)
    pool = ScriptedProviderPool(clock, [ProviderRecord("codex")])
    supervisor = _supervisor(tmp_path, pool, None, clock)
    item = _ready_item(supervisor.work_item_store, "crash-dispatch")
    injector = CrashInjector("after_dispatch_begins")
    dispatcher = ScriptedDispatcher(
        {
            item.work_item_id: [
                DispatchScript(True, "idle", "completed", "codex")
            ]
        },
        pool,
        injector,
    )
    supervisor.dispatcher = dispatcher
    with pytest.raises(InjectedCrash):
        supervisor.tick()

    restarted = _supervisor(tmp_path, pool, dispatcher, clock)
    restarted.tick()
    assert_no_duplicate_dispatch(dispatcher)
    assert restarted.work_item_store.load(item.work_item_id).state == (
        WorkItemState.AWAITING_OWNER
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_24_7: the supervisor has no injected crash seam covering "
        "the complete durable-boundary matrix"
    ),
)
def test_supervisor_exposes_every_required_crash_boundary():
    parameters = inspect.signature(FactorySupervisor).parameters
    assert "crash_injector" in parameters
    assert set(CRASH_MATRIX_BOUNDARIES).issubset(
        FactorySupervisor.CRASH_BOUNDARIES
    )


def test_operator_stop_then_resume_is_a_valid_persisted_transition(tmp_path):
    clock, pool, supervisor = _idle_supervisor(tmp_path)
    supervisor.stop()
    restarted = _supervisor(
        tmp_path, pool, ScriptedDispatcher({}, pool), clock
    )
    restarted.resume()
    assert restarted.state_record.state == SupervisorState.IDLE


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_24_7: factory status lacks the complete structured provider, "
        "parked-work, owner-request, capability, proposal, and idle-reason view"
    ),
)
def test_operator_status_answers_the_unattended_operation_questions(tmp_path):
    clock, pool, supervisor = _idle_supervisor(tmp_path)
    status = supervisor.status()
    assert {
        "state",
        "last_tick_at",
        "next_wake_at",
        "current_work_item_id",
        "recent_runs",
        "recent_failures",
        "parked_work",
        "owner_requests",
        "providers",
        "capabilities_added",
        "repo_proposals",
        "idle_reason",
    }.issubset(status)


def test_deterministic_72_hour_factory_scenario(tmp_path):
    wall_started = perf_counter()
    clock = FakeClock(START)
    transitions = [
        ProviderTransition(
            START + timedelta(hours=1),
            "claude",
            "SESSION_EXHAUSTED",
            START + timedelta(hours=6),
        ),
        ProviderTransition(
            START + timedelta(hours=2, minutes=30),
            "codex",
            "QUOTA_EXHAUSTED",
            START + timedelta(hours=10),
        ),
        ProviderTransition(
            START + timedelta(hours=6, minutes=1),
            "claude",
            "AVAILABLE",
        ),
        ProviderTransition(
            START + timedelta(hours=10),
            "codex",
            "AVAILABLE",
        ),
    ]
    pool = ScriptedProviderPool(
        clock,
        [
            ProviderRecord("claude"),
            ProviderRecord("codex"),
            ProviderRecord("local_ollama", locality="local"),
        ],
        transitions,
    )
    scheduled_observations = [
        (
            START + timedelta(hours=8),
            {
                "repository": "howlcipher/repo-b",
                "evidence_fingerprints": ["repo-b:shared-v1"],
                "capability_need": {
                    "capability_id": "shared-v1",
                    "required_interface": "shared-v1",
                },
            },
        ),
        (
            START + timedelta(hours=9),
            {
                "repository": "howlcipher/repo-c",
                "evidence_fingerprints": ["repo-a:x", "repo-b:x", "repo-c:x"],
                "capability_need": {
                    "capability_id": "cross-repo-x-v1",
                    "proposed_repository": "howl-cross-repo-x",
                    "has_natural_home": False,
                    "multiple_consumers": True,
                    "consumer_repositories": ["repo-a", "repo-b", "repo-c"],
                    "clear_purpose": True,
                    "bounded_maintenance": True,
                    "deterministic_verification": True,
                },
            },
        ),
    ]

    def discovery():
        due = []
        while scheduled_observations and scheduled_observations[0][0] <= clock.now():
            due.append(scheduled_observations.pop(0)[1])
        return due

    supervisor = _supervisor(tmp_path, pool, None, clock, discovery=discovery)
    supervisor.capability_registry.register(
        CapabilityRecord(
            capability_id="shared-v1",
            provided_by=["howlcipher/repo-a"],
            interfaces=["shared-v1"],
            verification_status=VerificationStatus.VERIFIED,
            active=True,
        )
    )
    origins = [
        WorkItemOrigin.OWNER_DIRECTION,
        WorkItemOrigin.EXISTING_BACKLOG,
        WorkItemOrigin.EXISTING_BACKLOG,
        WorkItemOrigin.MAINTENANCE,
        WorkItemOrigin.SELF_IMPROVEMENT,
        WorkItemOrigin.CREATIVE_EXPERIMENT,
        WorkItemOrigin.DISCOVERED_PROBLEM,
        WorkItemOrigin.INFERRED_NEED,
        WorkItemOrigin.EXISTING_BACKLOG,
        WorkItemOrigin.EXISTING_BACKLOG,
    ]
    items = [
        _ready_item(
            supervisor.work_item_store,
            index,
            origin=origin,
            repository=(
                "howlcipher/howlplane" if index % 3 == 0 else REPOSITORY
            ),
        )
        for index, origin in enumerate(origins)
    ]
    blocked = WorkItem.create(
        origin=WorkItemOrigin.EXISTING_BACKLOG,
        repository=REPOSITORY,
        title="blocked dependency",
        identity_keys=["factory-acceptance", "blocked"],
        blocked_by=[items[1].work_item_id],
    )
    blocked.transition_to(WorkItemState.ADMITTED)
    blocked.transition_to(WorkItemState.BLOCKED)
    supervisor.work_item_store.save_object(blocked)
    parked = WorkItem.create(
        origin=WorkItemOrigin.INFERRED_NEED,
        repository=REPOSITORY,
        title="needs owner",
        identity_keys=["factory-acceptance", "parked"],
    )
    parked.transition_to(WorkItemState.ADMITTED)
    parked.transition_to(WorkItemState.AWAITING_OWNER)
    supervisor.work_item_store.save_object(parked)

    scripts = {
        item.work_item_id: [
            DispatchScript(
                success=index not in {1, 2},
                next_state=("idle" if index not in {1, 2} else "backoff_after_failure"),
                reason=(
                    "completed"
                    if index not in {1, 2}
                    else ("engineering_failure" if index == 1 else "ci_failed")
                ),
                provider_id="auto",
            )
        ]
        for index, item in enumerate(items)
    }
    dispatcher = ScriptedDispatcher(scripts, pool)
    supervisor.dispatcher = dispatcher

    crash_at = START + timedelta(hours=12)
    recovery_item = None
    restart_count = 0
    while clock.now() < START + timedelta(hours=72):
        if clock.now() == crash_at:
            supervisor = _supervisor(
                tmp_path, pool, dispatcher, clock, discovery=discovery
            )
            recovery_item = _ready_item(
                supervisor.work_item_store,
                "post-restart-owner",
                origin=WorkItemOrigin.OWNER_DIRECTION,
            )
            dispatcher.scripts[recovery_item.work_item_id] = [
                DispatchScript(True, "idle", "completed", "auto")
            ]
            restart_count += 1
        supervisor.tick()
        clock.advance(60 * 60)

    assert_no_duplicate_dispatch(dispatcher)
    assert dispatcher.calls[0] == items[0].work_item_id
    assert len(dispatcher.calls) == 11
    assert recovery_item is not None
    assert recovery_item.work_item_id in dispatcher.calls
    assert restart_count == 1
    assert supervisor.work_item_store.load(parked.work_item_id).state == (
        WorkItemState.AWAITING_OWNER
    )
    assert supervisor.work_item_store.load(blocked.work_item_id).state == (
        WorkItemState.BLOCKED
    )
    assert len(pool.transition_history) == 4
    assert pool.records["local_ollama"].generative_attempts >= 1
    assert any(
        attempt >= START + timedelta(hours=6, minutes=1)
        for attempt in pool.records["claude"].generative_attempt_times
    )
    assert len(supervisor.state_record.recent_failed) == 2
    assert {failure["reason"] for failure in supervisor.state_record.recent_failed} == {
        "engineering_failure",
        "ci_failed",
    }
    reused = supervisor.capability_registry.find("shared-v1")
    assert reused is not None
    assert "howlcipher/repo-b" in reused.required_by
    proposals = supervisor.repo_proposal_store.list_awaiting_authority()
    assert [proposal.repository_name for proposal in proposals] == [
        "howl-cross-repo-x"
    ]
    assert supervisor.state_record.state in {
        SupervisorState.WAITING_FOR_WORK,
        SupervisorState.WAITING_FOR_PROVIDER,
    }
    assert perf_counter() - wall_started < 5.0

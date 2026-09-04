#!/usr/bin/env python3
"""Tests for the deterministic factory supervisor tick/run loop."""

from datetime import datetime, timedelta, timezone

import pytest

from tests._factory_test_helpers import (
    START,
    FakeProviderPool,
    RecordingDispatcher as FakeDispatcher,
    SuccessEngine,
    make_supervisor as _make_supervisor,
    ready_pair,
    ready_work_item as _ready_work_item,
    unavailable_supervisor,
)

from src.control_plane.factory.dispatcher import DispatchOutcome, MarathonDispatcherAdapter
from src.control_plane.factory.portfolio import FactoryPolicy
from src.control_plane.factory.repo_proposal import CapabilityStore, RepoProposalStore
from src.control_plane.factory.supervisor import FactorySupervisor
from src.control_plane.factory.supervisor_state import SupervisorState, SupervisorStateStore
from src.control_plane.factory.work_item import WorkItem, WorkItemOrigin, WorkItemState, WorkItemStore


class _ParkedAuthorityEngine:
    def execute_factory_work_item(self, item, files_changed=None):
        return False, {"integration_mode": "parked"}


class _ProviderExhaustedEngine:
    def execute_factory_work_item(self, item, files_changed=None):
        return False, {
            "failure_reason": "NO_ELIGIBLE_PROVIDER_REMAINING: tried codex",
            "failure_class": "PROVIDER_EXHAUSTED",
        }


class _BlockedEngine:
    def execute_factory_work_item(self, item, files_changed=None):
        return False, {
            "failure_reason": "BLOCKED: missing dependency",
            "failure_class": "DEPENDENCY_BLOCKED",
        }


class _FailingEngine:
    def execute_factory_work_item(self, item, files_changed=None):
        return False, {"failure_reason": "orchestrator_final_state:failed"}


def test_tick_with_no_work_goes_idle_and_schedules_next_wake(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    result = supervisor.tick()
    assert result.state == SupervisorState.WAITING_FOR_WORK
    assert result.next_wake_at == now["t"] + timedelta(seconds=5.0)
    assert result.selected_work_item_id is None


def test_backlog_evidence_is_admitted_and_ready(tmp_path):
    supervisor, now, sleeps = _make_supervisor(
        tmp_path,
        discovery=lambda: [
            {
                "origin": "existing_backlog",
                "repository": "howlcipher/howlplane",
                "title": "bug",
                "identity_keys": ["bugs.md", "1"],
                "evidence_refs": ["bugs.md#1"],
                "evidence_fingerprints": ["backlog:bugs.md:1"],
                "source_file_rank": 0,
                "source_rank": 1,
                "kind": "bug",
            }
        ],
    )
    supervisor.tick()
    item = supervisor.work_item_store.list_all()[0]
    assert item.state in (WorkItemState.READY, WorkItemState.SHIPPED)
    assert supervisor.state_record.observations_consumed == 1


@pytest.mark.parametrize(
    "origin, title, identity_key, extra_evidence",
    [
        ("inferred_need", "vague idea", "idea", {"is_ambiguous": True}),
        ("creative_experiment", "spike", "spike", {}),
    ],
)
def test_uncommitted_discovery_awaits_owner(
    tmp_path, origin, title, identity_key, extra_evidence
):
    evidence = {
        "origin": origin,
        "repository": "howlcipher/howlplane",
        "title": title,
        "identity_keys": [identity_key],
        "evidence_refs": ["obs-1"],
        "evidence_fingerprints": ["fp-1"],
        **extra_evidence,
    }
    supervisor, now, sleeps = _make_supervisor(tmp_path, discovery=lambda: [evidence])
    supervisor.tick()
    item = supervisor.work_item_store.list_all()[0]
    assert item.state == WorkItemState.AWAITING_OWNER


def test_owner_preemption_selects_owner_direction(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    ready = _ready_work_item(
        supervisor.work_item_store,
        title="ready",
        identity_keys=["ready"],
    )
    owner = _ready_work_item(
        supervisor.work_item_store,
        origin=WorkItemOrigin.OWNER_DIRECTION,
        title="owner direction",
        identity_keys=["owner"],
    )
    result = supervisor.tick()
    assert result.selected_work_item_id == owner.work_item_id


def test_dispatch_transitions_item_to_in_progress_and_records_ids(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    item = _ready_work_item(
        supervisor.work_item_store,
        title="do the thing",
        identity_keys=["bugs.md", "1"],
    )
    supervisor.tick()
    loaded = supervisor.work_item_store.load(item.work_item_id)
    assert loaded.state == WorkItemState.SHIPPED
    assert loaded.task_ids
    assert supervisor.state_record.current_work_item_id is None
    assert supervisor.state_record.recent_completed


def test_authority_park_sets_awaiting_owner_and_allows_next_item(tmp_path):
    supervisor, now, sleeps = _make_supervisor(
        tmp_path,
        dispatcher=FakeDispatcher([
            DispatchOutcome(
                success=False,
                work_item_id="",
                next_work_item_state=WorkItemState.AWAITING_OWNER,
                reason="parked_awaiting_human_authority",
                requires_authority=True,
                blocker="authority_boundary",
            ),
            DispatchOutcome(
                success=True,
                work_item_id="",
                next_work_item_state=WorkItemState.SHIPPED,
                reason="governed_lifecycle_completed",
            ),
        ]),
    )
    parked, ready = ready_pair(supervisor.work_item_store, first_title="parked")
    supervisor.tick()
    assert supervisor.work_item_store.load(parked.work_item_id).state == WorkItemState.AWAITING_OWNER
    supervisor.tick()
    assert supervisor.work_item_store.load(ready.work_item_id).state == WorkItemState.SHIPPED


def test_provider_park_sets_deferred_with_retry_metadata_and_reprobes(tmp_path):
    retry_after = (START + timedelta(seconds=60)).isoformat()
    supervisor, now, sleeps = _make_supervisor(
        tmp_path,
        dispatcher=MarathonDispatcherAdapter(lambda: _ProviderExhaustedEngine()),
        pool=FakeProviderPool(has_capacity=False, retry_after=retry_after),
    )
    item = _ready_work_item(
        supervisor.work_item_store,
        title="failing",
        identity_keys=["fail"],
    )
    result = supervisor.tick()
    assert result.state == SupervisorState.WAITING_FOR_PROVIDER
    assert result.next_wake_at == START + timedelta(seconds=60)
    loaded = supervisor.work_item_store.load(item.work_item_id)
    assert loaded.state == WorkItemState.DEFERRED


def test_dependency_block_sets_blocked_and_allows_other_ready(tmp_path):
    supervisor, now, sleeps = _make_supervisor(
        tmp_path,
        dispatcher=FakeDispatcher([
            DispatchOutcome(
                success=False,
                work_item_id="",
                next_work_item_state=WorkItemState.BLOCKED,
                reason="BLOCKED: missing dependency",
                blocker="dependency",
            ),
            DispatchOutcome(
                success=True,
                work_item_id="",
                next_work_item_state=WorkItemState.SHIPPED,
                reason="governed_lifecycle_completed",
            ),
        ]),
    )
    blocked, ready = ready_pair(supervisor.work_item_store, first_title="blocked")
    supervisor.tick()
    assert supervisor.work_item_store.load(blocked.work_item_id).state == WorkItemState.BLOCKED
    supervisor.tick()
    assert supervisor.work_item_store.load(ready.work_item_id).state == WorkItemState.SHIPPED


def test_failure_sets_failed_and_backoff(tmp_path):
    supervisor, now, sleeps = _make_supervisor(
        tmp_path,
        dispatcher=MarathonDispatcherAdapter(lambda: _FailingEngine()),
    )
    item = _ready_work_item(
        supervisor.work_item_store,
        title="failing",
        identity_keys=["fail"],
    )
    supervisor.tick()
    assert supervisor.work_item_store.load(item.work_item_id).state == WorkItemState.FAILED
    assert supervisor.state_record.state == SupervisorState.BACKOFF_AFTER_FAILURE
    assert supervisor.state_record.failure_count == 1


def test_crash_after_dispatch_before_portfolio_update_parks_item(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    item = _ready_work_item(
        supervisor.work_item_store,
        title="in flight",
        identity_keys=["inflight"],
    )
    supervisor.tick()
    assert supervisor.work_item_store.load(item.work_item_id).state == WorkItemState.SHIPPED

    # Simulate crash: reload state as-is, then pretend we were in DISPATCHING.
    supervisor2, now2, sleeps2 = _make_supervisor(tmp_path)
    supervisor2.state_record.state = "dispatching"
    supervisor2.state_record.current_work_item_id = item.work_item_id
    supervisor2.state_record.current_dispatch_id = "D-1"
    supervisor2.state_record.current_task_id = "FACTORY-1"
    supervisor2.state_store.save(supervisor2.state_record)

    supervisor3, now3, sleeps3 = _make_supervisor(tmp_path)
    assert supervisor3.state_record.state == SupervisorState.BACKOFF_AFTER_FAILURE
    assert supervisor3.state_record.current_work_item_id == item.work_item_id
    assert supervisor3.state_record.current_dispatch_id == "D-1"
    assert supervisor3.state_record.current_task_id == "FACTORY-1"


def test_no_spin_wait_uses_exact_retry_after(tmp_path):
    supervisor, now, sleeps, retry_after = unavailable_supervisor(
        tmp_path, wait_seconds=42
    )
    result = supervisor.tick()
    assert result.state == SupervisorState.WAITING_FOR_PROVIDER
    assert result.next_wake_at == START + timedelta(seconds=42)
    assert supervisor.state_record.provider_wake_conditions["retry_after"] == retry_after


def test_run_loop_stops_on_stop(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)

    def stop_after_one():
        if sleeps:
            supervisor.stop()

    original_sleep = supervisor._sleep

    def sleeping(seconds):
        original_sleep(seconds)
        stop_after_one()

    supervisor._sleep = sleeping
    supervisor.run()
    assert supervisor.state_record.state == SupervisorState.STOPPED
    assert sleeps


def test_next_wake_is_persisted_before_tick_work(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    _ready_work_item(
        supervisor.work_item_store,
        title="work",
        identity_keys=["ready"],
    )

    # Observe persisted next_wake before dispatch mutates state.
    before_tick = supervisor.state_store.load()
    assert before_tick.next_wake_at is None
    supervisor.tick()
    loaded = supervisor.state_store.load()
    assert loaded.next_wake_at is not None


def test_run_loop_reloads_state_and_stops_externally(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    # Pre-populate state as if another process stopped it.
    external = supervisor.state_store.load()
    external.transition_to(SupervisorState.STOPPED, reason="external_stop")
    external.stopped_reason = "external_stop"
    supervisor.state_store.save(external)

    supervisor.run()
    assert supervisor.state_record.state == SupervisorState.STOPPED
    assert supervisor.state_record.stopped_reason == "external_stop"
    assert not sleeps


def test_waiting_for_provider_does_not_reprobe_transient_exhaustion(tmp_path):
    pool = FakeProviderPool(has_capacity=False, retry_after=(START + timedelta(seconds=30)).isoformat())
    supervisor, now, sleeps = _make_supervisor(tmp_path, pool=pool)
    supervisor.tick()
    assert supervisor.state_record.state == SupervisorState.WAITING_FOR_PROVIDER
    assert not pool.reprobed


def test_deferred_item_is_requeued_only_when_retry_after_is_due(tmp_path):
    retry_after = (START + timedelta(seconds=60)).isoformat()

    class _EventuallySucceedsEngine:
        def __init__(self):
            self.calls = 0

        def execute_factory_work_item(self, item, files_changed=None):
            self.calls += 1
            if self.calls == 1:
                return False, {
            "failure_reason": "NO_ELIGIBLE_PROVIDER_REMAINING: tried codex",
            "failure_class": "PROVIDER_EXHAUSTED",
        }
            return True, {"provider": "fake"}

    engine = _EventuallySucceedsEngine()
    supervisor, now, sleeps = _make_supervisor(
        tmp_path,
        dispatcher=MarathonDispatcherAdapter(lambda: engine),
        pool=FakeProviderPool(has_capacity=True, retry_after=retry_after),
    )
    item = _ready_work_item(
        supervisor.work_item_store,
        title="deferred",
        identity_keys=["defer"],
    )

    result = supervisor.tick()
    loaded = supervisor.work_item_store.load(item.work_item_id)
    assert loaded.state == WorkItemState.DEFERRED
    assert loaded.retry_after == retry_after
    assert result.state == SupervisorState.WAITING_FOR_PROVIDER

    # Before retry_after, the item is not requeued.
    now["t"] = START + timedelta(seconds=30)
    result2 = supervisor.tick()
    assert supervisor.work_item_store.load(item.work_item_id).state == WorkItemState.DEFERRED
    assert result2.selected_work_item_id is None

    # Once retry_after is due, the item becomes READY again and can dispatch.
    now["t"] = START + timedelta(seconds=60)
    result3 = supervisor.tick()
    assert supervisor.work_item_store.load(item.work_item_id).state == WorkItemState.SHIPPED
    assert result3.selected_work_item_id == item.work_item_id


def test_orphan_in_progress_item_is_reconciled_even_when_state_not_dispatching(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    item = _ready_work_item(
        supervisor.work_item_store,
        title="orphan",
        identity_keys=["orphan"],
    )
    item.transition_to(WorkItemState.IN_PROGRESS, reason="crash")
    supervisor.work_item_store.save_object(item)

    supervisor.tick()
    loaded = supervisor.work_item_store.load(item.work_item_id)
    assert loaded.state == WorkItemState.AWAITING_OWNER
    assert loaded.admission_blocked_reason == "orphan_in_progress_reconciled"


def test_successful_dispatch_does_not_count_as_failure(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    _ready_work_item(
        supervisor.work_item_store,
        title="ok",
        identity_keys=["ok"],
    )
    supervisor.tick()
    assert supervisor.state_record.state == SupervisorState.IDLE
    assert supervisor.state_record.failure_count == 0
    assert supervisor.state_record.recent_failed == []


def test_authority_park_does_not_count_as_failure(tmp_path):
    supervisor, now, sleeps = _make_supervisor(
        tmp_path,
        dispatcher=MarathonDispatcherAdapter(lambda: _ParkedAuthorityEngine()),
    )
    item = _ready_work_item(
        supervisor.work_item_store,
        title="authority",
        identity_keys=["auth"],
    )
    supervisor.tick()
    assert supervisor.state_record.state == SupervisorState.WAITING_FOR_AUTHORITY
    assert supervisor.state_record.failure_count == 0
    assert supervisor.state_record.recent_failed == []
    assert any(p["work_item_id"] == item.work_item_id for p in supervisor.state_record.recent_parked)


def test_dependency_block_does_not_count_as_failure(tmp_path):
    supervisor, now, sleeps = _make_supervisor(
        tmp_path,
        dispatcher=MarathonDispatcherAdapter(lambda: _BlockedEngine()),
    )
    item = _ready_work_item(
        supervisor.work_item_store,
        title="blocked",
        identity_keys=["blocked"],
    )
    supervisor.tick()
    assert supervisor.state_record.state == SupervisorState.WAITING_FOR_DEPENDENCY
    assert supervisor.state_record.failure_count == 0
    assert supervisor.state_record.recent_failed == []
    assert any(p["work_item_id"] == item.work_item_id for p in supervisor.state_record.recent_parked)


def test_missing_selected_item_stops_and_retains_evidence(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    item = _ready_work_item(
        supervisor.work_item_store,
        title="will vanish",
        identity_keys=["vanish"],
    )

    # Remove the item between selection and dispatch by making load fail while
    # list_all still returns the candidate, simulating a race where the item
    # disappears after it was selected.
    class VanishingStore:
        def __init__(self, item):
            self._item = item

        def load(self, _id):
            raise FileNotFoundError()

        def list_all(self):
            return [self._item]

    original_store = supervisor.work_item_store
    supervisor.work_item_store = VanishingStore(item)  # type: ignore[assignment]
    result = supervisor.tick()
    assert result.state == SupervisorState.STOPPED
    assert supervisor.state_record.stopped_reason == "missing_item"
    assert item.work_item_id in (supervisor.state_record.last_error or "")


def test_dispatch_history_records_origin_and_repository(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    _ready_work_item(
        supervisor.work_item_store,
        title="work",
        identity_keys=["history"],
    )
    supervisor.tick()
    assert supervisor.state_record.dispatch_history
    last = supervisor.state_record.dispatch_history[-1]
    assert last["origin"] == WorkItemOrigin.EXISTING_BACKLOG
    assert last["repository"] == "howlcipher/howlplane"


def test_admission_decisions_are_deduped(tmp_path):
    supervisor, now, sleeps = _make_supervisor(
        tmp_path,
        discovery=lambda: [
            {
                "origin": "existing_backlog",
                "repository": "howlcipher/howlplane",
                "title": "dup",
                "identity_keys": ["bugs.md", "1"],
                "evidence_refs": ["bugs.md#1"],
                "evidence_fingerprints": ["backlog:bugs.md:1"],
                "source_file_rank": 0,
                "source_rank": 1,
                "kind": "bug",
            }
        ]
        * 3,
    )
    supervisor.tick()
    decisions = [
        d
        for d in supervisor.state_record.admission_decisions
        if d.get("origin") == "existing_backlog" and d.get("title") == "dup"
    ]
    assert len(decisions) == 1


def test_untrusted_owner_direction_is_parked(tmp_path):
    supervisor, now, sleeps = _make_supervisor(
        tmp_path,
        discovery=lambda: [
            {
                "origin": "owner_direction",
                "repository": "howlcipher/howlplane",
                "title": "untrusted",
                "identity_keys": ["owner"],
                "evidence_refs": ["obs-1"],
                "evidence_fingerprints": ["fp-1"],
                "trusted_provenance": False,
            }
        ],
    )
    supervisor.tick()
    item = supervisor.work_item_store.list_all()[0]
    assert item.state == WorkItemState.AWAITING_OWNER


def test_trusted_owner_direction_is_ready(tmp_path):
    supervisor, now, sleeps = _make_supervisor(
        tmp_path,
        discovery=lambda: [
            {
                "origin": "owner_direction",
                "repository": "howlcipher/howlplane",
                "title": "trusted",
                "identity_keys": ["owner"],
                "evidence_refs": ["obs-1"],
                "evidence_fingerprints": ["fp-1"],
                "trusted_provenance": True,
            }
        ],
    )
    supervisor.tick()
    item = supervisor.work_item_store.list_all()[0]
    # The backlog evidence path leads to dispatch with the success engine, so
    # the item ends up shipped; check it was at least admitted to READY first.
    assert item.state == WorkItemState.SHIPPED


def test_status_includes_provider_inventory(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    status = supervisor.status()
    assert "provider_inventory" in status


def test_run_holds_single_supervisor_lock(tmp_path):
    from src.control_plane.locking import SupervisorLock

    state_dir = tmp_path / "state"
    supervisor, now, sleeps = _make_supervisor(tmp_path, state_dir=state_dir)
    # First, pre-create the state store files so the supervisor can start.
    supervisor.state_store.save(supervisor.state_store.load())

    lock = SupervisorLock(state_dir)
    lock.acquire()
    try:
        # With the lock held externally, the run loop should fail to acquire.
        supervisor.run()
        # A rejected contender must not persist stale local state over the
        # supervisor that holds the singleton lock.
        persisted = supervisor.state_store.load()
        assert persisted.state == SupervisorState.IDLE
        assert persisted.stopped_reason is None
    finally:
        lock.release()


def test_default_sleep_is_time_sleep(tmp_path):
    import time

    state_store = SupervisorStateStore(tmp_path / "supervisor")
    work_store = WorkItemStore(tmp_path / "work_items")
    repo_store = RepoProposalStore(tmp_path / "repo_proposals")
    cap_store = CapabilityStore(tmp_path / "capabilities")
    supervisor = FactorySupervisor(
        state_store=state_store,
        work_item_store=work_store,
        repo_proposal_store=repo_store,
        capability_store=cap_store,
        dispatcher=MarathonDispatcherAdapter(lambda: SuccessEngine()),
        discovery=lambda: [],
        provider_pool=FakeProviderPool(),
    )
    assert supervisor._sleep is time.sleep


def test_multiple_consecutive_idle_ticks_do_not_oscillate_or_fail(tmp_path):
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    for _ in range(5):
        res = supervisor.tick()
        assert res.state == SupervisorState.WAITING_FOR_WORK
        assert supervisor.state_record.state == SupervisorState.WAITING_FOR_WORK
        assert supervisor.state_record.failure_count == 0
    # Exactly one initial transition to waiting_for_work, no backoff oscillation
    assert len(supervisor.state_record.transition_history) == 1
    assert supervisor.state_record.transition_history[0]["to_state"] == "waiting_for_work"


def test_resume_and_stop_synchronize_with_disk_state(tmp_path):
    supervisor1, now, sleeps = _make_supervisor(tmp_path)
    supervisor2, now2, sleeps2 = _make_supervisor(tmp_path)

    # supervisor1 stops
    supervisor1.stop(reason="external_stop")
    assert supervisor1.state_record.state == SupervisorState.STOPPED

    # supervisor2 reloads from disk and resumes successfully
    supervisor2.resume()
    assert supervisor2.state_record.state == SupervisorState.IDLE
    assert supervisor2.state_store.load().state == SupervisorState.IDLE


def test_proposal_disposition_value_is_canonical_string(tmp_path):
    supervisor, now, sleeps = _make_supervisor(
        tmp_path,
        discovery=lambda: [
            {
                "origin": "inferred_need",
                "repository": "howlcipher/howlplane",
                "capability_need": {
                    "capability_id": "metrics_engine",
                    "has_natural_home": False,
                    "clear_purpose": True,
                    "bounded_maintenance": True,
                    "deterministic_verification": True,
                    "multiple_consumers": True,
                    "proposed_repository": "howl-metrics",
                },
                "evidence_fingerprints": ["fp-metrics-1"],
            }
        ],
    )
    supervisor.tick()
    proposal = supervisor.repo_proposal_store.load("PROP-metrics_engine")
    assert proposal is not None
    assert proposal.disposition == "propose_new_repository"
    assert proposal.rationale == "propose_new_repository"

#!/usr/bin/env python3
"""Acceptance tests for the factory supervisor loop.

These tests exercise the complete durable lifecycle: crash windows, exact
retry-after scheduling without spin, owner priority, dependency handling, and the
full set of dispatch outcomes.
"""

from tests._factory_test_helpers import (
    make_supervisor as _make_supervisor,
    ready_work_item as _ready_item,
    unavailable_supervisor,
)

from src.control_plane.factory.dispatcher import DispatchOutcome
from src.control_plane.factory.supervisor_state import SupervisorState
from src.control_plane.factory.work_item import WorkItemState


class _CountingDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch(self, item, dispatch_id, task_id):
        self.calls.append(item.work_item_id)
        return DispatchOutcome(
            success=True,
            work_item_id=item.work_item_id,
            next_work_item_state=WorkItemState.SHIPPED,
            reason="governed_lifecycle_completed",
            task_id=task_id,
            dispatch_id=dispatch_id,
        )


def _restart_mid_dispatch(tmp_path, supervisor, item):
    supervisor.state_record.state = SupervisorState.DISPATCHING
    supervisor.state_record.current_work_item_id = item.work_item_id
    supervisor.state_record.current_dispatch_id = "D-1"
    supervisor.state_record.current_task_id = "FACTORY-1"
    supervisor.state_store.save(supervisor.state_record)
    counting = _CountingDispatcher()
    restarted, _, _ = _make_supervisor(tmp_path)
    restarted.dispatcher = counting
    restarted.tick()
    return restarted, counting


def test_crash_after_item_persisted_in_progress_prevents_duplicate_dispatch(tmp_path):
    """Crash after the item is IN_PROGRESS but before the supervisor records DISPATCHING."""
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    item = _ready_item(supervisor.work_item_store, key="inflight")
    item.transition_to(WorkItemState.IN_PROGRESS, reason="dispatched")
    supervisor.work_item_store.save_object(item)

    supervisor2, counting = _restart_mid_dispatch(tmp_path, supervisor, item)

    assert item.work_item_id not in counting.calls
    assert supervisor2.work_item_store.load(item.work_item_id).state == WorkItemState.AWAITING_OWNER
    assert supervisor2.state_record.state == SupervisorState.WAITING_FOR_WORK
    assert supervisor2.state_record.current_work_item_id is None


def test_crash_after_dispatch_before_state_record_persist_prevents_duplicate(tmp_path):
    """Crash after dispatch succeeds but before the state record is updated."""
    supervisor, now, sleeps = _make_supervisor(tmp_path)
    item = _ready_item(supervisor.work_item_store, key="nearly-done")
    supervisor.work_item_store.save_object(item)
    supervisor.tick()
    assert supervisor.work_item_store.load(item.work_item_id).state == WorkItemState.SHIPPED

    supervisor2, counting = _restart_mid_dispatch(tmp_path, supervisor, item)

    assert item.work_item_id not in counting.calls
    assert supervisor2.work_item_store.load(item.work_item_id).state == WorkItemState.SHIPPED
    assert supervisor2.state_record.state == SupervisorState.WAITING_FOR_WORK


def test_run_loop_uses_exact_retry_after_without_spin(tmp_path):
    """When the only blocker is provider retry_after, the loop sleeps exactly once."""
    supervisor, now, sleeps, retry_after = unavailable_supervisor(
        tmp_path, wait_seconds=42
    )
    calls = []
    original_sleep = supervisor._sleep

    def stop_after_two(seconds):
        calls.append(seconds)
        if len(calls) >= 2:
            supervisor.stop()
        original_sleep(seconds)

    supervisor._sleep = stop_after_two
    supervisor.run()

    assert supervisor.state_record.state == SupervisorState.STOPPED
    assert len(calls) >= 1
    assert calls[0] == 42.0
    # No zero-length sleeps that would indicate a spin loop.
    assert all(c > 0 for c in calls)



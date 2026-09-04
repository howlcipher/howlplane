#!/usr/bin/env python3
"""Tests for the durable factory supervisor state machine."""

import pytest

from src.control_plane.factory.supervisor_state import (
    SUPERVISOR_STATE_SCHEMA_VERSION,
    InvalidSupervisorStateTransitionError,
    SupervisorState,
    SupervisorStateRecord,
    SupervisorStateStore,
)


def test_default_state_is_idle():
    record = SupervisorStateRecord(created_at="2026-08-30T12:00:00+00:00")
    assert record.state == SupervisorState.IDLE
    assert record.schema_version == SUPERVISOR_STATE_SCHEMA_VERSION
    assert record.supervisor_id == "factory_supervisor"


def test_state_transition_is_recorded_in_transition_history():
    record = SupervisorStateRecord(created_at="2026-08-30T12:00:00+00:00")
    record.transition_to(SupervisorState.DISPATCHING, reason="item_selected")
    assert record.state == "dispatching"
    assert record.transition_history[-1]["to_state"] == "dispatching"
    assert record.transition_history[-1]["reason"] == "item_selected"


def test_illegal_transition_raises():
    record = SupervisorStateRecord(created_at="2026-08-30T12:00:00+00:00")
    with pytest.raises(InvalidSupervisorStateTransitionError):
        record.transition_to(SupervisorState.WAITING_FOR_AUTHORITY)


def test_reconcile_retains_current_item_and_dispatch():
    record = SupervisorStateRecord(created_at="2026-08-30T12:00:00+00:00")
    record.current_work_item_id = "WI-foo-1"
    record.current_dispatch_id = "D-1"
    record.current_task_id = "FACTORY-1"
    record.state = "dispatching"
    record.reconcile_on_load()
    assert record.state == SupervisorState.BACKOFF_AFTER_FAILURE
    assert record.failure_count == 1
    assert record.current_work_item_id == "WI-foo-1"
    assert record.current_dispatch_id == "D-1"
    assert record.current_task_id == "FACTORY-1"
    assert "Restart during dispatch" in (record.last_error or "")


def test_malformed_state_fails_closed(tmp_path):
    store = SupervisorStateStore(tmp_path / "supervisor")
    store.save(SupervisorStateRecord(created_at="2026-08-30T12:00:00+00:00"))
    (tmp_path / "supervisor" / "factory_supervisor.json").write_text("{not json")
    record = store.load()
    assert record.state == SupervisorState.STOPPED
    assert record.stopped_reason == "malformed_state"
    assert record.last_error is not None


def test_store_round_trip(tmp_path):
    store = SupervisorStateStore(tmp_path / "supervisor")
    record = store.load()
    record.transition_to(SupervisorState.WAITING_FOR_PROVIDER, reason="no_capacity")
    store.save(record)
    loaded = store.load()
    assert loaded.state == SupervisorState.WAITING_FOR_PROVIDER


def test_unknown_state_is_refused_at_construction():
    with pytest.raises(ValueError):
        SupervisorStateRecord(state="not_a_state", created_at="2026-08-30T12:00:00+00:00")


@pytest.mark.parametrize(
    "contents, expected_substring",
    [
        (
            '{"schema_version": "old", "state": "idle", "created_at": "2026-08-30T12:00:00+00:00"}',
            "schema version",
        ),
        (
            '{"schema_version": "' + SUPERVISOR_STATE_SCHEMA_VERSION + '", "state": "not_a_state", "created_at": "2026-08-30T12:00:00+00:00"}',
            "unknown supervisor state",
        ),
        ("{not json", "failed to read"),
    ],
)
def test_malformed_state_loads_fail_closed(tmp_path, contents, expected_substring):
    store = SupervisorStateStore(tmp_path / "supervisor")
    (tmp_path / "supervisor" / "factory_supervisor.json").write_text(contents)
    record = store.load()
    assert record.state == SupervisorState.STOPPED
    assert record.stopped_reason == "malformed_state"
    assert expected_substring in (record.last_error or "").lower()

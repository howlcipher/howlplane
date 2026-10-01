"""Backoff facts are stored once and read identically by status, events and snapshot (row 80)."""

import json
from dataclasses import asdict
from datetime import timedelta

import pytest

from tests._factory_test_helpers import make_supervisor, ready_work_item
from tests.test_factory_supervisor import _FailingEngine

from howlplane.control_plane.factory.dispatcher import MarathonDispatcherAdapter
from howlplane.control_plane.factory.oplog import events_path
from howlplane.control_plane.factory.status_publish import build_redacted_status
from howlplane.control_plane.factory.supervisor_state import SupervisorState, SupervisorStateRecord
from howlplane.control_plane.presentation.operator import derive_operator_status, render_operator_text

pytestmark = pytest.mark.unit


def _failed_supervisor(tmp_path):
    supervisor, now, _ = make_supervisor(
        tmp_path, dispatcher=MarathonDispatcherAdapter(lambda: _FailingEngine()))
    ready_work_item(supervisor.work_item_store, title="failing", identity_keys=["fail"])
    supervisor.tick()
    return supervisor, now


def test_backoff_facts_are_stored_when_the_retry_is_scheduled(tmp_path):
    supervisor, now = _failed_supervisor(tmp_path)
    record = supervisor.state_store.load()
    assert record.state == SupervisorState.BACKOFF_AFTER_FAILURE
    assert (record.backoff_reason, record.backoff_attempt, record.backoff_delay_seconds) == ("failure_backoff", 1, 2)
    assert record.next_wake_at == (now["t"] + timedelta(seconds=2)).isoformat()


def test_status_event_and_snapshot_report_the_same_fact(tmp_path):
    supervisor, _ = _failed_supervisor(tmp_path)
    status = supervisor.status()
    fact = (status["backoff_reason"], status["backoff_attempt"], status["backoff_delay_seconds"])
    assert fact == ("failure_backoff", 1, 2)

    events = [json.loads(line) for line in events_path(tmp_path).read_text().splitlines()]
    changed = [e for e in events if e["code"] == "supervisor.state_changed" and e.get("backoff_reason")][-1]
    assert (changed["backoff_reason"], changed["backoff_attempt"], changed["backoff_delay_seconds"]) == fact

    snapshot = build_redacted_status(status)
    assert (snapshot["backoff"]["reason"], snapshot["backoff"]["attempt"],
            snapshot["backoff"]["delay_seconds"]) == fact


def test_operator_status_copies_the_stored_fact_and_renders_it(tmp_path):
    supervisor, now = _failed_supervisor(tmp_path)
    status = supervisor.status()
    op = derive_operator_status(status, now=now["t"])
    assert (op.retry_reason, op.retry_attempt, op.retry_delay_seconds) == ("failure_backoff", 1, 2)
    assert any("failure backoff, attempt 1" in line for line in render_operator_text(op, status))


def test_waiting_for_provider_stores_the_provider_retry_after_and_no_attempt(tmp_path):
    from tests._factory_test_helpers import unavailable_supervisor
    supervisor, _, _, _ = unavailable_supervisor(tmp_path, wait_seconds=90)
    supervisor.tick()
    record = supervisor.state_store.load()
    assert record.state == SupervisorState.WAITING_FOR_PROVIDER
    assert record.backoff_reason == "provider_retry_after"
    assert record.backoff_attempt is None and record.backoff_delay_seconds == 90


def test_facts_clear_when_the_state_is_not_a_retry_wait(tmp_path):
    supervisor, now = _failed_supervisor(tmp_path)
    supervisor._compute_next_wake(SupervisorState.WAITING_FOR_WORK, now["t"])
    assert supervisor.state_record.backoff_facts() == {
        "backoff_reason": None, "backoff_attempt": None, "backoff_delay_seconds": None}


def test_old_records_without_backoff_fields_still_load():
    data = asdict(SupervisorStateRecord())
    for key in ("backoff_reason", "backoff_attempt", "backoff_delay_seconds"):
        data.pop(key)
    record = SupervisorStateRecord.from_dict(data)
    assert record.backoff_reason is None and record.backoff_delay_seconds is None

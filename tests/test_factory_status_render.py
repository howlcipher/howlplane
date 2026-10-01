"""factory status: state -> meaning -> next action, from durable facts only."""

from datetime import datetime, timedelta, timezone

import pytest

from howlplane.control_plane.cli import build_parser
from howlplane.control_plane.presentation.operator import (
    derive_operator_status, render_operator_text, worker_display)

pytestmark = pytest.mark.unit
NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def _status(**kw):
    base = {"state": "idle", "project": "grocery-optimizer", "authority": "standard", "recent_completed": [],
            "recent_failed": [], "parked_items": [], "proposals_awaiting_authority": [],
            "provider_wake_conditions": {}, "last_error": None, "stopped_reason": None}
    base.update(kw)
    return base


def _text(status):
    return "\n".join(render_operator_text(derive_operator_status(status, now=NOW), status))


def test_running_shows_worker_elapsed_attempt_and_no_action():
    status = _status(state="dispatching", current_work_item_id="WI-042", current_work_title="Normalize units",
                     worker_resource_id="codex", dispatch_started_at=(NOW - timedelta(seconds=374)).isoformat(),
                     current_attempt=1)
    op = derive_operator_status(status, now=NOW)
    assert (op.worker, op.worker_resource_id, op.elapsed_seconds, op.attempt) == ("Codex", "codex", 374, 1)
    text = _text(status)
    assert "RUNNING" in text and "06m 14s" in text and "WI-042: Normalize units" in text
    assert "No action required." in text and "Worker" in text


def test_worker_is_never_guessed():
    op = derive_operator_status(_status(state="dispatching", current_work_item_id="WI-1"), now=NOW)
    assert op.worker is None and "Worker" not in _text(_status(state="dispatching", current_work_item_id="WI-1"))
    assert worker_display("mystery_provider") == "mystery_provider"


def test_session_limit_waits_with_retry_and_automatic_recovery():
    status = _status(state="waiting_for_provider", last_error="SESSION_LIMIT", process="running",
                     next_wake_at=(NOW + timedelta(minutes=42)).isoformat())
    op = derive_operator_status(status, now=NOW)
    assert op.retry_in_seconds == 42 * 60 and op.recovery == "automatic"
    text = _text(status)
    assert "WAITING" in text and "SESSION_LIMIT" in text and "in 42m 00s" in text and "Automatic" in text
    assert "howlplane agents doctor" in text


def test_retry_not_shown_when_wake_time_unknown_or_past():
    assert derive_operator_status(_status(state="backoff_after_failure"), now=NOW).retry_in_seconds is None
    past = _status(state="backoff_after_failure", next_wake_at=(NOW - timedelta(seconds=5)).isoformat())
    assert derive_operator_status(past, now=NOW).retry_in_seconds is None


def test_backoff_points_at_error_logs():
    text = _text(_status(state="backoff_after_failure", last_error="transport failure"))
    assert "BACKING OFF" in text and "howlplane factory logs --errors" in text


def test_stopped_process_is_not_called_automatic():
    status = _status(state="backoff_after_failure", process="absent")
    assert derive_operator_status(status, now=NOW).recovery == "manual"


def test_owner_required_names_exact_decision_commands():
    decision = {"work_item_id": "WI-042", "task_id": "FACTORY-WI-042",
                "approve": "howlplane approve FACTORY-WI-042 --repo /w", "reject": "howlplane reject FACTORY-WI-042 --repo /w"}
    status = _status(state="waiting_for_authority", decisions=[decision], parked_items=[
        {"work_item_id": "WI-042", "state": "awaiting_owner", "blocker": "approval"}])
    op = derive_operator_status(status, now=NOW)
    assert op.next_action.command == decision["approve"] and op.next_action.alternatives == [decision["reject"]]
    text = _text(status)
    assert "OWNER REQUIRED" in text and decision["approve"] in text and decision["reject"] in text


def test_owner_required_without_task_falls_back_to_real_command():
    status = _status(state="waiting_for_authority", parked_items=[
        {"work_item_id": "WI-042", "state": "awaiting_owner", "blocker": "x"}])
    assert derive_operator_status(status, now=NOW).next_action.command == "howlplane status"


@pytest.mark.parametrize("status", [
    _status(state="stopped", stopped_reason="operator_stop"),
    _status(state="stopped", last_error="boom"),
    _status(state="stopped", bounded_run_completed_at="2026-01-01T00:00:00+00:00"),
    _status(state="waiting_for_provider", last_error="SESSION_LIMIT"),
    _status(state="backoff_after_failure"),
    _status(state="waiting_for_authority", decisions=[{"work_item_id": "W", "task_id": "T",
            "approve": "howlplane approve T", "reject": "howlplane reject T"}], parked_items=[
            {"work_item_id": "W", "state": "awaiting_owner"}]),
])
def test_every_printed_command_exists(status):
    op = derive_operator_status(status, now=NOW)
    parser = build_parser()
    for command in ([op.next_action.command] + list(op.next_action.alternatives)) if op.next_action else []:
        if command:
            parser.parse_args(command.split()[1:])
    for line in _text(status).splitlines():
        if line.strip().startswith("howlplane "):
            parser.parse_args(line.split()[1:])


def test_plain_output_has_no_ansi_or_wide_glyphs():
    text = _text(_status(state="dispatching", current_work_item_id="WI-1"))
    assert text.isascii()

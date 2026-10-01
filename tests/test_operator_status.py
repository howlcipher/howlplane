"""Operator status model: one state -> label, reason code and real next command."""

import pytest

from howlplane.control_plane.cli import build_parser
from howlplane.control_plane.presentation.operator import (
    OPERATOR_STATUS_SCHEMA,
    ReasonCode,
    Severity,
    derive_operator_status,
    render_operator_text,
)


def _status(**overrides):
    base = {
        "state": "idle", "objective": None, "current_work_item_id": None,
        "last_error": None, "stopped_reason": None, "provider_wake_conditions": {},
        "recent_completed": [], "recent_failed": [], "parked_items": [],
        "proposals_awaiting_authority": [],
    }
    base.update(overrides)
    return base


CASES = [
    (_status(state="dispatching", current_work_item_id="HOWL-014"), "RUNNING", ReasonCode.NONE, Severity.OK),
    (_status(state="idle"), "IDLE", ReasonCode.WAITING_FOR_WORK, Severity.OK),
    (_status(state="stopped", stopped_reason="operator_stop"), "STOPPED", ReasonCode.STOPPED_BY_OPERATOR, Severity.INFO),
    (_status(state="stopped", last_error="boom"), "FAILED", ReasonCode.FACTORY_FAILURE, Severity.ERROR),
    (_status(state="waiting_for_authority", parked_items=[
        {"work_item_id": "HOWL-014", "state": "awaiting_owner", "blocker": "needs approval"}]),
     "OWNER REQUIRED", ReasonCode.OWNER_REQUIRED, Severity.ATTENTION),
    (_status(state="waiting_for_authority", authority="not configured"),
     "OWNER REQUIRED", ReasonCode.AUTHORITY_NOT_CONFIGURED, Severity.ATTENTION),
    (_status(state="waiting_for_provider", last_error="SESSION_LIMIT reached"),
     "WAITING", ReasonCode.SESSION_LIMIT, Severity.ATTENTION),
    (_status(state="backoff_after_failure", last_error="AUTHENTICATION_REQUIRED for codex"),
     "ATTENTION", ReasonCode.AUTHENTICATION_REQUIRED, Severity.ATTENTION),
    (_status(state="idle", process="stale"), "STALE", ReasonCode.STALE_STATE, Severity.ERROR),
]


def test_idle_after_only_failures_is_not_reported_as_healthy_empty_queue():
    failed = [{"work_item_id": "A", "reason": "boom", "at": "2026-01-01T00:00:02+00:00"}]
    done = [{"work_item_id": "B", "at": "2026-01-01T00:00:01+00:00"}]
    op = derive_operator_status(_status(state="waiting_for_work", last_error="boom", recent_failed=failed))
    assert (op.label, op.reason_code, op.severity) == ("IDLE", ReasonCode.FACTORY_FAILURE, Severity.ATTENTION)
    assert op.next_action.command == "howlplane logs --errors"
    # A later success supersedes the failure.
    healthy = derive_operator_status(
        _status(state="waiting_for_work", recent_failed=failed, recent_completed=done + [
            {"work_item_id": "C", "at": "2026-01-01T00:00:03+00:00"}]))
    assert (healthy.reason_code, healthy.severity) == (ReasonCode.WAITING_FOR_WORK, Severity.OK)


@pytest.mark.parametrize("status,label,code,severity", CASES)
def test_states_map_to_label_reason_and_severity(status, label, code, severity):
    op = derive_operator_status(status)
    assert (op.label, op.reason_code, op.severity) == (label, code, severity)
    assert op.to_dict()["schema"] == OPERATOR_STATUS_SCHEMA
    assert op.to_dict()["reason_code"] == code.value


def test_owner_required_flags_owner_and_abnormal_text_explains_why_and_what_next():
    status = CASES[4][0]
    op = derive_operator_status(status)
    assert op.owner_required
    text = "\n".join(render_operator_text(op, status))
    assert "OWNER REQUIRED" in text and "Reason" in text and "HOWL-014" in text
    assert "Next" in text and "howlplane status" not in text


def test_healthy_text_hides_internals_and_says_no_action():
    status = _status(state="dispatching", current_work_item_id="HOWL-014", project="grocery")
    text = "\n".join(render_operator_text(derive_operator_status(status), status))
    assert "Reason:" not in text and "dispatch" not in text.lower().replace("dispatching", "")
    assert "No action required." in text
    assert "Project" in text and "grocery" in text


def test_every_suggested_command_is_a_real_howlplane_command():
    parser = build_parser()
    for status, *_ in CASES:
        action = derive_operator_status(status).next_action
        if action and action.command:
            argv = action.command.split()
            assert argv[0] == "howlplane"
            parser.parse_args(argv[1:])


def test_waiting_for_provider_missing_executable_requires_owner():
    st = derive_operator_status(_status(
        state="waiting_for_provider",
        last_error="MISSING_EXECUTABLE: executable 'codex' not found on PATH"))
    assert st.owner_required is True
    assert st.reason_code is ReasonCode.MISSING_EXECUTABLE
    assert "'codex'" in st.summary
    assert st.next_action.command == "howlplane doctor --agents"


def test_waiting_for_provider_without_missing_executable_stays_automatic():
    st = derive_operator_status(_status(state="waiting_for_provider"))
    assert st.owner_required is False


def test_broken_pipe_is_quiet_not_an_internal_error(monkeypatch, capsys, tmp_path):
    """`howlplane logs | head` closes the pipe; that must not be reported as a HowlPlane bug."""
    from howlplane.control_plane import cli

    def closed_pipe(_args):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setitem(cli.HANDLERS, "logs", closed_pipe)
    monkeypatch.setenv("HOWLPLANE_DIAGNOSTICS_DIR", str(tmp_path))
    monkeypatch.setattr(cli.os, "dup2", lambda *a, **k: None)
    assert cli.main(["logs"]) == 141
    assert "INTERNAL_ERROR" not in capsys.readouterr().err
    assert not list(tmp_path.iterdir())

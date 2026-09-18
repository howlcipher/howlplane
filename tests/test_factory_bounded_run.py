#!/usr/bin/env python3
"""Tests for bounded Factory execution mode (--max-work-items).

Regression coverage for the 2026-09-15 dogfooding incident where a single-item
canary deferred due to NO_ELIGIBLE_PROVIDER_REMAINING and the continuous Factory
immediately selected a second independent work item.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from tests._factory_test_helpers import (
    RecordingDispatcher,
    make_supervisor as _make_supervisor,
    ready_work_item as _ready_work_item,
)

from src.control_plane.factory.dispatcher import DispatchOutcome
from src.control_plane.factory.service import load_process, process_status
from src.control_plane.factory.supervisor_state import SupervisorState, SupervisorStateStore
from src.control_plane.factory.work_item import WorkItemOrigin, WorkItemState


def _make_bounded_supervisor(tmp_path, *, max_work_items, dispatcher=None, pool=None):
    supervisor, now, sleeps = _make_supervisor(tmp_path, dispatcher=dispatcher, pool=pool)
    supervisor._state_record.run_mode = "bounded"
    supervisor._state_record.max_work_items = max_work_items
    supervisor._state_record.bounded_run_started_at = now["t"].isoformat()
    supervisor._persist()
    return supervisor, now, sleeps


@pytest.mark.parametrize(
    "outcome_kwargs, expected_reason",
    [
        (
            {
                "success": True,
                "next_work_item_state": WorkItemState.SHIPPED,
                "reason": "governed_lifecycle_completed",
            },
            "bounded_work_item_completed",
        ),
        (
            {
                "success": False,
                "next_work_item_state": WorkItemState.FAILED,
                "reason": "orchestrator_final_state:failed",
            },
            "bounded_work_item_failed",
        ),
        (
            {
                "success": False,
                "next_work_item_state": WorkItemState.DEFERRED,
                "reason": "NO_ELIGIBLE_PROVIDER_REMAINING: tried all",
                "provider_unavailable": True,
                "failure_class": "PROVIDER_EXHAUSTED",
            },
            "bounded_no_eligible_provider",
        ),
        (
            {
                "success": False,
                "next_work_item_state": WorkItemState.AWAITING_OWNER,
                "reason": "parked_awaiting_human_authority",
                "requires_authority": True,
                "blocker": "authority_boundary",
            },
            "bounded_work_item_authority_blocked",
        ),
        (
            {
                "success": False,
                "next_work_item_state": WorkItemState.BLOCKED,
                "reason": "BLOCKED: missing dependency",
                "blocker": "dep-1",
            },
            "bounded_work_item_blocked",
        ),
    ],
)
def test_bounded_terminal_outcomes_stop_supervisor(tmp_path, outcome_kwargs, expected_reason):
    """Any terminal/bounded outcome halts after 1 work item without dispatching item B."""
    dispatcher = RecordingDispatcher([DispatchOutcome(work_item_id="placeholder", **outcome_kwargs)])
    supervisor, now, _ = _make_bounded_supervisor(tmp_path, max_work_items=1, dispatcher=dispatcher)
    item_a = _ready_work_item(supervisor.work_item_store, title="item-a", identity_keys=["a"], source_rank=1)
    item_b = _ready_work_item(supervisor.work_item_store, title="item-b", identity_keys=["b"], source_rank=2)

    tick_res = supervisor.tick()
    assert dispatcher.calls == [item_a.work_item_id]
    assert tick_res.state == SupervisorState.STOPPED
    assert supervisor.state_record.work_items_dispatched == 1
    assert supervisor.state_record.bounded_run_stop_reason == expected_reason

    # Item B remains undispatched
    assert supervisor.work_item_store.load(item_b.work_item_id).state == WorkItemState.READY


def test_bounded_failover_and_remediation_count_single_work_item(tmp_path):
    """Provider failover, retries, and remediation cycles within the same work item do not consume budget."""
    outcome = DispatchOutcome(
        success=True,
        work_item_id="item-x",
        next_work_item_state=WorkItemState.SHIPPED,
        reason="governed_lifecycle_completed",
    )
    dispatcher = RecordingDispatcher([outcome])
    supervisor, _, _ = _make_bounded_supervisor(tmp_path, max_work_items=1, dispatcher=dispatcher)
    item = _ready_work_item(supervisor.work_item_store, title="complex-item", identity_keys=["complex"])
    item.attempts = 3
    supervisor.work_item_store.save_object(item)

    supervisor.tick()
    assert supervisor.state_record.work_items_dispatched == 1
    assert supervisor.state_record.state == SupervisorState.STOPPED


def test_bounded_budget_multiple_items(tmp_path):
    """max_work_items=2 allows exactly two distinct dispatches before stopping."""
    dispatcher = RecordingDispatcher([
        DispatchOutcome(success=True, work_item_id="1", next_work_item_state=WorkItemState.SHIPPED, reason="done"),
        DispatchOutcome(success=True, work_item_id="2", next_work_item_state=WorkItemState.SHIPPED, reason="done"),
    ])
    supervisor, _, _ = _make_bounded_supervisor(tmp_path, max_work_items=2, dispatcher=dispatcher)
    w1 = _ready_work_item(supervisor.work_item_store, title="w1", identity_keys=["1"], source_rank=1)
    w2 = _ready_work_item(supervisor.work_item_store, title="w2", identity_keys=["2"], source_rank=2)
    w3 = _ready_work_item(supervisor.work_item_store, title="w3", identity_keys=["3"], source_rank=3)

    r1 = supervisor.tick()
    assert r1.state != SupervisorState.STOPPED
    assert supervisor.state_record.work_items_dispatched == 1

    r2 = supervisor.tick()
    assert r2.state == SupervisorState.STOPPED
    assert supervisor.state_record.work_items_dispatched == 2
    assert dispatcher.calls == [w1.work_item_id, w2.work_item_id]
    assert supervisor.work_item_store.load(w3.work_item_id).state == WorkItemState.READY


def test_continuous_mode_remains_unbounded(tmp_path):
    """Without max_work_items, normal continuous dispatching continues unchanged."""
    dispatcher = RecordingDispatcher([
        DispatchOutcome(success=True, work_item_id="1", next_work_item_state=WorkItemState.SHIPPED, reason="done"),
        DispatchOutcome(success=True, work_item_id="2", next_work_item_state=WorkItemState.SHIPPED, reason="done"),
    ])
    supervisor, _, _ = _make_supervisor(tmp_path, dispatcher=dispatcher)
    assert supervisor.state_record.run_mode == "continuous"
    assert supervisor.state_record.max_work_items is None

    _ready_work_item(supervisor.work_item_store, title="c1", identity_keys=["c1"], source_rank=1)
    _ready_work_item(supervisor.work_item_store, title="c2", identity_keys=["c2"], source_rank=2)

    supervisor.tick()
    res2 = supervisor.tick()
    assert res2.state != SupervisorState.STOPPED
    assert len(dispatcher.calls) == 2


def test_restart_after_bounded_completion_prevents_second_dispatch(tmp_path):
    """Resuming or running a completed bounded campaign stops without dispatching further items."""
    dispatcher = RecordingDispatcher([
        DispatchOutcome(success=True, work_item_id="1", next_work_item_state=WorkItemState.SHIPPED, reason="done"),
    ])
    supervisor, _, _ = _make_bounded_supervisor(tmp_path, max_work_items=1, dispatcher=dispatcher)
    _ready_work_item(supervisor.work_item_store, title="r1", identity_keys=["r1"], source_rank=1)
    _ready_work_item(supervisor.work_item_store, title="r2", identity_keys=["r2"], source_rank=2)

    supervisor.tick()
    assert supervisor.state_record.state == SupervisorState.STOPPED

    # Second supervisor instance pointing to the same state directory
    supervisor2, _, _ = _make_supervisor(tmp_path)
    supervisor2.resume()
    res = supervisor2.tick()
    assert res.state == SupervisorState.STOPPED
    assert len(dispatcher.calls) == 1


def test_crash_recovery_preserves_work_item_accounting(tmp_path):
    """Reconciling an in-flight crash does not double-count the recovered work item."""
    supervisor, _, _ = _make_bounded_supervisor(tmp_path, max_work_items=1)
    item = _ready_work_item(supervisor.work_item_store, title="crash-item", identity_keys=["ci"])

    # Simulate in-flight dispatch state
    supervisor._state_record.state = SupervisorState.DISPATCHING
    supervisor._state_record.current_work_item_id = item.work_item_id
    supervisor._state_record.work_items_dispatched = 1
    supervisor._state_record.bounded_dispatched_ids = [item.work_item_id]
    supervisor._persist()

    store = SupervisorStateStore(tmp_path / "supervisor")
    reconciled = store.load(reconcile_restart=True)
    assert reconciled.work_items_dispatched == 1
    assert reconciled.bounded_dispatched_ids == [item.work_item_id]


def test_bounded_status_and_reports(tmp_path):
    """Verify status payload and deterministic report artifacts (JSON & Markdown)."""
    outcome = DispatchOutcome(
        success=False,
        work_item_id="WI-howlframe-c03eb7b89b922a16",
        next_work_item_state=WorkItemState.DEFERRED,
        reason="NO_ELIGIBLE_PROVIDER_REMAINING: tried AGY, Claude, Codex",
        provider_unavailable=True,
        failure_class="PROVIDER_EXHAUSTED",
        failure_code="NO_ELIGIBLE_PROVIDER_REMAINING",
    )
    dispatcher = RecordingDispatcher([outcome])
    supervisor, _, _ = _make_bounded_supervisor(tmp_path, max_work_items=1, dispatcher=dispatcher)

    # Incident-shaped work item
    item_a = _ready_work_item(
        supervisor.work_item_store,
        title="incident-target",
        identity_keys=["incident-target"],
        source_rank=1,
    )
    item_b = _ready_work_item(
        supervisor.work_item_store,
        title="incident-bystander",
        identity_keys=["incident-bystander"],
        source_rank=2,
    )

    supervisor.run()

    # Status validation
    status = supervisor.status()
    assert status["run_mode"] == "bounded"
    assert status["max_work_items"] == 1
    assert status["work_items_dispatched"] == 1
    assert status["state"] == SupervisorState.STOPPED
    assert status["bounded_run_stop_reason"] == "bounded_no_eligible_provider"
    assert dispatcher.calls == [item_a.work_item_id]
    assert supervisor.work_item_store.load(item_b.work_item_id).state == WorkItemState.READY

    # Report validation
    reports_dir = tmp_path / "reports"
    json_reports = list(reports_dir.glob("bounded_run_*.json"))
    md_reports = list(reports_dir.glob("bounded_run_*.md"))
    assert len(json_reports) == 1
    assert len(md_reports) == 1

    report_content = json.loads(json_reports[0].read_text(encoding="utf-8"))
    assert report_content["schema"] == "howlplane.factory.bounded_run_report/v1"
    assert report_content["run_mode"] == "bounded"
    assert report_content["bounded_run_stop_reason"] == "bounded_no_eligible_provider"
    assert item_a.work_item_id in report_content["work_item_final_states"]

    md_content = md_reports[0].read_text(encoding="utf-8")
    assert "HowlPlane Factory Bounded Run Report" in md_content
    assert "bounded_no_eligible_provider" in md_content


class TimeAdvancingDispatcher(RecordingDispatcher):
    """Dispatcher that simulates duration by advancing the virtual clock."""

    def __init__(self, outcomes, advance_seconds, now_dict):
        super().__init__(outcomes)
        self.advance_seconds = advance_seconds
        self.now_dict = now_dict

    def dispatch(self, item, dispatch_id, task_id):
        self.now_dict["t"] += timedelta(seconds=self.advance_seconds)
        return super().dispatch(item, dispatch_id, task_id)


def _record_impl_attempt(
    run_dir: Path,
    folder_name: str,
    *,
    resource_id: str,
    success: bool,
    failure_class: str = None,
    capacity_after: dict = None,
    error_message: str = None,
    duration_seconds: int = None,
    timestamp: str = None,
) -> None:
    p_dir = run_dir / "implementation" / "attempts" / folder_name
    p_dir.mkdir(parents=True, exist_ok=True)
    rec = {"resource_id": resource_id, "success": success}
    if failure_class:
        rec["failure_class"] = failure_class
    if capacity_after:
        rec["capacity_after"] = capacity_after
    (p_dir / "attempt_record.json").write_text(json.dumps(rec), encoding="utf-8")

    res = {"agent_id": resource_id, "success": success}
    if error_message:
        res["error_message"] = error_message
    if duration_seconds is not None:
        res["duration_seconds"] = duration_seconds
    if timestamp:
        res["timestamp"] = timestamp
    (p_dir / "result.json").write_text(json.dumps(res), encoding="utf-8")


def _setup_campaign_and_process(
    tmp_path,
    *,
    campaign_id,
    supervisor_state=None,
    run_mode="continuous",
    bounded_stop_reason=None,
    stopped_reason=None,
    pid=99999998,
    process_create_time=0.0,
    hostname="localhost",
    status="running",
):
    from types import SimpleNamespace
    from src.control_plane.factory.campaign import FactoryCampaign
    from src.control_plane.factory.service import FactoryProcessRecord, _save_process
    from src.control_plane.factory.supervisor_state import SupervisorStateStore

    state_dir = tmp_path / "state"
    target_dir = tmp_path / "target"
    state_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    repo = SimpleNamespace(campaign_id=campaign_id, root=tmp_path)
    campaign = FactoryCampaign(repo, state_dir, target_dir)

    if supervisor_state is not None:
        store = SupervisorStateStore(state_dir / "supervisor")
        rec = store.load()
        rec.state = supervisor_state
        rec.run_mode = run_mode
        if bounded_stop_reason:
            rec.bounded_run_stop_reason = bounded_stop_reason
        if stopped_reason:
            rec.stopped_reason = stopped_reason
        store.save(rec)

    proc = FactoryProcessRecord(
        pid=pid,
        process_create_time=process_create_time,
        hostname=hostname,
        backend="direct",
        command=["howlplane", "factory", "run"],
        started_at="2026-08-30T12:00:00+00:00",
        log_path=str(state_dir / "logs" / "factory.log"),
        status=status,
    )
    _save_process(campaign, proc)
    return campaign


def _load_bounded_reports(tmp_path: Path):
    reports_dir = tmp_path / "reports"
    json_files = list(reports_dir.glob("bounded_run_*.json"))
    md_files = list(reports_dir.glob("bounded_run_*.md"))
    assert len(json_files) == 1 and len(md_files) == 1
    report_dict = json.loads(json_files[0].read_text(encoding="utf-8"))
    md_text = md_files[0].read_text(encoding="utf-8")
    return report_dict, md_text


def _shipped_outcome() -> DispatchOutcome:
    return DispatchOutcome(
        success=True,
        work_item_id="placeholder",
        next_work_item_state=WorkItemState.SHIPPED,
        reason="governed_lifecycle_completed",
    )


def _exhausted_outcome(reason: str) -> DispatchOutcome:
    return DispatchOutcome(
        success=False,
        work_item_id="placeholder",
        next_work_item_state=WorkItemState.DEFERRED,
        reason=reason,
        provider_unavailable=True,
        failure_class="PROVIDER_EXHAUSTED",
        failure_code="NO_ELIGIBLE_PROVIDER_REMAINING",
    )


def test_accurate_completion_timestamp_beyond_first_tick(tmp_path):
    """Report completion timestamp reflects post-dispatch time and provider activity."""
    supervisor, now, _ = _make_bounded_supervisor(tmp_path, max_work_items=1)
    supervisor.dispatcher = TimeAdvancingDispatcher(
        [_exhausted_outcome("NO_ELIGIBLE_PROVIDER_REMAINING: tried agy")],
        advance_seconds=3600,
        now_dict=now,
    )

    item = _ready_work_item(supervisor.work_item_store, title="timed-canary", identity_keys=["timed"])
    attempt_ended = now["t"] + timedelta(seconds=3500)
    _record_impl_attempt(
        tmp_path / ".task_runs" / item.work_item_id,
        "01-agy",
        resource_id="agy",
        success=False,
        failure_class="TIMEOUT",
        capacity_after={"agy": "depleted"},
        error_message="Command timed out after 3500s",
        duration_seconds=1800,
        timestamp=attempt_ended.isoformat(),
    )

    tick_res = supervisor.tick()
    assert tick_res.state == SupervisorState.STOPPED

    report_content, md_content = _load_bounded_reports(tmp_path)
    started_at = report_content["bounded_run_started_at"]
    completed_at = report_content["bounded_run_completed_at"]
    duration_seconds = report_content["duration_seconds"]

    started_dt = datetime.fromisoformat(started_at)
    completed_dt = datetime.fromisoformat(completed_at)
    attempt_ended_dt = datetime.fromisoformat(report_content["provider_attempts"][0]["ended_at"])

    # Prove started_at < attempt_ended_at <= completed_at
    assert started_dt < attempt_ended_dt <= completed_dt
    # JSON and MD duration agreement
    assert duration_seconds == (completed_dt - started_dt).total_seconds()
    assert duration_seconds == 3600.0

    assert f"| Started | {started_at} |" in md_content
    assert f"| Completed | {completed_at} |" in md_content
    from src.control_plane.progress import format_elapsed
    assert f"| Duration | {format_elapsed(duration_seconds)} |" in md_content


def test_provider_attempt_chain_failed_run(tmp_path):
    """Detailed attempt chain captured for multi-provider failed run."""
    blocked_msg = "NO_ELIGIBLE_PROVIDER_REMAINING: tried agy, claude_code"
    supervisor, now, _ = _make_bounded_supervisor(
        tmp_path,
        max_work_items=1,
        dispatcher=RecordingDispatcher([_exhausted_outcome(blocked_msg)]),
    )

    item = _ready_work_item(supervisor.work_item_store, title="multi-fail", identity_keys=["multi-fail"])
    item.admission_blocked_reason = blocked_msg
    supervisor.work_item_store.save_object(item)

    run_dir = tmp_path / ".task_runs" / item.work_item_id
    _record_impl_attempt(
        run_dir,
        "01-agy",
        resource_id="agy",
        success=False,
        failure_class="TIMEOUT",
        capacity_after={"agy": "depleted"},
        error_message="AGY timed out",
        duration_seconds=300,
        timestamp=(now["t"] + timedelta(seconds=300)).isoformat(),
    )
    _record_impl_attempt(
        run_dir,
        "02-claude_code",
        resource_id="claude_code",
        success=False,
        failure_class="AUTH_FAILURE",
        capacity_after={"claude_code": "disabled"},
        error_message="Token invalid",
        duration_seconds=120,
        timestamp=(now["t"] + timedelta(seconds=420)).isoformat(),
    )

    supervisor.tick()

    report, md_content = _load_bounded_reports(tmp_path)
    attempts = report["provider_attempts"]
    assert len(attempts) == 2

    assert attempts[0]["resource_id"] == "agy"
    assert attempts[0]["role"] == "implementation"
    assert attempts[0]["outcome"] == "TIMEOUT"
    assert attempts[0]["provider_state_after"] == "depleted"
    assert attempts[0]["reason"] == "AGY timed out"
    assert attempts[0]["duration_seconds"] == 300

    assert attempts[1]["resource_id"] == "claude_code"
    assert attempts[1]["role"] == "implementation"
    assert attempts[1]["outcome"] == "AUTH_FAILURE"
    assert attempts[1]["provider_state_after"] == "disabled"
    assert attempts[1]["reason"] == "Token invalid"
    assert attempts[1]["duration_seconds"] == 120

    for expected_snippet in [
        "## Provider Attempt Chain",
        "1. agy",
        "- outcome: TIMEOUT",
        "- provider state after attempt: depleted",
        "- failure/retry reason: AGY timed out",
        "2. claude_code",
        "- outcome: AUTH_FAILURE",
        "- provider state after attempt: disabled",
        "- failure/retry reason: Token invalid",
        "Final work-item outcome:\nNO_ELIGIBLE_PROVIDER_REMAINING: tried agy, claude_code",
    ]:
        assert expected_snippet in md_content


def test_provider_attempt_chain_failover_success(tmp_path):
    """Failover from failed provider to successful provider recorded accurately."""
    supervisor, now, _ = _make_bounded_supervisor(
        tmp_path,
        max_work_items=1,
        dispatcher=RecordingDispatcher([_shipped_outcome()]),
    )

    item = _ready_work_item(supervisor.work_item_store, title="failover-item", identity_keys=["failover"])
    item_run_path = tmp_path / ".task_runs" / item.work_item_id

    _record_impl_attempt(
        item_run_path,
        "01-agy",
        resource_id="agy",
        success=False,
        failure_class="RATE_LIMITED",
        capacity_after={"agy": "cooldown"},
        error_message="Rate limit exceeded",
        duration_seconds=60,
        timestamp=(now["t"] + timedelta(seconds=60)).isoformat(),
    )
    _record_impl_attempt(
        item_run_path,
        "02-codex",
        resource_id="codex",
        success=True,
        capacity_after={"codex": "ready"},
        duration_seconds=180,
        timestamp=(now["t"] + timedelta(seconds=240)).isoformat(),
    )

    supervisor.tick()

    rep, md_text = _load_bounded_reports(tmp_path)
    prov_attempts = rep["provider_attempts"]
    assert len(prov_attempts) == 2
    assert prov_attempts[0]["resource_id"] == "agy"
    assert prov_attempts[0]["outcome"] == "RATE_LIMITED"
    assert prov_attempts[0]["provider_state_after"] == "cooldown"
    assert prov_attempts[1]["resource_id"] == "codex"
    assert prov_attempts[1]["outcome"] == "implementation succeeded"

    for expected in [
        "## Provider Attempt Chain",
        "1. agy\n   - role: implementation\n   - started:",
        "- outcome: RATE_LIMITED",
        "2. codex\n   - role: implementation",
        "- outcome: implementation succeeded",
        "Final work-item outcome:\nshipped",
    ]:
        assert expected in md_text


def test_review_remediation_verification_reporting(tmp_path):
    """Full lifecycle with implementation, review, remediation, and verification."""
    supervisor, now, _ = _make_bounded_supervisor(
        tmp_path,
        max_work_items=1,
        dispatcher=RecordingDispatcher([_shipped_outcome()]),
    )

    item = _ready_work_item(supervisor.work_item_store, title="full-lifecycle", identity_keys=["lifecycle"])
    run_dir = tmp_path / ".task_runs" / item.work_item_id

    # 1. Implementation
    _record_impl_attempt(
        run_dir,
        "01-agy",
        resource_id="agy",
        success=True,
        duration_seconds=120,
        timestamp=(now["t"] + timedelta(seconds=120)).isoformat(),
    )

    # 2. Review
    rev_dir = run_dir / "reviews" / "review-01"
    rev_dir.mkdir(parents=True)
    (rev_dir / "result.json").write_text(
        json.dumps({
            "resource_id": "claude_code",
            "status": "findings_detected",
            "disposition": "changes_requested",
            "completed_at": (now["t"] + timedelta(seconds=180)).isoformat(),
            "duration_seconds": 60,
        }),
        encoding="utf-8",
    )

    # 3. Remediation
    rem_dir = run_dir / "remediation" / "cycle-01"
    rem_dir.mkdir(parents=True)
    (rem_dir / "result.json").write_text(
        json.dumps({
            "agent_id": "codex",
            "success": True,
            "duration_seconds": 90,
            "timestamp": (now["t"] + timedelta(seconds=270)).isoformat(),
        }),
        encoding="utf-8",
    )

    # 4. Verification
    (run_dir / "verification_result.json").write_text(
        json.dumps({
            "status": "passed",
            "started_at": (now["t"] + timedelta(seconds=275)).isoformat(),
            "completed_at": (now["t"] + timedelta(seconds=320)).isoformat(),
            "duration_seconds": 45,
        }),
        encoding="utf-8",
    )

    supervisor.tick()

    full_rep, full_md = _load_bounded_reports(tmp_path)
    stages = full_rep["provider_attempts"]
    assert len(stages) == 4

    roles = [a["role"] for a in stages]
    assert roles == ["implementation", "review", "remediation", "verification"]

    assert stages[0]["resource_id"] == "agy"
    assert stages[0]["outcome"] == "implementation succeeded"

    assert stages[1]["resource_id"] == "claude_code"
    assert stages[1]["outcome"] == "changes_requested"

    assert stages[2]["resource_id"] == "codex"
    assert stages[2]["outcome"] == "remediation succeeded"

    assert stages[3]["role"] == "verification"
    assert stages[3]["outcome"] == "passed"
    # No invented missing fields
    assert "reason" not in stages[3]
    assert "provider_state_after" not in stages[3]

    for expected in [
        "## Provider Attempt Chain",
        "1. agy\n   - role: implementation",
        "2. claude_code\n   - role: review",
        "3. codex\n   - role: remediation",
        "4. verification\n   - role: verification",
        "- outcome: passed",
    ]:
        assert expected in full_md


def _assert_campaign_process_status(campaign, expected: str):
    assert process_status(campaign) == expected
    assert load_process(campaign).status == expected


def test_clean_bounded_stop_reports_stopped(tmp_path):
    """Clean bounded stop with inactive process returns stopped, not stale."""
    campaign = _setup_campaign_and_process(
        tmp_path,
        campaign_id="canary-clean",
        supervisor_state=SupervisorState.STOPPED,
        run_mode="bounded",
        bounded_stop_reason="bounded_no_eligible_provider",
    )
    _assert_campaign_process_status(campaign, "stopped")


def test_operator_stopped_campaign_reports_stopped(tmp_path):
    """Operator stopped supervisor with inactive process returns stopped."""
    campaign = _setup_campaign_and_process(
        tmp_path,
        campaign_id="canary-operator-stop",
        supervisor_state=SupervisorState.STOPPED,
        stopped_reason="operator_stop",
    )
    _assert_campaign_process_status(campaign, "stopped")


def test_truly_stale_active_record_reports_stale(tmp_path):
    """Unclean exit with supervisor still in DISPATCHING returns stale."""
    campaign = _setup_campaign_and_process(
        tmp_path,
        campaign_id="canary-stale",
        supervisor_state=SupervisorState.DISPATCHING,
    )
    _assert_campaign_process_status(campaign, "stale")


def test_continuous_running_campaign_reports_running(tmp_path):
    """Campaign with live process returns running."""
    import os
    import socket
    from src.control_plane.factory.service import process_status
    from src.control_plane.locking import get_process_create_time

    pid = os.getpid()
    campaign = _setup_campaign_and_process(
        tmp_path,
        campaign_id="canary-live",
        supervisor_state=None,
        pid=pid,
        process_create_time=get_process_create_time(pid),
        hostname=socket.gethostname(),
    )
    assert process_status(campaign) == "running"


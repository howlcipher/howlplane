"""The supervisor records the real worker when it is selected, and logs operator events."""

import json

import pytest

from tests._factory_test_helpers import make_supervisor, ready_work_item

from howlplane.control_plane.factory.dispatcher import MarathonDispatcherAdapter
from howlplane.control_plane.factory.oplog import events_path

pytestmark = pytest.mark.unit


class _FailoverEngine:
    """Tries codex, then fails over to claude_code, as the governed engine does."""

    def __init__(self, supervisor_ref, outcome="ok"):
        self.seen = []
        self.outcome = outcome
        self.supervisor_ref = supervisor_ref

    def execute_factory_work_item(self, item, files_changed=None, dispatch_id=None):
        self.provider_observer("codex", 1)
        self.seen.append(self.supervisor_ref[0].state_record.current_provider)
        self.provider_observer("claude_code", 2)
        self.seen.append(self.supervisor_ref[0].state_record.current_provider)
        if self.outcome == "fail":
            return False, {"failure_reason": "tests failed", "provider": "claude_code"}
        return True, {"provider": "claude_code", "merged": True}


def _run(tmp_path, outcome="ok"):
    ref = []
    engine = _FailoverEngine(ref, outcome)
    supervisor, _now, _sleeps = make_supervisor(tmp_path, dispatcher=MarathonDispatcherAdapter(lambda: engine))
    ref.append(supervisor)
    ready_work_item(supervisor.work_item_store, title="do it", identity_keys=["a"])
    supervisor.tick()
    return supervisor, engine


def _events(tmp_path):
    return [json.loads(line) for line in events_path(tmp_path).read_text().splitlines()]


def test_provider_is_recorded_when_selected_and_on_failover(tmp_path):
    supervisor, engine = _run(tmp_path)
    assert engine.seen == ["codex", "claude_code"]
    entry = supervisor.state_record.dispatch_history[-1]
    assert entry["provider"] == "claude_code" and entry["provider_attempt"] == 2


def test_current_worker_is_cleared_when_no_work_is_active(tmp_path):
    supervisor, _ = _run(tmp_path)
    supervisor.state_record.clear_current_dispatch()
    assert supervisor.state_record.current_provider is None
    assert supervisor.state_record.current_dispatch_started_at is None


def test_operator_events_describe_the_dispatch(tmp_path):
    _run(tmp_path)
    codes = [e["code"] for e in _events(tmp_path)]
    for expected in ("dispatch.started", "provider.selected", "provider.failover", "dispatch.completed",
                     "merge.completed", "supervisor.state_changed"):
        assert expected in codes
    failover = next(e for e in _events(tmp_path) if e["code"] == "provider.failover")
    assert failover["severity"] == "WARNING" and failover["provider"] == "claude_code"
    assert failover["from_provider"] == "codex" and failover["dispatch_id"]


def test_failed_dispatch_is_an_error_event_with_reason(tmp_path):
    _run(tmp_path, outcome="fail")
    failed = next(e for e in _events(tmp_path) if e["code"] == "dispatch.failed")
    assert failed["severity"] == "ERROR" and failed["reason"] == "tests failed"
    assert failed["provider"] == "claude_code"


def test_old_state_records_without_worker_fields_still_load(tmp_path):
    from howlplane.control_plane.factory.supervisor_state import SupervisorStateRecord
    data = SupervisorStateRecord().to_dict()
    for key in ("current_provider", "current_provider_attempt", "current_dispatch_started_at"):
        data.pop(key)
    assert SupervisorStateRecord.from_dict(data).current_provider is None

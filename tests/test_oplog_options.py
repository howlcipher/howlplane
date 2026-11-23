"""OperationalLog keyword options added for `howlplane tui` keep Factory events unchanged."""

import json
import stat
from datetime import datetime, timezone

import pytest

from howlplane.control_plane.factory.oplog import EVENTS_FILENAME, OperationalLog

pytestmark = pytest.mark.unit

FIXED = datetime(2026, 11, 23, tzinfo=timezone.utc)


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_default_factory_event_shape_is_unchanged(tmp_path):
    log = OperationalLog(tmp_path, {"campaign_id": "C1"}, clock=lambda: FIXED)
    log.emit("WORK_DISPATCHED", "WI-1 dispatched", work_item="WI-1")
    path = tmp_path / "logs" / EVENTS_FILENAME
    assert path.read_text() == json.dumps({"ts": FIXED.isoformat(), "severity": "INFO", "code": "WORK_DISPATCHED",
                                           "message": "WI-1 dispatched", "campaign_id": "C1",
                                           "work_item": "WI-1"}) + "\n"


def test_schema_sequence_and_private_mode(tmp_path):
    log = OperationalLog(tmp_path, {"session_id": "S"}, clock=lambda: FIXED, filename="tui_events.jsonl",
                         schema="howlplane.tui_event/v1", sequenced=True, private=True)
    first = log.emit("A", "one")
    log.emit("B", "two token=abc123secret", "error")
    path = tmp_path / "logs" / "tui_events.jsonl"
    events = lines(path)
    assert first == events[0]
    assert list(events[0])[:2] == ["schema", "seq"]
    assert [e["seq"] for e in events] == [1, 2]
    assert events[1]["severity"] == "ERROR" and "abc123secret" not in events[1]["message"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_private_mode_tightens_an_existing_file(tmp_path):
    path = tmp_path / "logs" / "tui_events.jsonl"
    path.parent.mkdir()
    path.write_text("")
    path.chmod(0o644)
    OperationalLog(tmp_path, filename="tui_events.jsonl", private=True).emit("A", "x")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_no_state_dir_writes_nothing():
    assert OperationalLog(None).emit("A", "x") is None

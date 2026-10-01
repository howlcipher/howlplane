"""factory logs: one redact -> parse -> filter -> render pipeline for every source."""

import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from howlplane.control_plane.factory import logs_cli, service
from howlplane.control_plane.factory.oplog import OperationalLog, events_path
from howlplane.control_plane.presentation.style import Style

pytestmark = pytest.mark.unit

SECRETS = ["ghp_abcdefgh12345678", "sk-abcdefgh12345678", "Authorization: Bearer abc123def456ghi"]


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "load_process", lambda c: None)
    monkeypatch.setattr(logs_cli, "_color_mode", lambda: "never")
    return SimpleNamespace(state_dir=tmp_path)


def _args(**kw):
    base = dict(follow=False, lines=80, errors=False, level=None, work_item=None, provider=None,
                since=None, raw=False, json=False, verbose=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _emit_sample(state_dir):
    log = OperationalLog(state_dir, {"supervisor_id": "S1"})
    log.emit("dispatch.started", "WI-042 dispatched", work_item_id="WI-042", dispatch_id="D1")
    log.emit("provider.selected", "WI-042 running on codex", provider="codex", work_item_id="WI-042")
    log.emit("provider.unavailable", "Codex hit session limit", "WARNING", provider="codex",
             work_item_id="WI-042", failure_class="SESSION_LIMIT")
    log.emit("dispatch.failed", "WI-043 failed: tests", "ERROR", work_item_id="WI-043", provider="claude")


def test_event_structure_carries_only_known_fields(tmp_path):
    OperationalLog(tmp_path, {"supervisor_id": "S1"}).emit(
        "dispatch.started", "WI-1 dispatched", work_item_id="WI-1", task_id=None)
    event = json.loads(events_path(tmp_path).read_text())
    assert event["code"] == "dispatch.started" and event["severity"] == "INFO"
    assert event["supervisor_id"] == "S1" and event["work_item_id"] == "WI-1"
    assert "task_id" not in event and event["ts"]


def test_events_are_redacted_before_disk(tmp_path):
    OperationalLog(tmp_path).emit("x.y", f"leaked {SECRETS[0]}", reason=SECRETS[2])
    text = events_path(tmp_path).read_text()
    assert "abcdefgh" not in text and "abc123def456" not in text


def test_default_view_is_compact_and_readable(campaign, capsys):
    _emit_sample(campaign.state_dir)
    assert logs_cli.command(campaign, _args()) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 4
    assert "INFO" in out[0] and "WI-042 dispatched" in out[0]
    assert "WARNING" in out[2] and "Codex hit session limit" in out[2]
    assert "\x1b" not in "".join(out)


@pytest.mark.parametrize("kwargs,expected", [
    (dict(errors=True), ["WI-043 failed"]),
    (dict(level="WARNING"), ["session limit", "WI-043 failed"]),
    (dict(work_item="WI-042"), ["WI-042 dispatched", "running on codex", "session limit"]),
    (dict(provider="claude"), ["WI-043 failed"]),
])
def test_filters(campaign, capsys, kwargs, expected):
    _emit_sample(campaign.state_dir)
    logs_cli.command(campaign, _args(**kwargs))
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == len(expected)
    for fragment in expected:
        assert fragment in out


def test_since_filter_drops_old_events(campaign, capsys):
    old = OperationalLog(campaign.state_dir, clock=lambda: datetime.now(timezone.utc) - timedelta(hours=3))
    old.emit("old.event", "ancient")
    OperationalLog(campaign.state_dir).emit("new.event", "recent")
    logs_cli.command(campaign, _args(since="1h"))
    out = capsys.readouterr().out
    assert "recent" in out and "ancient" not in out


def test_json_output_is_clean_jsonl(campaign, capsys):
    _emit_sample(campaign.state_dir)
    logs_cli.command(campaign, _args(json=True, errors=True))
    out = capsys.readouterr().out
    assert "\x1b" not in out
    rows = [json.loads(line) for line in out.splitlines()]
    assert rows[0]["severity"] == "ERROR" and rows[0]["work_item_id"] == "WI-043"


def test_verbose_exposes_correlation_ids(campaign, capsys):
    _emit_sample(campaign.state_dir)
    logs_cli.command(campaign, _args(verbose=True, work_item="WI-042"))
    assert "dispatch_id=D1" in capsys.readouterr().out


@pytest.mark.parametrize("secret", SECRETS)
def test_tail_redacts_raw_process_log(campaign, capsys, secret):
    path = service._log_path(campaign)
    path.parent.mkdir(parents=True)
    path.write_text(f"starting\nusing {secret} for call\n")
    logs_cli.command(campaign, _args())
    out = capsys.readouterr().out
    assert "using" in out and secret.split()[-1] not in out


@pytest.mark.parametrize("secret", SECRETS)
def test_follow_path_redacts_new_lines(secret):
    out = io.StringIO()
    logs_cli.process_lines([f"later {secret}\n"], logs_cli.LogFilter(), Style(), out=out)
    assert secret.split()[-1] not in out.getvalue()


def test_systemd_journal_output_goes_through_the_same_pipeline(campaign, capsys, monkeypatch):
    record = SimpleNamespace(backend="systemd", unit_name="howlplane-factory-x")
    monkeypatch.setattr(service, "load_process", lambda c: record)

    class FakeProc:
        stdout = iter([f"journal line {SECRETS[0]}\n", "plain\n"])

        def terminate(self):
            pass
    monkeypatch.setattr(logs_cli.subprocess, "Popen", lambda *a, **k: FakeProc())
    assert logs_cli.command(campaign, _args()) == 0
    out = capsys.readouterr().out
    assert "plain" in out and "abcdefgh" not in out


def test_no_logs_yet_message(campaign, capsys):
    assert logs_cli.command(campaign, _args()) == 0
    assert "No Factory logs" in capsys.readouterr().out


def test_parse_since_forms():
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    assert logs_cli.parse_since("30m", now) == now - timedelta(minutes=30)
    assert logs_cli.parse_since("2d", now) == now - timedelta(days=2)
    with pytest.raises(ValueError):
        logs_cli.parse_since("soon")


def test_tail_reads_last_lines_from_large_file(tmp_path):
    path = tmp_path / "big.log"
    path.write_text("".join(f"line {i}\n" for i in range(50000)), encoding="utf-8")
    assert logs_cli._tail(path, 3) == ["line 49997", "line 49998", "line 49999"]
    small = tmp_path / "small.log"
    small.write_text("a\nb\nc", encoding="utf-8")
    assert logs_cli._tail(small, 10) == ["a", "b", "c"]
    assert logs_cli._tail(small, 0) == []


def test_emit_rotates_when_size_cap_reached(tmp_path, monkeypatch):
    from howlplane.control_plane.factory import oplog

    monkeypatch.setattr(oplog, "MAX_LOG_BYTES", 300)
    monkeypatch.setattr(oplog, "KEEP_ROTATED", 2)
    log = OperationalLog(tmp_path)
    for i in range(60):
        log.emit("X", f"event {i}")
    files = sorted(p.name for p in events_path(tmp_path).parent.iterdir())
    assert files == [oplog.EVENTS_FILENAME, oplog.EVENTS_FILENAME + ".1", oplog.EVENTS_FILENAME + ".2"]
    assert "event 59" in events_path(tmp_path).read_text()

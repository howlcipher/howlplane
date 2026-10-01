"""``howlplane factory logs``: the operator diagnostic view of what the Factory did.

One pipeline serves every source and mode:

    raw line -> redact -> parse -> filter -> render

so the initial tail, ``--follow``, the portable process log and the systemd
journal can never show an unredacted line. The default source is the
structured event log (``oplog``); ``--raw`` shows the process output
(``factory.log`` or the journal) instead. Provider transcripts are not logs and
are never printed here.
"""

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from howlplane.control_plane.factory.oplog import SEVERITIES, events_path
from howlplane.control_plane.presentation.redact import redact_operator_text
from howlplane.control_plane.presentation.style import Style, resolve_style

_SINCE = re.compile(r"^(\d+)\s*([smhdw])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_LEGACY_LEVEL = [
    (re.compile(r"\b(CRITICAL|FATAL)\b|Traceback \(most recent"), "CRITICAL"),
    (re.compile(r"\bERROR\b|\bFAILED\b|\bfailed\b"), "ERROR"),
    (re.compile(r"\bWARN(?:ING)?\b"), "WARNING"),
]
_LEVEL_STYLE = {"DEBUG": "muted", "INFO": "info", "WARNING": "warn", "ERROR": "error", "CRITICAL": "error"}


@dataclass
class LogEntry:
    message: str
    severity: str = "INFO"
    ts: Optional[datetime] = None
    fields: Dict[str, Any] = field(default_factory=dict)
    raw: Optional[str] = None


def parse_since(value: str, now: Optional[datetime] = None) -> datetime:
    """``90s``, ``30m``, ``1h``, ``2d``, ``1w`` or an ISO timestamp."""
    now = now or datetime.now(timezone.utc)
    match = _SINCE.match(value.strip().lower())
    if match:
        return now - timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"--since must look like 30m, 1h, 2d or an ISO timestamp, not {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_line(line: str) -> LogEntry:
    """Structured event when the line is one, otherwise a legacy text line."""
    text = line.rstrip("\n")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict) and "code" in data and "message" in data:
            ts = None
            try:
                ts = datetime.fromisoformat(str(data.get("ts")))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
            severity = str(data.get("severity", "INFO")).upper()
            fields = {k: v for k, v in data.items() if k not in ("ts", "severity", "message")}
            return LogEntry(str(data["message"]), severity if severity in SEVERITIES else "INFO", ts, fields)
    severity = "INFO"
    for pattern, level in _LEGACY_LEVEL:
        if pattern.search(text):
            severity = level
            break
    return LogEntry(text, severity, None, {}, raw=text)


@dataclass
class LogFilter:
    min_level: str = "DEBUG"
    work_item: Optional[str] = None
    provider: Optional[str] = None
    since: Optional[datetime] = None

    def accepts(self, entry: LogEntry) -> bool:
        if SEVERITIES.index(entry.severity) < SEVERITIES.index(self.min_level):
            return False
        if self.since and entry.ts and entry.ts < self.since:
            return False
        if self.work_item and not _matches(entry, "work_item_id", self.work_item):
            return False
        if self.provider and not _matches(entry, "provider", self.provider):
            return False
        return True


def _matches(entry: LogEntry, key: str, wanted: str) -> bool:
    wanted = wanted.lower()
    value = entry.fields.get(key)
    if value is not None:
        return wanted in str(value).lower()
    return entry.raw is not None and wanted in entry.raw.lower()


def render_entry(entry: LogEntry, style: Style, verbose: bool = False) -> str:
    when = entry.ts.astimezone().strftime("%H:%M:%S") if entry.ts else " " * 8
    level = entry.severity.ljust(8)
    line = f"{when}  {style.paint(_LEVEL_STYLE[entry.severity], level)} {entry.message}"
    context = []
    if entry.fields.get("work_item_id") and entry.fields["work_item_id"] not in entry.message:
        context.append(str(entry.fields["work_item_id"]))
    if entry.fields.get("provider") and entry.fields["provider"] not in entry.message:
        context.append(str(entry.fields["provider"]))
    if context:
        line += style.muted(f"  [{' '.join(context)}]")
    if verbose and entry.fields:
        extra = " ".join(f"{k}={v}" for k, v in entry.fields.items() if k != "code")
        line += "\n" + " " * 10 + style.muted(f"{entry.fields.get('code', '')} {extra}".strip())
    return line


def _entry_json(entry: LogEntry) -> str:
    data = {"ts": entry.ts.isoformat() if entry.ts else None, "severity": entry.severity,
            "message": entry.message, **entry.fields}
    return json.dumps({k: v for k, v in data.items() if v is not None})


def process_lines(lines: Iterable[str], log_filter: LogFilter, style: Style, as_json: bool = False,
                  verbose: bool = False, out=None) -> int:
    """The single redact -> parse -> filter -> render pipeline. Returns lines shown."""
    out = out or sys.stdout
    shown = 0
    for line in lines:
        entry = parse_line(redact_operator_text(line))
        if not log_filter.accepts(entry):
            continue
        print(_entry_json(entry) if as_json else render_entry(entry, style, verbose), file=out, flush=True)
        shown += 1
    return shown


# Sources ---------------------------------------------------------------------

def _tail(path: Path, count: int) -> List[str]:
    """Last `count` lines, read by seeking back from the end of the file."""
    if count <= 0:
        return []
    block = 65536
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        pos, data = size, b""
        while pos > 0 and data.count(b"\n") <= count:
            step = min(block, pos)
            pos -= step
            handle.seek(pos)
            data = handle.read(step) + data
    lines = data.decode("utf-8", errors="replace").splitlines()
    if pos > 0 and lines:
        lines = lines[1:]  # first line may be partial
    return lines[-count:]


def _follow_file(path: Path) -> Iterator[str]:
    """Yield new lines forever; survives truncation and rotation."""
    handle = path.open("r", encoding="utf-8", errors="replace")
    handle.seek(0, os.SEEK_END)
    inode = os.fstat(handle.fileno()).st_ino
    try:
        while True:
            line = handle.readline()
            if line:
                yield line
                continue
            time.sleep(0.25)
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_ino != inode or stat.st_size < handle.tell():
                handle.close()
                handle = path.open("r", encoding="utf-8", errors="replace")
                inode = os.fstat(handle.fileno()).st_ino
    finally:
        handle.close()


def _journal(unit: str, count: int, follow: bool) -> Iterator[str]:
    command = ["journalctl", "--user", "--unit", unit, "--no-pager", "-o", "cat", "-n", str(count)]
    if follow:
        command.append("--follow")
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, errors="replace")
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            yield line
    finally:
        proc.terminate()


def command(campaign: Any, args: Any) -> int:
    """Run ``factory logs`` for a resolved campaign."""
    from howlplane.control_plane.factory.service import _log_path, load_process

    as_json = bool(getattr(args, "json", False))
    style = Style() if as_json else resolve_style(sys.stdout, _color_mode())
    min_level = "ERROR" if getattr(args, "errors", False) else (getattr(args, "level", None) or "DEBUG")
    log_filter = LogFilter(
        min_level=min_level.upper(),
        work_item=getattr(args, "work_item", None),
        provider=getattr(args, "provider", None),
        since=parse_since(args.since) if getattr(args, "since", None) else None,
    )
    count = getattr(args, "lines", 80)
    follow = bool(getattr(args, "follow", False))
    verbose = bool(getattr(args, "verbose", False))

    events = events_path(campaign.state_dir)
    record = load_process(campaign)
    use_raw = bool(getattr(args, "raw", False)) or not events.exists()
    if not use_raw:
        source, follow_path = _tail(events, count), events
    elif record and record.backend == "systemd" and record.unit_name:
        source, follow_path = None, None
    else:
        raw_path = _log_path(campaign)
        if not raw_path.exists():
            _print_empty(as_json)
            return 0
        source, follow_path = _tail(raw_path, count), raw_path

    try:
        if source is None:
            shown = process_lines(_journal(record.unit_name, count, follow), log_filter, style, as_json, verbose)
        else:
            shown = process_lines(source, log_filter, style, as_json, verbose)
            if follow:
                process_lines(_follow_file(follow_path), log_filter, style, as_json, verbose)
    except KeyboardInterrupt:
        return 0
    if not shown and not as_json:
        print("No matching log entries." if (log_filter.work_item or log_filter.provider or log_filter.since
                                              or min_level != "DEBUG") else "No Factory log entries yet.")
    return 0


def _color_mode() -> Optional[str]:
    from howlplane.control_plane import cli
    return getattr(cli, "_COLOR_MODE", None)


def _print_empty(as_json: bool) -> None:
    if not as_json:
        print("No Factory logs have been recorded for this campaign.")

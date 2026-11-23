"""Operational event log: short structured facts about what HowlPlane did.

One JSON object per line in ``<state_dir>/logs/factory_events.jsonl``. This is
the operator-facing record ("WI-042 dispatched", "Codex hit its session limit").
It is deliberately separate from provider transcripts and run evidence, which
stay under the target repository's ``.task_runs`` directories and are never
copied here. Only fields that exist are written, and every free-text value is
redacted before it reaches disk.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Union

from howlplane.control_plane.presentation.redact import redact_operator_text

EVENTS_FILENAME = "factory_events.jsonl"
SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_MAX_TEXT = 500
MAX_LOG_BYTES = 10 * 1024 * 1024
KEEP_ROTATED = 5

logger = logging.getLogger("howlplane.factory.oplog")


def events_path(state_dir: Union[str, Path]) -> Path:
    return Path(state_dir) / "logs" / EVENTS_FILENAME


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        text = redact_operator_text(value)
        return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clean(str(value))


def _rotate(path: Path, max_bytes: int, keep: int) -> None:
    """Shift path -> path.1 -> ... -> path.<keep> once path reaches max_bytes."""
    try:
        if path.stat().st_size < max_bytes:
            return
        oldest = path.with_name(f"{path.name}.{keep}")
        oldest.unlink(missing_ok=True)
        for i in range(keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{i}")
            if src.exists():
                src.rename(path.with_name(f"{path.name}.{i + 1}"))
        path.rename(path.with_name(f"{path.name}.1"))
    except OSError:
        logger.debug("could not rotate operational log", exc_info=True)


class OperationalLog:
    """Append-only event writer. Failure to log never changes control-plane behavior.

    The keyword options are additive and off by default, so Factory events keep
    their exact shape. ``schema`` names a versioned event contract and is
    written first; ``sequenced`` adds a per-writer ``seq`` that increases by one
    per emitted event; ``private`` creates the directory 0700 and the file 0600
    regardless of umask; ``filename`` replaces ``factory_events.jsonl``.
    """

    def __init__(self, state_dir: Optional[Union[str, Path]], context: Optional[Dict[str, Any]] = None,
                 clock=lambda: datetime.now(timezone.utc), *, filename: str = EVENTS_FILENAME,
                 schema: Optional[str] = None, sequenced: bool = False, private: bool = False):
        self.path = Path(state_dir) / "logs" / filename if state_dir else None
        self.context = {k: v for k, v in (context or {}).items() if v is not None}
        self._clock = clock
        self._schema = schema
        self._sequenced = sequenced
        self._private = private
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, code: str, message: str, severity: str = "INFO", **fields: Any) -> Optional[Dict[str, Any]]:
        """Writes one event and returns it as written, or None when nothing was written."""
        if self.path is None:
            return None
        severity = severity.upper() if severity.upper() in SEVERITIES else "INFO"
        with self._lock:
            event: Dict[str, Any] = {"schema": self._schema} if self._schema else {}
            if self._sequenced:
                self._seq += 1
                event["seq"] = self._seq
            event.update({"ts": self._clock().isoformat(), "severity": severity, "code": code,
                          "message": message})
            event.update(self.context)
            event.update({k: v for k, v in fields.items() if v is not None})
            try:
                if self._private:
                    self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.chmod(self.path.parent, 0o700)
                else:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                cleaned = {k: _clean(v) for k, v in event.items()}
                line = json.dumps(cleaned, sort_keys=False)
                _rotate(self.path, MAX_LOG_BYTES, KEEP_ROTATED)
                if self._private:
                    fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                else:
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                return cleaned
            except (OSError, TypeError, ValueError):
                logger.debug("could not write operational event %s", code, exc_info=True)
                return None

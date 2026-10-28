#!/usr/bin/env python3
"""
evidence_ledger.py

Durable, history-preserving, append-only evidence ledger with automatic redaction
of secrets, credentials, and sensitive data.

The active file rotates into gzip-compressed archive segments once it reaches a
size threshold. Rotation never drops history: every read returns the archived
segments, oldest first, followed by the active file.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timedelta, timezone
import functools
import gzip
import json
import logging
import os
from pathlib import Path
import re
import shutil
from typing import Any, Dict, Iterator, List, Optional, Tuple
import uuid

try:
    import fcntl
except ImportError:  # Windows has no flock, so the ledger is never rotated there.
    fcntl = None

from jsonschema import Draft202012Validator, FormatChecker

from howlplane.control_plane.resources import CHECKOUT_ROOT, data_file, is_checkout
from howlplane.control_plane.task_spec import DataClassSerializationMixin

EVIDENCE_ENTRY_SCHEMA_VERSION = "ai.evidence_entry/v1"

_SCHEMA_PATH = data_file("schemas", "evidence-entry.schema.json")
_LOG = logging.getLogger(__name__)

# An append that leaves the active file at or above this size rotates it.
DEFAULT_ROTATE_BYTES = 64 * 1024 * 1024
# Fixed width, so archive names sort in the order they were written.
_ARCHIVE_STAMP = "%Y%m%dT%H%M%S%fZ"
# Every file a ledger named *.jsonl owns: the active file and its staged,
# archived and partially written segments.
_LEDGER_FILE = re.compile(r"\.jsonl(\.\d{8}T\d{12}Z(\.gz)?(\.partial)?)?$")


def is_ledger_file(path: str) -> bool:
    """Whether ``path`` names a file owned by an evidence ledger.

    Repository fingerprints leave these out, so a ledger kept inside a
    repository never reads as a code change, however it has rotated.
    """
    return bool(_LEDGER_FILE.search(path))


class EvidenceSchemaError(ValueError):
    """An evidence entry violates the published schema. This is a control-plane defect."""


class EvidenceCorruptionError(ValueError):
    """The ledger holds a malformed or schema-invalid record (strict read)."""


@dataclass(frozen=True)
class LedgerDiagnostic:
    """One unreadable or schema-invalid ledger line."""

    line_number: int
    kind: str  # "malformed" (unparseable, skipped) or "schema_invalid" (readable, flagged)
    reason: str
    source: str = ""  # file name of the segment holding the line; line_number counts within it


@functools.lru_cache(maxsize=1)
def _entry_validator() -> Draft202012Validator:
    return Draft202012Validator(
        json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")), format_checker=FormatChecker()
    )


def schema_violations(record: Any) -> List[str]:
    """Every way ``record`` violates the evidence schema, as ``path: message`` strings."""
    errors = sorted(_entry_validator().iter_errors(record), key=lambda e: list(map(str, e.absolute_path)))
    out = []
    for e in errors:
        where = "/".join(map(str, e.absolute_path))
        # An enum message would list every known action; the offending value is enough.
        message = f"{e.instance!r} is not a recognised value" if e.validator == "enum" else e.message
        out.append(f"{where}: {message}" if where else message)
    return out

# Patterns for sensitive data redaction
REDACTION_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|secret|password|token|auth[_-]?header|bearer)\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.]{8,})['\"]?"), r"\1=[REDACTED]"),
    (re.compile(r"ghp_[A-Za-z0-9]{36}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA )?PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"), "[REDACTED_EMAIL]"),
]


def redact_sensitive_data(text: str) -> str:
    """Scans and scrubs secrets, credentials, and PII from text."""
    if not isinstance(text, str):
        return text
    sanitized = text
    for pattern, replacement in REDACTION_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def sanitize_value(val: Any) -> Any:
    """Recursively redacts sensitive strings inside dicts, lists, and strings."""
    if isinstance(val, str):
        return redact_sensitive_data(val)
    if isinstance(val, dict):
        return {k: sanitize_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [sanitize_value(item) for item in val]
    return val


@dataclass
class EvidenceEntry(DataClassSerializationMixin):
    """Represents a single immutable event in the evidence ledger."""

    task_id: str
    agent_id: str
    action: str
    entry_id: str = field(default_factory=lambda: f"ev-{uuid.uuid4().hex[:12]}")
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    command: Optional[str] = None
    result: Optional[str] = None
    artifact: Optional[str] = None
    task_class: Optional[str] = None
    risk_level: Optional[str] = None
    reasoning_tier: Optional[str] = None
    implementing_agent: Optional[str] = None
    recommended_agent: Optional[str] = None
    actual_agent: Optional[str] = None
    is_override: Optional[bool] = None
    override_reason: Optional[str] = None
    defect_type: Optional[str] = None
    orchestration_action: Optional[str] = None
    repository: Optional[str] = None
    reviewing_agents: Optional[List[str]] = None
    remediation_cycles: Optional[int] = None
    control_plane_caught_defect: Optional[bool] = None
    session_count: Optional[Any] = None
    elapsed_work_time: Optional[Any] = None
    token_usage: Optional[Any] = None
    monetary_cost: Optional[Any] = None
    findings_summary: Optional[Dict[str, int]] = None
    verification_summary: Optional[Dict[str, str]] = None
    human_decision: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema: str = EVIDENCE_ENTRY_SCHEMA_VERSION

    def __post_init__(self):
        # Sanitize sensitive fields upon creation
        if self.command:
            self.command = redact_sensitive_data(self.command)
        if self.result:
            self.result = redact_sensitive_data(self.result)
        if self.artifact:
            self.artifact = redact_sensitive_data(self.artifact)
        if self.metadata:
            self.metadata = sanitize_value(self.metadata)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EvidenceEntry":
        d = dict(data)
        d.pop("schema", None)
        valid_fields = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in d.items() if k in valid_fields}
        return cls(schema=EVIDENCE_ENTRY_SCHEMA_VERSION, **filtered)


def default_ledger_path() -> Path:
    """logs/control_plane/evidence_ledger.jsonl under the checkout, HOWLPLANE_HOME, or XDG state.

    A source checkout keeps its ledger in the repository, as before. An installed
    wheel has no repository, and its package directory is replaced on update, so
    the ledger goes under HOWLPLANE_HOME (or HOWLPLANE_DIR) when set, and
    otherwise under $XDG_STATE_HOME/howlplane.
    """
    if is_checkout():
        root = CHECKOUT_ROOT
    else:
        home = os.environ.get("HOWLPLANE_HOME") or os.environ.get("HOWLPLANE_DIR")
        if home:
            root = Path(home).expanduser()
        else:
            state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
            root = Path(state) / "howlplane"
    return root / "logs" / "control_plane" / "evidence_ledger.jsonl"


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class EvidenceLedger:
    """Manages the append-only evidence ledger log files.

    ``ledger_file`` is the active file. An append that leaves it at or above
    ``rotate_bytes`` compresses it into ``<name>.<UTC timestamp>.gz`` beside it,
    and the next append starts a new active file. ``rotate_bytes=0`` disables
    rotation.

    Appends and reads hold a shared ``flock`` on the ledger's directory; rotation
    holds it exclusively. No writer can therefore still be writing to a file
    that is being archived, and no reader can see the segment list change under
    it. Locking the directory rather than a lock file beside the ledger leaves
    nothing behind that the ledger did not already own.
    """

    def __init__(self, ledger_file: Optional[str] = None, rotate_bytes: int = DEFAULT_ROTATE_BYTES):
        if ledger_file is None:
            ledger_file = str(default_ledger_path())
        if rotate_bytes < 0:
            raise ValueError(f"rotate_bytes must be 0 (disabled) or positive, got {rotate_bytes}")
        self.ledger_file = Path(ledger_file)
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        self.rotate_bytes = rotate_bytes
        # <name>.<stamp> is a staged segment, <name>.<stamp>.gz an archive, and
        # <name>.<stamp>.gz.partial an archive still being written.
        self._segment_name = re.compile(
            rf"^{re.escape(self.ledger_file.name)}\.(\d{{8}}T\d{{12}}Z)(\.gz)?(\.partial)?$"
        )

    @contextmanager
    def _locked(self, exclusive: bool) -> Iterator[None]:
        if fcntl is None:
            yield
            return
        fd = os.open(self.ledger_file.parent, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the lock

    def append_entry(self, entry: EvidenceEntry) -> None:
        """Validates against the schema, then appends the sanitized entry to the JSON Lines log.

        Nothing is written when validation or serialization fails. A rotation
        triggered by this append that fails is logged, not raised: the entry is
        already durable, and an error here would invite a duplicate retry.
        """
        record = entry.to_dict()
        line = json.dumps(record) + "\n"
        violations = schema_violations(json.loads(line))
        if violations:
            _LOG.error("evidence entry %s (%s) violates the schema: %s", entry.entry_id, entry.action, violations)
            raise EvidenceSchemaError(
                f"evidence entry {entry.entry_id} (action={entry.action!r}) violates "
                f"{_SCHEMA_PATH.name}: " + "; ".join(violations)
            )
        # One O_APPEND write of the whole line keeps concurrent appenders from
        # interleaving records; fsync makes the entry durable before returning.
        data = line.encode("utf-8")
        with self._locked(exclusive=False):
            fd = os.open(self.ledger_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
            try:
                written = os.write(fd, data)
                while written < len(data):
                    written += os.write(fd, data[written:])
                os.fsync(fd)
                size = os.fstat(fd).st_size
            finally:
                os.close(fd)
        if self.rotate_bytes and size >= self.rotate_bytes:
            try:
                self._rotate(min_bytes=self.rotate_bytes)
            except OSError as exc:
                _LOG.error("could not rotate %s: %s; the next rotation will retry", self.ledger_file, exc)

    def rotate(self) -> Optional[Path]:
        """Archives the active file now, whatever its size.

        Returns the new archive, or None when there is nothing to archive or the
        platform has no ``flock``.
        """
        return self._rotate(min_bytes=1)

    def _rotate(self, min_bytes: int) -> Optional[Path]:
        if fcntl is None:
            return None
        with self._locked(exclusive=True):
            self._finish_interrupted_rotations()
            try:
                size = self.ledger_file.stat().st_size
            except FileNotFoundError:
                return None
            # Another appender may have rotated while this one waited for the lock.
            if size < min_bytes:
                return None
            staged = self._new_segment_path()
            os.replace(self.ledger_file, staged)
            return self._compress(staged)

    def _new_segment_path(self) -> Path:
        stamp = datetime.now(timezone.utc)
        archived = self._archives()
        if archived:
            # Never sort before an existing archive, even if the clock went back.
            latest = datetime.strptime(archived[-1][0], _ARCHIVE_STAMP).replace(tzinfo=timezone.utc)
            stamp = max(stamp, latest + timedelta(microseconds=1))
        return self.ledger_file.with_name(f"{self.ledger_file.name}.{stamp.strftime(_ARCHIVE_STAMP)}")

    def _compress(self, staged: Path) -> Path:
        """Compresses a staged segment, then removes it. Caller holds the exclusive lock.

        The archive is complete and durable before the staged copy goes, so a
        crash at any point leaves each entry in a readable segment exactly once:
        readers prefer the archive when both exist.
        """
        archive = staged.with_name(staged.name + ".gz")
        partial = staged.with_name(staged.name + ".gz.partial")
        with open(staged, "rb") as src, open(partial, "wb") as raw:
            with gzip.GzipFile(filename=staged.name, mode="wb", fileobj=raw, mtime=0) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(partial, archive)
        _fsync_directory(archive.parent)
        os.unlink(staged)
        return archive

    def _finish_interrupted_rotations(self) -> None:
        """Completes any rotation a crash cut short. Caller holds the exclusive lock."""
        staged, archived = [], set()
        for path in self.ledger_file.parent.iterdir():
            match = self._segment_name.match(path.name)
            if not match:
                continue
            if match.group(3):
                path.unlink()
            elif match.group(2):
                archived.add(match.group(1))
            else:
                staged.append((match.group(1), path))
        for stamp, path in sorted(staged):
            if stamp in archived:
                path.unlink()
            else:
                self._compress(path)

    def _archives(self) -> List[Tuple[str, Path]]:
        """Archived segments, oldest first. A finished archive wins over its staged original."""
        found: Dict[str, Path] = {}
        if not self.ledger_file.parent.is_dir():
            return []
        for path in self.ledger_file.parent.iterdir():
            match = self._segment_name.match(path.name)
            if not match or match.group(3):
                continue
            if match.group(2) or match.group(1) not in found:
                found[match.group(1)] = path
        return sorted(found.items())

    def segment_files(self) -> List[Path]:
        """Every file holding ledger history, oldest first: archives, then the active file."""
        with self._locked(exclusive=False):
            return self._segment_files()

    def _segment_files(self) -> List[Path]:
        files = [path for _, path in self._archives()]
        if self.ledger_file.exists():
            files.append(self.ledger_file)
        return files

    def _lines(self) -> Iterator[Tuple[str, int, str]]:
        """Yields (segment name, line number, text) across all history.

        Holds the shared lock until the generator is exhausted or closed, so a
        rotation cannot move a segment between listing and reading it.
        """
        with self._locked(exclusive=False):
            for path in self._segment_files():
                opener = gzip.open if path.name.endswith(".gz") else open
                with opener(path, "rt", encoding="utf-8") as f:
                    for number, raw in enumerate(f, start=1):
                        yield path.name, number, raw

    def read_entries(self, strict: bool = False) -> Tuple[List[EvidenceEntry], List[LedgerDiagnostic]]:
        """Reads the whole ledger history and reports every record that is not clean.

        Malformed lines cannot be returned and are listed as diagnostics instead of
        disappearing. Schema-invalid but parseable (for example historical) records are
        still returned and flagged. With ``strict`` any diagnostic raises
        :class:`EvidenceCorruptionError`.
        """
        entries: List[EvidenceEntry] = []
        diagnostics: List[LedgerDiagnostic] = []
        for source, number, raw in self._lines():
            text = raw.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
                entry = EvidenceEntry.from_dict(record)
            except Exception as exc:
                diagnostics.append(LedgerDiagnostic(number, "malformed", f"{type(exc).__name__}: {exc}", source))
                continue
            entries.append(entry)
            violations = schema_violations(record)
            if violations:
                diagnostics.append(LedgerDiagnostic(number, "schema_invalid", "; ".join(violations), source))
        if strict and diagnostics:
            first = diagnostics[0]
            raise EvidenceCorruptionError(
                f"{self.ledger_file}: {len(diagnostics)} unclean record(s); first is line "
                f"{first.line_number} of {first.source} ({first.kind}): {first.reason}"
            )
        return entries, diagnostics

    def list_all_entries(self) -> List[EvidenceEntry]:
        """Reads all parseable entries across the whole history. Skipped malformed lines are logged, never silent."""
        entries: List[EvidenceEntry] = []
        skipped = 0
        for _, _, raw in self._lines():
            line = raw.strip()
            if line:
                try:
                    entries.append(EvidenceEntry.from_dict(json.loads(line)))
                except Exception:
                    skipped += 1
        if skipped:
            _LOG.warning("%s: skipped %d malformed evidence line(s); run `howlplane doctor`", self.ledger_file, skipped)
        return entries

    def get_task_entries(self, task_id: str) -> List[EvidenceEntry]:
        """Retrieves all evidence entries associated with a specific task_id."""
        return [e for e in self.list_all_entries() if e.task_id == task_id]

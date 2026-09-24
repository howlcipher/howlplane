#!/usr/bin/env python3
"""
evidence_ledger.py

Durable, history-preserving, append-only evidence ledger with automatic redaction
of secrets, credentials, and sensitive data.
"""

from dataclasses import dataclass, field, asdict, fields
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
import uuid

from howlplane.control_plane.task_spec import DataClassSerializationMixin

EVIDENCE_ENTRY_SCHEMA_VERSION = "ai.evidence_entry/v1"

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


class EvidenceLedger:
    """Manages the append-only evidence ledger log files."""

    def __init__(self, ledger_file: Optional[str] = None):
        if ledger_file is None:
            # Default to logs/control_plane/evidence_ledger.jsonl
            repo_root = Path(__file__).resolve().parents[3]
            ledger_file = str(repo_root / "logs" / "control_plane" / "evidence_ledger.jsonl")
        self.ledger_file = Path(ledger_file)
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)

    def append_entry(self, entry: EvidenceEntry) -> None:
        """Appends a new sanitized entry to the JSON Lines log."""
        line = json.dumps(entry.to_dict()) + "\n"
        with open(self.ledger_file, "a", encoding="utf-8") as f:
            f.write(line)

    def list_all_entries(self) -> List[EvidenceEntry]:
        """Reads all entries from the ledger."""
        if not self.ledger_file.exists():
            return []
        entries = []
        with open(self.ledger_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(EvidenceEntry.from_dict(json.loads(line)))
                    except Exception:
                        continue
        return entries

    def get_task_entries(self, task_id: str) -> List[EvidenceEntry]:
        """Retrieves all evidence entries associated with a specific task_id."""
        return [e for e in self.list_all_entries() if e.task_id == task_id]

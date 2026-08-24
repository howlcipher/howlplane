#!/usr/bin/env python3
"""Shared safety rules for durable reasoning artifacts."""

import hashlib
import json
from dataclasses import asdict
from typing import Any, Dict

from src.control_plane.evidence_ledger import sanitize_value
from src.control_plane.task_spec import DataClassSerializationMixin

MAX_ARTIFACT_DEPTH = 8
MAX_COLLECTION_ITEMS = 100
MAX_STRING_LENGTH = 4096

FORBIDDEN_REASONING_FIELDS = {
    "chain_of_thought",
    "hidden_reasoning",
    "internal_thoughts",
    "private_notes",
    "raw_prompt",
    "reasoning_trace",
}


class ArtifactIntegrityError(ValueError):
    """Raised when persisted reasoning evidence fails integrity validation."""


class SafeArtifactSerializationMixin(DataClassSerializationMixin):
    """Serialize dataclasses through the common bounds and redaction policy."""

    def to_dict(self) -> Dict[str, Any]:
        return safe_artifact_value(asdict(self))


def safe_artifact_value(value: Any, depth: int = 0) -> Any:
    """Remove hidden reasoning, redact secrets, and bound persisted payloads."""
    if depth >= MAX_ARTIFACT_DEPTH:
        return "[MAX_DEPTH_REACHED]"
    if isinstance(value, dict):
        bounded: Dict[str, Any] = {}
        for key, item in list(value.items())[:MAX_COLLECTION_ITEMS]:
            normalized_key = str(key)
            if normalized_key.lower() in FORBIDDEN_REASONING_FIELDS:
                continue
            bounded[normalized_key] = safe_artifact_value(item, depth + 1)
        return bounded
    if isinstance(value, (list, tuple, set)):
        return [
            safe_artifact_value(item, depth + 1)
            for item in list(value)[:MAX_COLLECTION_ITEMS]
        ]
    if isinstance(value, str):
        return sanitize_value(value[:MAX_STRING_LENGTH])
    return value


def canonical_digest(data: Dict[str, Any], *excluded_fields: str) -> str:
    """Return a deterministic digest over the safe durable representation."""
    payload = safe_artifact_value(data)
    for field in excluded_fields:
        payload.pop(field, None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

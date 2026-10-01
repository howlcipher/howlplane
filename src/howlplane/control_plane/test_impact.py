"""Test Impact Assessment (TIA) as a durable, validated evidence record.

AGENTS.md requires a TIA for every code-changing task. This module gives it a
structured form: a JSON document validated against
``schemas/test-impact-assessment.schema.json`` and appended to the evidence ledger
as a ``test_impact_assessed`` entry. ``check_assessment`` is what the ship check
uses to refuse completion when a code-changing task has no valid record.

It deliberately is not a test-management system: it records the decision and
checks that the decision is complete and internally consistent.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jsonschema import Draft202012Validator

from howlplane.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger

TIA_SCHEMA_VERSION = "howlplane.test_impact_assessment/v1"
TIA_ACTION = "test_impact_assessed"
_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "test-impact-assessment.schema.json"


def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")))


def validate_assessment(document: Any) -> List[str]:
    """Every reason the document is not an acceptable TIA (empty when it is)."""
    if not isinstance(document, dict):
        return ["a Test Impact Assessment must be a JSON object"]
    errors = [
        f"{'.'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
        for err in sorted(_validator().iter_errors(document), key=lambda e: list(e.absolute_path))
    ]
    if errors:
        return errors
    if document["full_regression_required"] and not document.get("regression_commands_run"):
        errors.append("regression_commands_run: required when full_regression_required is true")
    if document["tests_removed"] and not document["duplicate_or_obsolete_tests_reviewed"]:
        errors.append("tests_removed: removing tests requires duplicate_or_obsolete_tests_reviewed to be true")
    return errors


def record_assessment(
    ledger: EvidenceLedger, task_id: str, document: Dict[str, Any], agent_id: str = "agent"
) -> EvidenceEntry:
    """Validate and append one TIA. Raises ValueError listing every problem."""
    errors = validate_assessment(document)
    if errors:
        raise ValueError("; ".join(errors))
    entry = EvidenceEntry(
        task_id=task_id, agent_id=agent_id, action=TIA_ACTION, result="recorded",
        metadata={"tia": {"schema": TIA_SCHEMA_VERSION, **document}})
    ledger.append_entry(entry)
    return entry


def check_assessment(ledger: EvidenceLedger, task_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """(latest valid TIA, "") or (None, why none is acceptable) for one task."""
    candidates = [e for e in ledger.get_task_entries(task_id) if e.action == TIA_ACTION]
    if not candidates:
        return None, "no test_impact_assessed entry is recorded for this task"
    latest = candidates[-1]
    document = (latest.metadata or {}).get("tia")
    errors = validate_assessment({k: v for k, v in (document or {}).items() if k != "schema"})
    if errors:
        return None, "the latest Test Impact Assessment is invalid: " + "; ".join(errors)
    return document, ""

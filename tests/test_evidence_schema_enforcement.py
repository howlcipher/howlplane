"""#84: evidence is schema-valid at write time and corruption is visible on read."""

import json
from pathlib import Path

import pytest

from howlplane.control_plane.evidence_ledger import (
    EvidenceCorruptionError,
    EvidenceEntry,
    EvidenceLedger,
    EvidenceSchemaError,
    _entry_validator,
)
from howlplane.control_plane.test_impact import record_assessment
from tests._evidence_inventory import emitted_actions
from tests._tia_helpers import valid_tia_document

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((REPO / "schemas" / "evidence-entry.schema.json").read_text(encoding="utf-8"))
SCHEMA_ACTIONS = set(SCHEMA["properties"]["action"]["enum"])


def _ledger(tmp_path) -> EvidenceLedger:
    return EvidenceLedger(str(tmp_path / "l.jsonl"))


def test_every_action_emitted_by_production_code_is_in_the_schema():
    emitted = emitted_actions(REPO / "src")
    assert emitted, "inventory found nothing; the scanner is broken"
    missing = {a: sorted(sites)[:2] for a, sites in emitted.items() if a not in SCHEMA_ACTIONS}
    assert not missing, f"emitted but not in schemas/evidence-entry.schema.json: {missing}"


def test_known_historical_and_core_actions_remain_in_the_schema():
    for action in ("task_created", "routed", "state_transition", "diff_generated", "task_closed",
                   "human_approval", "human_rejection", "task_resumed", "task_completed",
                   "task_cancelled", "stale_approval_detected", "bounded_execution_completed",
                   "work_item_decision", "repo_proposal_decision", "test_impact_assessed",
                   "completion_blocked_tia"):
        assert action in SCHEMA_ACTIONS


def test_schema_enum_has_no_duplicates():
    enum = SCHEMA["properties"]["action"]["enum"]
    assert len(enum) == len(set(enum))


def test_valid_entry_appends(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.append_entry(EvidenceEntry(task_id="T", agent_id="a", action="task_completed", metadata={"k": 1}))
    assert [e.action for e in ledger.list_all_entries()] == ["task_completed"]


def _assert_rejected_without_writing(ledger, entry, match):
    before = ledger.ledger_file.read_bytes() if ledger.ledger_file.exists() else b""
    with pytest.raises(EvidenceSchemaError, match=match):
        ledger.append_entry(entry)
    after = ledger.ledger_file.read_bytes() if ledger.ledger_file.exists() else b""
    assert after == before


def test_unknown_action_fails_before_write(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.append_entry(EvidenceEntry(task_id="T", agent_id="a", action="task_created"))
    _assert_rejected_without_writing(
        ledger, EvidenceEntry(task_id="T", agent_id="a", action="not_a_real_action"), "not_a_real_action"
    )


def test_wrong_field_type_fails_before_write(tmp_path):
    ledger = _ledger(tmp_path)
    _assert_rejected_without_writing(
        ledger,
        EvidenceEntry(task_id="T", agent_id="a", action="task_created", remediation_cycles="two"),
        "remediation_cycles",
    )


def test_required_field_violation_fails_before_write(tmp_path):
    ledger = _ledger(tmp_path)
    _assert_rejected_without_writing(
        ledger, EvidenceEntry(task_id="T", agent_id="", action="task_created"), "agent_id"
    )
    _assert_rejected_without_writing(
        ledger, EvidenceEntry(task_id="", agent_id="a", action="task_created"), "task_id"
    )


def test_unserializable_metadata_writes_nothing(tmp_path):
    ledger = _ledger(tmp_path)
    with pytest.raises(TypeError):
        ledger.append_entry(EvidenceEntry(task_id="T", agent_id="a", action="task_created", metadata={"x": object()}))
    assert not ledger.ledger_file.exists() or ledger.ledger_file.read_text() == ""


def test_validator_is_cached():
    assert _entry_validator() is _entry_validator()


def test_tia_and_decision_evidence_stay_valid(tmp_path):
    ledger = _ledger(tmp_path)
    record_assessment(ledger, "T", valid_tia_document())
    from howlplane.control_plane.factory import owner_decision as od

    od.record_decision(ledger, "WI-1", "work_item_decision", "approved", {"reason": "x"})
    od.record_decision(ledger, "PROP-1", "repo_proposal_decision", "approved", {"reason": "x"})
    assert [e.action for e in ledger.list_all_entries()] == [
        "test_impact_assessed", "work_item_decision", "repo_proposal_decision",
    ]


def test_validation_failure_does_not_record_evidence_about_itself(tmp_path):
    ledger = _ledger(tmp_path)
    with pytest.raises(EvidenceSchemaError):
        ledger.append_entry(EvidenceEntry(task_id="T", agent_id="a", action="bogus"))
    assert not ledger.ledger_file.exists() or ledger.ledger_file.read_text() == ""


# ---- read visibility ------------------------------------------------------

def _corrupt(tmp_path) -> EvidenceLedger:
    ledger = _ledger(tmp_path)
    ledger.append_entry(EvidenceEntry(task_id="T", agent_id="a", action="task_created"))
    with open(ledger.ledger_file, "a", encoding="utf-8") as f:
        f.write("{not json\n")
        f.write(json.dumps({**EvidenceEntry(task_id="H", agent_id="a", action="legacy_action").to_dict()}) + "\n")
    return ledger


def test_read_entries_reports_malformed_and_schema_invalid_lines(tmp_path):
    entries, diagnostics = _corrupt(tmp_path).read_entries()
    assert [e.task_id for e in entries] == ["T", "H"]  # historical record still readable
    assert [(d.line_number, d.kind) for d in diagnostics] == [(2, "malformed"), (3, "schema_invalid")]
    assert "legacy_action" in diagnostics[1].reason


def test_strict_read_raises_on_corruption(tmp_path):
    with pytest.raises(EvidenceCorruptionError, match="line 2"):
        _corrupt(tmp_path).read_entries(strict=True)


def test_strict_read_passes_on_a_clean_ledger(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.append_entry(EvidenceEntry(task_id="T", agent_id="a", action="task_created"))
    entries, diagnostics = ledger.read_entries(strict=True)
    assert len(entries) == 1 and diagnostics == []


def test_lenient_list_all_entries_logs_skipped_lines(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        entries = _corrupt(tmp_path).list_all_entries()
    assert len(entries) == 2
    assert "skipped 1 malformed" in caplog.text


def test_record_cli_rejects_unknown_action(tmp_path, capsys):
    from howlplane.control_plane.cli import main

    ledger_file = tmp_path / "l.jsonl"
    code = main(["record", "--task-id", "T", "--agent-id", "a", "--action", "typo_action",
                 "--ledger-file", str(ledger_file)])
    assert code != 0
    err = capsys.readouterr().err
    assert "EVIDENCE_SCHEMA_INVALID" in err and "typo_action" in err
    assert not ledger_file.exists() or ledger_file.read_text() == ""


def test_orchestrator_does_not_swallow_a_schema_violation(tmp_path):
    from howlplane.control_plane.orchestrator import GovernedTaskOrchestrator

    orch = GovernedTaskOrchestrator(target_repo=tmp_path, control_plane_root=tmp_path / "cp")
    with pytest.raises(EvidenceSchemaError):
        orch._record_event(task_id="T", agent_id="control_plane", action="bogus_action")

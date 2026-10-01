"""Test Impact Assessment: a structured, validated evidence record that ship checks can require."""

import json

import pytest

from howlplane.control_plane import test_impact as tia
from howlplane.control_plane.cli import main
from howlplane.control_plane.evidence_ledger import EvidenceLedger

pytestmark = pytest.mark.unit


def _doc(**overrides):
    base = {
        "changed_behavior": "approve --proposal marks a proposal accepted",
        "tests_added": ["tests/test_proposal_decision.py"],
        "tests_updated": [],
        "tests_removed": [],
        "duplicate_or_obsolete_tests_reviewed": True,
        "full_regression_required": True,
        "regression_commands_run": ["make test-full"],
        "rationale": "New behavior is covered directly; shared decision plumbing needs the full gate.",
    }
    base.update(overrides)
    return base


def test_valid_assessment_has_no_errors():
    assert tia.validate_assessment(_doc()) == []


@pytest.mark.parametrize("field", ["changed_behavior", "tests_added", "tests_updated", "tests_removed",
                                   "duplicate_or_obsolete_tests_reviewed", "full_regression_required", "rationale"])
def test_every_required_field_is_enforced(field):
    doc = _doc()
    del doc[field]
    errors = tia.validate_assessment(doc)
    assert errors and any(field in e for e in errors)


@pytest.mark.parametrize("override,fragment", [
    ({"changed_behavior": ""}, "changed_behavior"),
    ({"rationale": ""}, "rationale"),
    ({"tests_added": "tests/x.py"}, "tests_added"),
    ({"unexpected": 1}, "unexpected"),
    ({"full_regression_required": True, "regression_commands_run": []}, "regression_commands_run"),
    ({"tests_removed": ["tests/old.py"], "duplicate_or_obsolete_tests_reviewed": False}, "tests_removed"),
])
def test_invalid_or_inconsistent_assessments_are_rejected(override, fragment):
    errors = tia.validate_assessment(_doc(**override))
    assert errors and any(fragment in e for e in errors)


def test_regression_commands_are_optional_when_no_full_regression_is_required():
    assert tia.validate_assessment(_doc(full_regression_required=False, regression_commands_run=[])) == []


def test_non_object_is_rejected():
    assert tia.validate_assessment(["not", "an", "object"])


def test_record_and_check_round_trip(tmp_path):
    ledger = EvidenceLedger(str(tmp_path / "l.jsonl"))
    assert tia.check_assessment(ledger, "T-1")[0] is None
    tia.record_assessment(ledger, "T-1", _doc())
    document, reason = tia.check_assessment(ledger, "T-1")
    assert document["schema"] == tia.TIA_SCHEMA_VERSION and reason == ""
    assert ledger.get_task_entries("T-1")[0].action == "test_impact_assessed"
    assert tia.check_assessment(ledger, "T-2")[0] is None  # per-task, not global


def test_invalid_assessment_is_never_written(tmp_path):
    ledger = EvidenceLedger(str(tmp_path / "l.jsonl"))
    with pytest.raises(ValueError):
        tia.record_assessment(ledger, "T-1", _doc(rationale=""))
    assert ledger.list_all_entries() == []


def test_latest_assessment_wins_and_a_corrupted_one_fails_closed(tmp_path):
    ledger = EvidenceLedger(str(tmp_path / "l.jsonl"))
    tia.record_assessment(ledger, "T-1", _doc())
    path = tmp_path / "l.jsonl"
    entry = json.loads(path.read_text().splitlines()[-1])
    entry["metadata"]["tia"]["rationale"] = ""
    entry["entry_id"] = "later"
    path.write_text(path.read_text() + json.dumps(entry) + "\n")
    document, reason = tia.check_assessment(EvidenceLedger(str(path)), "T-1")
    assert document is None and "invalid" in reason


def test_cli_record_then_check(tmp_path, capsys):
    ledger = str(tmp_path / "l.jsonl")
    file = tmp_path / "tia.json"
    file.write_text(json.dumps(_doc()), encoding="utf-8")
    assert main(["tia", "check", "T-9", "--ledger-file", ledger]) == 1
    err = capsys.readouterr().err
    assert "TIA_MISSING" in err and "howlplane tia record --task-id T-9" in err
    assert main(["tia", "record", "--task-id", "T-9", "--file", str(file), "--ledger-file", ledger, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["recorded"] is True
    assert main(["tia", "check", "T-9", "--ledger-file", ledger, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_record_rejects_an_incomplete_assessment(tmp_path, capsys):
    ledger = tmp_path / "l.jsonl"
    file = tmp_path / "tia.json"
    file.write_text(json.dumps(_doc(rationale="")), encoding="utf-8")
    assert main(["tia", "record", "--task-id", "T-9", "--file", str(file), "--ledger-file", str(ledger)]) == 1
    assert "TIA_INVALID" in capsys.readouterr().err
    assert not ledger.exists() or ledger.read_text() == ""


def test_no_code_change_declaration_passes_without_a_record(tmp_path, capsys):
    assert main(["tia", "check", "T-9", "--no-code-change", "--ledger-file", str(tmp_path / "l.jsonl")]) == 0
    assert "no code change" in capsys.readouterr().out


def test_suggested_recovery_command_parses():
    from howlplane.control_plane.cli import build_parser
    build_parser().parse_args("tia record --task-id T-9 --file tia.json".split())


def test_evidence_schema_enum_includes_the_new_actions():
    from pathlib import Path
    schema = json.loads((Path(__file__).resolve().parent.parent / "schemas" / "evidence-entry.schema.json").read_text())
    actions = set(schema["properties"]["action"]["enum"])
    assert {"test_impact_assessed", "repo_proposal_decision", "work_item_decision"} <= actions

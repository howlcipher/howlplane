"""Shared helper: seed a valid Test Impact Assessment so completion-path tests can finish."""

from howlplane.control_plane.test_impact import record_assessment


def valid_tia_document(**overrides):
    doc = {
        "changed_behavior": "test fixture behavior",
        "tests_added": [],
        "tests_updated": [],
        "tests_removed": [],
        "duplicate_or_obsolete_tests_reviewed": True,
        "full_regression_required": False,
        "rationale": "fixture assessment for a completion-path test",
    }
    doc.update(overrides)
    return doc


def record_valid_tia(ledger, task_id, **overrides):
    """Record a valid TIA for ``task_id`` in ``ledger`` and return the entry."""
    return record_assessment(ledger, task_id, valid_tia_document(**overrides))

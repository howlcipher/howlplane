"""Fallback assessments must meet the same intake contract as Frame output."""

import pytest

from howlplane.control_plane.howldream_runner import HowlDreamRunner


@pytest.mark.parametrize("status,executable,disposition", [
    ("LOCALLY_VERIFIED", False, "ACCEPT_FOR_DEVELOPMENT"),
    ("REJECTED", False, "REJECT"),
    ("UNRESOLVED", False, "UNRESOLVED"),
    ("GENERATED", False, "INVESTIGATE"),
    ("LOCALLY_VERIFIED", True, "REJECT"),
])
def test_fallback_assessment_matches_current_candidate_contract(status, executable, disposition):
    candidate = {"candidate_id": "synthetic/candidate", "status": status,
                 "authority": {"type": "ADVISORY", "executable": executable}}
    result = HowlDreamRunner()._fallback_evaluate_candidate(candidate)
    assert result.get("schema_version") == "howl.assessment/v1"
    assert result["assessment_id"]
    assert result["candidate_id"] == candidate["candidate_id"]
    assert result["disposition"] == disposition
    assert result["authority"] == {"type": "ADVISORY", "executable": False}
    assert result["limitations"]
    assert not {"schema", "reason", "explanation", "parent_dream_id"} & result.keys()

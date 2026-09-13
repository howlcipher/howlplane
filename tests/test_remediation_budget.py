"""
test_remediation_budget.py

Deterministic tests for the remediation execution-budget policy.
"""

import pytest

from src.control_plane.orchestrator import OrchestrationConfig, compute_remediation_timeout
from src.control_plane.reconciliation import ReviewFinding


def _finding(severity: str) -> ReviewFinding:
    return ReviewFinding(
        id=f"f-{severity}",
        reviewer_role="tester",
        title=f"{severity} finding",
        severity=severity,
        category="quality",
        description="a finding",
    )


def test_remediation_timeout_defaults_to_base_when_no_findings():
    config = OrchestrationConfig(timeout_seconds=600)
    assert compute_remediation_timeout(config) == 600


def test_remediation_timeout_increases_with_findings():
    config = OrchestrationConfig(
        timeout_seconds=600,
        remediation_timeout_per_finding_seconds=60,
    )
    findings = [_finding("medium")]
    assert compute_remediation_timeout(config, findings) == 600 + 60


def test_remediation_timeout_weights_severity():
    config = OrchestrationConfig(timeout_seconds=600)
    findings = [_finding("blocker"), _finding("high")]
    assert compute_remediation_timeout(config, findings) == 600 + 120 + 90


def test_remediation_timeout_caps_at_max_multiplier():
    config = OrchestrationConfig(
        timeout_seconds=600,
        remediation_timeout_max_multiplier=2.0,
    )
    findings = [_finding("blocker")] * 20
    assert compute_remediation_timeout(config, findings) == 1200


def test_remediation_timeout_explicit_value_honored():
    config = OrchestrationConfig(
        timeout_seconds=600,
        remediation_timeout_seconds=900,
    )
    findings = [_finding("blocker")] * 100
    assert compute_remediation_timeout(config, findings) == 900


def test_remediation_timeout_explicit_value_capped():
    config = OrchestrationConfig(
        timeout_seconds=600,
        remediation_timeout_seconds=5000,
        remediation_timeout_max_multiplier=3.0,
    )
    assert compute_remediation_timeout(config) == 1800


def test_remediation_timeout_adds_per_affected_file_overhead():
    config = OrchestrationConfig(
        timeout_seconds=600,
        remediation_timeout_per_finding_seconds=60,
        remediation_timeout_per_affected_file_seconds=30,
    )
    findings = [_finding("medium")]
    affected = ["a.py", "b.py", "c.py"]
    assert compute_remediation_timeout(config, findings, affected) == 600 + 60 + 90


def test_remediation_timeout_deduplicates_affected_files():
    config = OrchestrationConfig(
        timeout_seconds=600,
        remediation_timeout_per_affected_file_seconds=30,
    )
    affected = ["a.py", "a.py", "b.py"]
    assert compute_remediation_timeout(config, affected_files=affected) == 600 + 60


def test_remediation_timeout_with_large_remediation_is_bounded():
    config = OrchestrationConfig(
        timeout_seconds=600,
        remediation_timeout_max_multiplier=3.0,
    )
    findings = [_finding("blocker")] * 10
    affected = [f"f{i}.py" for i in range(100)]
    # Without cap this would be 600 + 1200 + 3000 = 4800
    assert compute_remediation_timeout(config, findings, affected) == 1800

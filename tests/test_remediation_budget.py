"""
test_remediation_budget.py

Deterministic tests for the remediation execution-budget policy.
"""

import pytest

from howlplane.control_plane.orchestrator import OrchestrationConfig, compute_remediation_timeout
from howlplane.control_plane.reconciliation import ReviewFinding


def _finding(severity: str) -> ReviewFinding:
    return ReviewFinding(
        id=f"f-{severity}",
        reviewer_role="tester",
        title=f"{severity} finding",
        severity=severity,
        category="quality",
        description="a finding",
    )


def _config(**kwargs) -> OrchestrationConfig:
    return OrchestrationConfig(timeout_seconds=600, **kwargs)


@pytest.mark.parametrize(
    "kwargs,findings,affected,expected",
    [
        ({}, None, None, 600),
        ({"remediation_timeout_per_finding_seconds": 60}, [_finding("medium")], None, 660),
        ({}, [_finding("blocker"), _finding("high")], None, 810),
        ({"remediation_timeout_max_multiplier": 2.0}, [_finding("blocker")] * 20, None, 1200),
        ({"remediation_timeout_seconds": 900}, [_finding("blocker")] * 100, None, 900),
        ({"remediation_timeout_seconds": 5000, "remediation_timeout_max_multiplier": 3.0}, None, None, 1800),
        (
            {"remediation_timeout_per_finding_seconds": 60, "remediation_timeout_per_affected_file_seconds": 30},
            [_finding("medium")],
            ["a.py", "b.py", "c.py"],
            750,
        ),
        ({"remediation_timeout_per_affected_file_seconds": 30}, None, ["a.py", "a.py", "b.py"], 660),
        (
            {"remediation_timeout_max_multiplier": 3.0},
            [_finding("blocker")] * 10,
            [f"f{i}.py" for i in range(100)],
            1800,
        ),
    ],
)
def test_compute_remediation_timeout(kwargs, findings, affected, expected):
    config = _config(**kwargs)
    assert compute_remediation_timeout(config, findings, affected) == expected

"""
test_review_runner.py

Unit tests for independent review execution, structured findings validation,
and targeted re-review role determination.
"""

from src.control_plane.reconciliation import ReviewFinding
from src.control_plane.review_runner import (
    ReviewRunner,
    parse_and_validate_findings,
)
from src.control_plane.task_spec import TaskSpec


def test_parse_findings_clean_output():
    # Empty string: never a deliberate clean signal (#59.2 Phase 7).
    findings, err, is_valid = parse_and_validate_findings("", "correctness-reviewer")
    assert findings == []
    assert err is None
    assert is_valid is False

    # Explicit empty list: a deliberate, valid "no findings" signal.
    yaml_clean = "findings: []\n"
    findings, err, is_valid = parse_and_validate_findings(yaml_clean, "correctness-reviewer")
    assert findings == []
    assert err is None
    assert is_valid is True

    # Text saying clean: also a deliberate, valid signal.
    findings, err, is_valid = parse_and_validate_findings("No defects found. Implementation is clean.", "security-reviewer")
    assert findings == []
    assert err is None
    assert is_valid is True


def test_parse_findings_empty_output_marked_invalid_not_clean():
    """REVIEW_OUTPUT_INVALID: exit-0-with-empty-stdout must never collapse
    into the same signal as a reviewer that actually completed and found
    nothing (#59.2 Phase 7)."""
    findings, err, is_valid = parse_and_validate_findings("   \n  ", "architecture-reviewer")
    assert findings == []
    assert err is None
    assert is_valid is False


def test_parse_findings_valid_yaml():
    yaml_content = """
```yaml
findings:
  - id: "F001"
    title: "SQL injection vulnerability in query builder"
    severity: "high"
    category: "security"
    location: "src/db.py:42"
    claim: "User input is formatted directly into SQL query"
    evidence: "query = f'SELECT * FROM users WHERE name={name}'"
    suggested_fix: "Use parameterized query with cursor.execute(query, (name,))"
```
"""
    findings, err, is_valid = parse_and_validate_findings(yaml_content, "security-reviewer")
    assert err is None
    assert is_valid is True
    assert len(findings) == 1
    f = findings[0]
    assert f.id == "F001"
    assert f.severity == "high"
    assert f.category == "security"
    assert f.reviewer_role == "security-reviewer"
    assert f.location == "src/db.py:42"
    assert "SQL injection" in f.title


def test_parse_findings_malformed_yaml_yields_failure_finding():
    # Malformed YAML syntax must never become 0 findings / pass
    malformed = "```yaml\nfindings: [unclosed list bracket\n```"
    findings, err, is_valid = parse_and_validate_findings(malformed, "correctness-reviewer")
    assert err is not None
    assert is_valid is False
    assert len(findings) == 1
    assert findings[0].severity in ("high", "blocker")
    assert "Malformed reviewer output" in findings[0].title
    assert findings[0].reviewer_role == "correctness-reviewer"


def test_execute_review_cycle_with_fake_reviewers(tmp_path):
    spec = TaskSpec(
        task_id="TASK-REV-01",
        repository="test_repo",
        objective="Implement auth check",
    )

    def reviewer_fn(role_id, diff, task):
        if role_id == "security-reviewer":
            return """
findings:
  - id: "SEC-01"
    title: "Hardcoded secret key in auth module"
    severity: "high"
    category: "security"
    location: "src/auth.py:10"
    claim: "Secret key is hardcoded"
"""
        elif role_id == "test-falsifier":
            return """
findings:
  - id: "TST-01"
    title: "Missing test for invalid token"
    severity: "medium"
    category: "test_gap"
    location: "tests/test_auth.py:20"
"""
        return "findings: []"

    cycle_res = ReviewRunner.execute_review_cycle(
        task=spec,
        diff_content="+ secret = '12345'",
        reviewer_roles=["security-reviewer", "test-falsifier", "correctness-reviewer"],
        cwd=tmp_path,
        custom_reviewer_fn=reviewer_fn,
    )

    assert cycle_res.status == "has_findings"
    assert cycle_res.requires_remediation is True
    assert len(cycle_res.all_findings) == 2
    assert cycle_res.reconciliation is not None
    assert cycle_res.reconciliation.unresolved_highs >= 1


def test_execute_review_cycle_all_reviewers_fail_requires_remediation(tmp_path):
    """#59.2 Phase 3: zero findings from failed reviewers must never read as
    "clean" -- absence of findings is not evidence of a completed review."""
    spec = TaskSpec(
        task_id="TASK-REV-02",
        repository="test_repo",
        objective="Implement auth check",
    )

    def failing_reviewer_fn(role_id, diff, task):
        raise RuntimeError(f"{role_id} backend crashed")

    cycle_res = ReviewRunner.execute_review_cycle(
        task=spec,
        diff_content="+ secret = '12345'",
        reviewer_roles=["security-reviewer", "test-falsifier"],
        cwd=tmp_path,
        custom_reviewer_fn=failing_reviewer_fn,
    )

    assert cycle_res.all_findings == []
    assert cycle_res.status == "review_failure"
    assert cycle_res.requires_remediation is True
    for res in cycle_res.reviewer_results.values():
        assert res.status == "reviewer_failure"


def test_execute_review_cycle_empty_output_requires_remediation(tmp_path):
    """A reviewer that "succeeds" but returns nothing must be classified
    output_invalid, not clean, and must still require remediation."""
    spec = TaskSpec(
        task_id="TASK-REV-03",
        repository="test_repo",
        objective="Implement auth check",
    )

    cycle_res = ReviewRunner.execute_review_cycle(
        task=spec,
        diff_content="+ secret = '12345'",
        reviewer_roles=["security-reviewer"],
        cwd=tmp_path,
        custom_reviewer_fn=lambda role_id, diff, task: "",
    )

    assert cycle_res.reviewer_results["security-reviewer"].status == "output_invalid"
    assert cycle_res.status == "review_failure"
    assert cycle_res.requires_remediation is True


def test_determine_re_review_roles():
    f_sec = ReviewFinding(
        id="F1",
        reviewer_role="security-reviewer",
        title="Sec bug",
        severity="high",
        category="security",
        description="Sec issue",
    )
    f_arch = ReviewFinding(
        id="F2",
        reviewer_role="architecture-reviewer",
        title="Arch coupling",
        severity="medium",
        category="architecture",
        description="Coupling issue",
    )

    all_roles = ["correctness-reviewer", "regression-reviewer", "security-reviewer", "architecture-reviewer", "test-falsifier", "simplicity-reviewer"]

    # Security finding should trigger security, correctness, and test falsifier
    roles_sec = ReviewRunner.determine_re_review_roles([f_sec], all_roles)
    assert "security-reviewer" in roles_sec
    assert "correctness-reviewer" in roles_sec
    assert "test-falsifier" in roles_sec

    # Architecture finding should trigger architecture, regression, and correctness
    roles_arch = ReviewRunner.determine_re_review_roles([f_arch], all_roles)
    assert "architecture-reviewer" in roles_arch
    assert "regression-reviewer" in roles_arch
    assert "correctness-reviewer" in roles_arch

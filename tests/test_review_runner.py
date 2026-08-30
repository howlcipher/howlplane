"""
test_review_runner.py

Unit tests for independent review execution, structured findings validation,
and targeted re-review role determination.
"""

from src.control_plane.agent_execution import (
    AgentExecutionResult,
    LAUNCH_OUTCOME_KEY,
    LAUNCH_OUTCOME_LAUNCHED,
    LAUNCH_OUTCOME_SPAWN_FAILED,
    TIMEOUT_SOURCE_KEY,
    TIMEOUT_SOURCE_HARNESS,
)
from src.control_plane.atomic_io import safe_load_json
from src.control_plane.reconciliation import ReviewFinding
from src.control_plane.resource_models import ProviderFailureClass
from src.control_plane.review_runner import (
    REVIEW_TIMEOUT_SECONDS,
    ReviewRunner,
    SingleReviewResult,
    build_reviewer_candidates,
    invoke_reviewer_with_failover,
    parse_and_validate_findings,
    write_review_result,
)
from src.control_plane.synthesis.provider_pool import ProviderPoolManager
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


def test_parse_findings_unfenced_prose_with_bullets_recognized_as_clean():
    """
    VERIFIED_REPLAY (#59.2): live campaign DOGFOOD-20260823-203128-ed0e9e's
    simplicity-reviewer wrote unfenced prose containing markdown bullets and
    colons -- yaml.safe_load would misparse that as an unrelated structured
    (and thus malformed) shape unless the clean-phrase check runs first.
    """
    prose = (
        "Zero defects found. The change is a single, minimal documentation "
        "file with no code, no redundant boilerplate, and no unnecessary "
        "abstraction — it's leaner than the project's own journal template, "
        "appropriate for its narrow purpose (recording canary initiation "
        "facts without asserting an unfinished outcome)."
    )
    findings, err, is_valid = parse_and_validate_findings(prose, "simplicity-reviewer")
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


class _StubPool:
    """Minimal provider pool exposing only the candidate selection hook."""

    def __init__(self, pool):
        self._pool = pool

    def select_candidates(self, task_category=None, avoid_provider=None, task=None, role=None):
        return [c for c in self._pool if c != avoid_provider]


def _candidates(pool, preferred, implementer):
    return build_reviewer_candidates(
        "correctness-reviewer",
        preferred,
        _StubPool(pool),
        TaskSpec(task_id="T-1", repository="repo", objective="obj"),
        implementer,
    )


def test_implementer_is_ordered_last_among_reviewer_candidates():
    """Failover must exhaust every independent reviewer before self-review.

    HOWLFRAM-BUG-50: build_reviewer_candidates was never told who implemented
    the change, so ordinary failover handed the implementer three of its own
    reviews, including the final correctness verdict on its own diff.
    """
    order = _candidates(["agy", "claude_code", "codex"], preferred="agy", implementer="agy")
    assert order[-1] == "agy"
    assert set(order) == {"agy", "claude_code", "codex"}


def test_implementer_stays_reachable_when_it_is_the_only_candidate():
    """A degraded pool still yields signal; it is labelled, not withheld."""
    assert _candidates(["agy"], preferred="agy", implementer="agy") == ["agy"]


def test_candidate_order_is_unchanged_when_the_implementer_is_not_a_candidate():
    order = _candidates(["claude_code", "codex"], preferred="claude_code", implementer="agy")
    assert order == ["claude_code", "codex"]


def test_no_implementer_supplied_preserves_previous_ordering():
    order = _candidates(["agy", "claude_code"], preferred="agy", implementer=None)
    assert order[0] == "agy"


# ---------------------------------------------------------------------------
# Review budget and failure taxonomy (issues.md #13, #14)
# ---------------------------------------------------------------------------


def _real_pool():
    """A real ProviderPoolManager, used only for its failure classifier."""
    from src.control_plane.agent_registry import AgentRegistry

    return ProviderPoolManager(
        registry=AgentRegistry([]), backend_resolver=None, probe_on_start=False
    )


class _FakePool:
    """Provider pool exposing only what reviewer failover consults."""

    def __init__(self, pool=(), exhausted=()):
        self._pool = list(pool)
        self._exhausted = set(exhausted)

    def select_candidates(self, task_category=None, avoid_provider=None, task=None, role=None):
        return [c for c in self._pool if c != avoid_provider]

    def get_status(self, candidate):
        return None

    def detect_exhaustion(self, candidate, result, task_id=None):
        return object() if candidate in self._exhausted else None

    def classify_failure(self, agent_id, result):
        return _real_pool().classify_failure(agent_id, result)


def _result(agent_id, **kw):
    base = dict(
        agent_id=agent_id, role="review", command=agent_id, exit_code=0,
        stdout="findings: []", stderr="", duration_seconds=1.0, success=True,
    )
    base.update(kw)
    return AgentExecutionResult(**base)


def _timed_out(agent_id, seconds):
    return _result(
        agent_id, exit_code=-1, stdout="", stderr=f"Timeout after {seconds}s.",
        duration_seconds=seconds, success=False, timed_out=True,
        metadata={LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_LAUNCHED,
                  TIMEOUT_SOURCE_KEY: TIMEOUT_SOURCE_HARNESS},
    )


def _spawn_failed(agent_id):
    return _result(
        agent_id, exit_code=127, stdout="", stderr="", duration_seconds=0.0, success=False,
        metadata={LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_SPAWN_FAILED},
    )


def _launched_then_failed(agent_id):
    return _result(
        agent_id, exit_code=2, stdout="", stderr="reviewer blew up", duration_seconds=4.0,
        success=False, metadata={LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_LAUNCHED},
    )


def _invoke(results, candidates, pool=None):
    """Runs reviewer failover against scripted per-candidate results."""
    backends = {aid: type("B", (), {
        "is_available": lambda self: True,
        "execute": (lambda res: (lambda self, **kw: res))(res),
    })() for aid, res in results.items()}
    return invoke_reviewer_with_failover(
        role_id="correctness-reviewer",
        candidates=candidates,
        task=TaskSpec(task_id="T-1", repository="repo", objective="obj"),
        cwd=".",
        prompt_override="brief",
        backend_lookup=lambda aid: backends.get(aid),
        provider_pool=pool or _FakePool(),
    )


def test_review_budget_admits_a_reviewer_slower_than_the_old_180s_ceiling():
    """The old budget sat at the median review duration (issues.md #13)."""
    assert REVIEW_TIMEOUT_SECONDS >= 600
    # The four reviews that completed on HOWLFRAM-BUG-50 took 124-178s, and the
    # deadline cut off 13 of 20 attempts at ~180s. A 300s reviewer is exactly
    # the case the old ceiling rejected and the new one must accept.
    assert 300 < REVIEW_TIMEOUT_SECONDS


def test_a_harness_timeout_is_durably_classified_as_a_budget_failure():
    winner, agent_res, attempts = _invoke(
        {"claude_code": _timed_out("claude_code", 600.1)}, ["claude_code"]
    )
    assert winner is None
    attempt = attempts[-1]
    assert attempt["outcome"] == "reviewer_failure"
    assert attempt["timed_out"] is True
    assert attempt["timeout_source"] == TIMEOUT_SOURCE_HARNESS
    assert attempt["failure_class"] == ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED.value


def test_a_spawn_failure_is_classified_differently_from_a_timeout():
    _w, _r, attempts = _invoke({"ghost": _spawn_failed("ghost")}, ["ghost"])
    attempt = attempts[-1]
    assert attempt["timed_out"] is False
    assert attempt["launch_outcome"] == LAUNCH_OUTCOME_SPAWN_FAILED
    assert attempt["failure_class"] == ProviderFailureClass.MISSING_EXECUTABLE.value


def test_a_launched_process_that_exits_nonzero_is_not_a_launch_failure():
    _w, _r, attempts = _invoke({"codex": _launched_then_failed("codex")}, ["codex"])
    attempt = attempts[-1]
    assert attempt["launch_outcome"] == LAUNCH_OUTCOME_LAUNCHED
    assert attempt["failure_class"] != ProviderFailureClass.MISSING_EXECUTABLE.value
    assert attempt["exit_code"] == 2


def test_failover_still_advances_and_records_every_attempt():
    winner, agent_res, attempts = _invoke(
        {"claude_code": _timed_out("claude_code", 600.1), "agy": _result("agy")},
        ["claude_code", "agy"],
    )
    assert winner == "agy"
    assert [a["provider"] for a in attempts] == ["claude_code", "agy"]
    assert attempts[0]["failure_class"] == ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED.value
    assert attempts[-1]["outcome"] == "completed"


def test_malformed_and_invalid_output_keep_their_existing_statuses():
    _w, _r, malformed = _invoke(
        {"a": _result("a", stdout="findings: [oops")}, ["a"]
    )
    assert malformed[-1]["outcome"] == "malformed_output"
    _w2, _r2, invalid = _invoke({"b": _result("b", stdout="")}, ["b"])
    assert invalid[-1]["outcome"] == "output_invalid"


def test_failure_classification_survives_into_role_level_evidence(tmp_path):
    """A total failure must still say why, in result.json (issues.md #14)."""
    _w, _r, attempts = _invoke(
        {"claude_code": _timed_out("claude_code", 600.1),
         "agy": _timed_out("agy", 600.2)},
        ["claude_code", "agy"],
    )
    single = SingleReviewResult(
        reviewer_role="correctness-reviewer", reviewer_name="Correctness Reviewer",
        status="reviewer_failure", error_message="All candidate reviewers failed or were unavailable",
        attempts=attempts,
    )
    write_review_result(tmp_path, "correctness-reviewer", single, implementer="agy",
                        assigned_resource="claude_code")
    persisted = safe_load_json(tmp_path / "correctness-reviewer" / "result.json")

    assert persisted["status"] == "reviewer_failure"          # contract unchanged
    assert persisted["process"]["timed_out"] is True          # was null
    assert persisted["timeout_source"] == TIMEOUT_SOURCE_HARNESS
    assert persisted["normalized_failure"] == ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED.value
    # A role that consumed two full budgets did not take 0.0 seconds.
    assert persisted["duration_seconds"] > 1000
    assert persisted["output_valid"] is False


def test_a_quota_failure_is_distinguishable_from_a_timeout_in_evidence(tmp_path):
    """The two failures that were byte-identical on HOWLFRAM-BUG-50."""
    _w, _r, fast = _invoke({"devin_cli": _launched_then_failed("devin_cli")}, ["devin_cli"])
    _w2, _r2, slow = _invoke({"agy": _timed_out("agy", 600.1)}, ["agy"])
    assert fast[-1]["timed_out"] is False
    assert slow[-1]["timed_out"] is True
    assert fast[-1]["failure_class"] != slow[-1]["failure_class"]


def test_resume_cannot_read_a_failed_review_as_clean(tmp_path):
    _w, _r, attempts = _invoke({"agy": _timed_out("agy", 600.1)}, ["agy"])
    single = SingleReviewResult(
        reviewer_role="correctness-reviewer", reviewer_name="Correctness Reviewer",
        status="reviewer_failure", attempts=attempts,
    )
    write_review_result(tmp_path, "correctness-reviewer", single)
    persisted = safe_load_json(tmp_path / "correctness-reviewer" / "result.json")
    assert persisted["disposition"] == "reviewer_failure"
    assert persisted["output_present"] is False
    assert persisted["findings_count"] == 0

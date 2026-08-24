"""
test_control_plane.py

Comprehensive unit and integration tests for the Multi-Agent Engineering Control Plane.
"""

import json
from pathlib import Path
import tempfile
import pytest

from src.control_plane.task_spec import (
    TaskSpec,
    InvalidStateTransitionError,
    TaskSpecValidationError,
    VALID_TASK_STATES,
)
from src.control_plane.agent_registry import (
    AgentProfile,
    AgentRegistry,
    BUILTIN_AGENTS,
)
from src.control_plane.router import (
    TaskRouter,
    RoutingDecision,
)
from src.control_plane.reviewers import (
    ReviewerRole,
    REVIEWER_ROLES,
    get_reviewer_role,
    list_reviewer_roles,
)
from src.control_plane.reconciliation import (
    ReviewFinding,
    ReconciliationResult,
    ReviewReconciler,
    ReconciliationValidationError,
)
from src.control_plane.verification import (
    VerificationStep,
    VerificationPlan,
    VerificationError,
    resolve_python_interpreter,
)
from src.control_plane.evidence_ledger import (
    EvidenceEntry,
    EvidenceLedger,
    redact_sensitive_data,
)
from src.control_plane.metrics import (
    MetricsCalculator,
    PerformanceMetricsSummary,
)
from src.control_plane.project_adapter import (
    ProjectContext,
    ProjectAdapter,
)
from src.control_plane.human_boundary import (
    HumanBoundaryGate,
    HumanDecisionPacket,
    BoundaryCheckResult,
)
from src.control_plane.cli import main as cli_main


# ============================================================================
# 1. TaskSpec Tests
# ============================================================================

def test_task_spec_creation_and_validation():
    spec = TaskSpec(
        task_id="TASK-001",
        repository="howlplane",
        objective="Build control plane",
        acceptance_criteria=["Tests pass", "Evidence recorded"],
        risk_level="medium",
        recommended_reasoning_tier="tier_2",
    )
    assert spec.task_id == "TASK-001"
    assert spec.current_state == "discovered"
    assert len(spec.acceptance_criteria) == 2


def test_task_spec_invalid_fields_raise():
    with pytest.raises(TaskSpecValidationError):
        TaskSpec(task_id="", repository="repo", objective="obj")

    with pytest.raises(TaskSpecValidationError):
        TaskSpec(task_id="T1", repository="repo", objective="obj", risk_level="invalid_risk")

    with pytest.raises(TaskSpecValidationError):
        TaskSpec(task_id="T1", repository="repo", objective="obj", recommended_reasoning_tier="tier_99")


def test_task_spec_valid_lifecycle_transitions():
    spec = TaskSpec(
        task_id="TASK-002",
        repository="repo",
        objective="Lifecycle test",
    )
    assert spec.current_state == "discovered"

    spec.transition_to("planned", reason="Task broken into steps")
    assert spec.current_state == "planned"

    spec.transition_to("implementing", reason="Started coding")
    assert spec.current_state == "implementing"

    spec.transition_to("reviewing", reason="Submitted diff for review")
    assert spec.current_state == "reviewing"

    spec.transition_to("remediating", reason="Fixing reviewer findings")
    assert spec.current_state == "remediating"

    spec.transition_to("verifying", reason="Running test suite")
    assert spec.current_state == "verifying"

    spec.transition_to("awaiting_human", reason="Boundary triggered")
    assert spec.current_state == "awaiting_human"

    spec.transition_to("complete", reason="Human approved and tests green")
    assert spec.current_state == "complete"


def test_task_spec_invalid_lifecycle_transitions():
    spec = TaskSpec(
        task_id="TASK-003",
        repository="repo",
        objective="Invalid transition test",
    )
    assert spec.current_state == "discovered"

    # Cannot jump directly from discovered to complete or verifying
    with pytest.raises(InvalidStateTransitionError):
        spec.transition_to("complete")

    with pytest.raises(InvalidStateTransitionError):
        spec.transition_to("verifying")

    spec.transition_to("planned")
    with pytest.raises(InvalidStateTransitionError):
        spec.transition_to("complete")

    with pytest.raises(InvalidStateTransitionError):
        spec.transition_to("remediating")


def test_task_spec_serialization_roundtrip():
    spec = TaskSpec(
        task_id="TASK-004",
        repository="howlcipher/test",
        objective="Serialization check",
        acceptance_criteria=["Criterion 1"],
        risk_level="high",
        required_skills=["cyber_security"],
        metadata={"author": "test_agent"},
    )
    # JSON
    json_str = spec.to_json()
    loaded_json = TaskSpec.from_json(json_str)
    assert loaded_json.task_id == spec.task_id
    assert loaded_json.required_skills == ["cyber_security"]

    # YAML
    yaml_str = spec.to_yaml()
    loaded_yaml = TaskSpec.from_yaml(yaml_str)
    assert loaded_yaml.task_id == spec.task_id
    assert loaded_yaml.risk_level == "high"


# ============================================================================
# 2. Agent Registry Tests
# ============================================================================

def test_agent_registry_builtins():
    registry = AgentRegistry()
    agents = registry.list_agents()
    assert len(agents) >= 5
    agent_ids = {a.agent_id for a in agents}
    assert "claude_code" in agent_ids
    assert "gemini_cli" in agent_ids
    assert "agy" in agent_ids
    assert "local_ollama" in agent_ids


def test_agent_registry_filtering():
    registry = AgentRegistry()
    tier_1 = registry.filter_agents(reasoning_tier="tier_1")
    assert any(a.agent_id == "claude_code" for a in tier_1)

    free_local = registry.filter_agents(cost_class="free_local")
    assert len(free_local) == 1
    assert free_local[0].agent_id == "local_ollama"

    reviewers = registry.filter_agents(capability="code_review")
    assert len(reviewers) >= 4


# ============================================================================
# 3. Task Router Tests
# ============================================================================

def test_router_tier_1_risk():
    router = TaskRouter()
    task = TaskSpec(
        task_id="SEC-01",
        repository="repo",
        objective="Patch critical authentication vulnerability",
        risk_level="critical",
        required_skills=["cyber_security"],
        recommended_reasoning_tier="tier_1",
    )
    decision = router.route(task)
    assert decision.reasoning_tier == "tier_1"
    assert "security-reviewer" in decision.recommended_reviewers
    assert "correctness-reviewer" in decision.recommended_reviewers
    assert "test-falsifier" in decision.recommended_reviewers


def test_router_low_cost_tier_3():
    router = TaskRouter()
    task = TaskSpec(
        task_id="DOC-01",
        repository="repo",
        objective="Format inline documentation comments",
        risk_level="low",
        recommended_reasoning_tier="tier_3",
    )
    decision = router.route(task)
    assert decision.selected_agent_id == "local_ollama"


def test_router_user_override():
    router = TaskRouter()
    task = TaskSpec(
        task_id="OVERRIDE-01",
        repository="repo",
        objective="Standard refactoring",
        preferred_agent="codex",
    )
    decision = router.route(task)
    assert decision.is_override is True
    assert decision.selected_agent_id == "codex"


# ============================================================================
# 4. Reviewer Roles & Brief Generation Tests
# ============================================================================

def test_reviewer_roles_loaded():
    roles = list_reviewer_roles()
    assert len(roles) >= 6
    role_ids = {r.role_id for r in roles}
    assert {
        "correctness-reviewer",
        "regression-reviewer",
        "security-reviewer",
        "test-falsifier",
        "architecture-reviewer",
        "simplicity-reviewer",
    }.issubset(role_ids)


def test_reviewer_brief_rendering():
    task = TaskSpec(
        task_id="REV-01",
        repository="repo",
        objective="Implement rate limiting",
        acceptance_criteria=["Max 100 requests per minute"],
        constraints=["Zero external redis"],
    )
    role = get_reviewer_role("correctness-reviewer")
    assert role is not None
    brief = role.render_brief(task=task, diff_content="+ def rate_limit(): pass")
    assert "Independent Review Request" in brief
    assert "Max 100 requests per minute" in brief
    assert "Zero external redis" in brief
    assert "+ def rate_limit(): pass" in brief


# ============================================================================
# 5. Review Reconciliation Tests
# ============================================================================

def test_reconciliation_multi_reviewer_agreement():
    f1 = ReviewFinding(
        id="F001",
        reviewer_role="correctness-reviewer",
        title="Off by one in rate limit window",
        severity="high",
        category="correctness",
        description="Limit check uses <= instead of <",
        location="src/limiter.py:42",
    )
    f2 = ReviewFinding(
        id="F002",
        reviewer_role="security-reviewer",
        title="Rate limit window allows 101 requests",
        severity="high",
        category="security",
        description="Boundary comparison allows extra request",
        location="src/limiter.py:42",
    )
    result = ReviewReconciler.reconcile([f1, f2])
    assert len(result.confirmed) == 2
    assert result.unresolved_highs == 2


def test_reconciliation_dismissal_requires_reason():
    # Attempting to dismiss a high finding without reason must raise ReconciliationValidationError
    with pytest.raises(ReconciliationValidationError):
        ReviewFinding(
            id="F003",
            reviewer_role="regression-reviewer",
            title="Signature mismatch",
            severity="high",
            category="regression",
            description="Parameter renamed",
            status="false_positive",
            resolution_reason="",  # Missing required reason
        )

    # Valid dismissal with reason
    f = ReviewFinding(
        id="F004",
        reviewer_role="regression-reviewer",
        title="Signature mismatch",
        severity="high",
        category="regression",
        description="Parameter renamed",
        status="false_positive",
        resolution_reason="Target is private and has zero external callers.",
    )
    result = ReviewReconciler.reconcile([f])
    assert len(result.false_positives) == 1
    assert result.unresolved_highs == 0


# ============================================================================
# 6. Verification Plan & Runner Tests
# ============================================================================

def test_verification_plan_execution():
    plan = VerificationPlan(task_id="VERIF-01")
    plan.add_step(
        step_id="step-01",
        name="Echo Success",
        command=["python3", "-c", "import sys; sys.exit(0)"],
        category="unit_test",
    )
    plan.add_step(
        step_id="step-02",
        name="Echo Output",
        command=["python3", "-c", "print('hello verification')"],
        category="lint",
    )

    status = plan.execute_all()
    assert status == "passed"
    assert plan.steps[0].status == "verified"
    assert plan.steps[0].exit_code == 0
    assert plan.steps[1].status == "verified"
    assert "hello verification" in (plan.steps[1].stdout or "")


def test_verification_plan_failure():
    plan = VerificationPlan(task_id="VERIF-02")
    plan.add_step(
        step_id="step-01",
        name="Failing check",
        command=["python3", "-c", "import sys; sys.exit(1)"],
        category="security_check",
        required=True,
    )
    status = plan.execute_all()
    assert status == "failed"
    assert plan.steps[0].status == "failed"
    assert plan.steps[0].exit_code == 1


# ============================================================================
# 7. Evidence Ledger & Secret Redaction Tests
# ============================================================================

def test_evidence_ledger_redaction():
    text = "Authorization: Bearer sk-1234567890abcdef1234567890 and user email test@example.com"
    redacted = redact_sensitive_data(text)
    assert "sk-1234567890abcdef1234567890" not in redacted
    assert "test@example.com" not in redacted
    assert "[REDACTED" in redacted


def test_evidence_ledger_append_and_read():
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "evidence.jsonl"
        ledger = EvidenceLedger(str(ledger_path))

        entry1 = EvidenceEntry(
            task_id="TASK-99",
            agent_id="claude_code",
            action="task_created",
        )
        entry2 = EvidenceEntry(
            task_id="TASK-99",
            agent_id="claude_code",
            action="verification_executed",
            result="passed",
        )
        ledger.append_entry(entry1)
        ledger.append_entry(entry2)

        entries = ledger.get_task_entries("TASK-99")
        assert len(entries) == 2
        assert entries[0].action == "task_created"
        assert entries[1].result == "passed"


# ============================================================================
# 8. Performance Metrics Calculation Tests
# ============================================================================

def test_metrics_calculation_and_strict_first_pass_semantics():
    entries = [
        # Task 1: Truly clean first-pass success
        EvidenceEntry(task_id="T1", agent_id="claude_code", action="task_created", task_class="feature"),
        EvidenceEntry(
            task_id="T1",
            agent_id="claude_code",
            action="review_submitted",
            reviewing_agents=["correctness-reviewer", "test-falsifier"],
            findings_summary={"total": 0, "blocker": 0, "high": 0, "confirmed": 0},
        ),
        EvidenceEntry(task_id="T1", agent_id="claude_code", action="verification_executed", result="passed"),
        EvidenceEntry(task_id="T1", agent_id="claude_code", action="task_closed", result="complete"),

        # Task 2: Required reviewer-driven remediation -> NOT first-pass success
        EvidenceEntry(task_id="T2", agent_id="agy", action="task_created", task_class="bug_fix"),
        EvidenceEntry(
            task_id="T2",
            agent_id="agy",
            action="review_submitted",
            reviewing_agents=["security-reviewer", "test-falsifier"],
            findings_summary={"total": 2, "blocker": 1, "high": 1, "confirmed": 2},
            metadata={
                "reviewers_breakdown": {
                    "security-reviewer": {
                        "findings_total": 1,
                        "confirmed": 1,
                        "likely": 0,
                        "false_positives": 0,
                        "disputed": 0,
                        "blockers": 1,
                        "highs": 0,
                        "unique": 1,
                        "triggered_remediation": 1,
                        "prevented_bad_completion": 1,
                    },
                    "test-falsifier": {
                        "findings_total": 1,
                        "confirmed": 1,
                        "likely": 0,
                        "false_positives": 0,
                        "disputed": 0,
                        "blockers": 0,
                        "highs": 1,
                        "unique": 1,
                        "triggered_remediation": 1,
                        "prevented_bad_completion": 1,
                    },
                }
            },
        ),
        EvidenceEntry(task_id="T2", agent_id="agy", action="remediation_started"),
        EvidenceEntry(task_id="T2", agent_id="agy", action="remediation_completed", result="Fixed security blocker and test gap"),
        EvidenceEntry(task_id="T2", agent_id="agy", action="control_plane_defect_caught", metadata={"failure_mode": "security_vulnerability"}),
        EvidenceEntry(task_id="T2", agent_id="agy", action="verification_executed", result="passed"),
        EvidenceEntry(task_id="T2", agent_id="agy", action="task_closed", result="complete"),

        # Task 3: Verification failure -> NOT first-pass success
        EvidenceEntry(task_id="T3", agent_id="codex", action="task_created", task_class="refactor"),
        EvidenceEntry(task_id="T3", agent_id="codex", action="verification_executed", result="failed"),
        EvidenceEntry(task_id="T3", agent_id="codex", action="remediation_started"),
        EvidenceEntry(task_id="T3", agent_id="codex", action="remediation_completed"),
        EvidenceEntry(task_id="T3", agent_id="codex", action="verification_executed", result="passed"),
        EvidenceEntry(task_id="T3", agent_id="codex", action="task_closed", result="complete"),
    ]

    metrics = MetricsCalculator.calculate(entries)
    assert metrics.total_tasks == 3
    assert metrics.completed_tasks == 3
    assert metrics.tasks_requiring_remediation == 2
    # Only Task 1 is first-pass success; Tasks 2 and 3 required remediation/verif fix
    assert metrics.first_pass_successes == 1
    assert metrics.first_pass_success_rate == 0.33
    assert metrics.rework_cycles_total == 2
    assert metrics.total_review_findings == 2
    assert metrics.blocker_findings == 1
    assert metrics.high_findings == 1
    assert metrics.verification_failures == 1
    assert metrics.control_plane_caught_defects >= 2

    # Check Reviewer metrics
    assert "security-reviewer" in metrics.reviewer_summaries
    sec_sum = metrics.reviewer_summaries["security-reviewer"]
    assert sec_sum.reviewer_runs == 1
    assert sec_sum.blockers_found == 1
    assert sec_sum.unique_findings == 1
    assert sec_sum.findings_that_triggered_remediation == 1

    # Check Agent metrics
    claude_sum = metrics.agent_summaries["claude_code"]
    assert claude_sum.tasks_worked == 1
    assert claude_sum.first_pass_successes == 1
    assert claude_sum.first_pass_success_rate == 1.0

    agy_sum = metrics.agent_summaries["agy"]
    assert agy_sum.tasks_worked == 1
    assert agy_sum.first_pass_successes == 0
    assert agy_sum.first_pass_success_rate == 0.0
    assert agy_sum.remediation_cycles == 1

    md = metrics.render_markdown()
    assert "Multi-Agent Engineering Control Plane Operational Report" in md
    assert "security-reviewer" in md
    assert "First-pass success rate:** 33.3%" in md


def test_reconciliation_redundancy_and_unique_findings():
    f1 = ReviewFinding(
        id="F001",
        reviewer_role="correctness-reviewer",
        title="Null check missing in parser",
        severity="high",
        category="correctness",
        description="Parsing empty payload raises AttributeError",
        location="src/parser.py:50",
    )
    f2 = ReviewFinding(
        id="F002",
        reviewer_role="regression-reviewer",
        title="Empty payload crashes parser",
        severity="high",
        category="regression",
        description="Callers passing empty dict trigger crash",
        location="src/parser.py:50",
    )
    f3 = ReviewFinding(
        id="F003",
        reviewer_role="test-falsifier",
        title="Missing negative test for empty payload",
        severity="medium",
        category="test_gap",
        description="No test exercises empty dict input",
        location="tests/test_parser.py:100",
    )

    result = ReviewReconciler.reconcile([f1, f2, f3])
    assert result.duplicate_count + result.overlapping_count >= 1
    assert len(result.confirmed) == 2  # F1 and F2 confirmed due to co-location
    assert result.unique_findings_by_role.get("test-falsifier", 0) == 1


# ============================================================================
# 9. Project Adapter Tests
# ============================================================================

def test_project_adapter_discovery(tmp_path):
    # Setup mock repo with .ai-project.toml
    toml_file = tmp_path / ".ai-project.toml"
    toml_file.write_text(
        """
schema_version = 1
name = "test-service"
project_type = ["go", "backend"]
skills = ["software_development"]

[commands]
test = ["go", "test", "./..."]
build = ["go", "build", "./..."]
""",
        encoding="utf-8",
    )

    ctx = ProjectAdapter.discover(tmp_path)
    assert ctx.name == "test-service"
    assert ctx.has_manifest is True
    assert ctx.test_commands == [["go", "test", "./..."]]

    plan = ProjectAdapter.create_verification_plan(ctx, "TASK-T1")
    assert len(plan.steps) == 2
    assert plan.steps[0].category == "build"
    assert plan.steps[1].category == "unit_test"


# ============================================================================
# 10. Human Authority Boundary Tests
# ============================================================================

def test_human_boundary_gate_triggers():
    task = TaskSpec(
        task_id="DEPLOY-01",
        repository="infra",
        objective="Deploy new Kubernetes ingress",
        human_approval_requirements=["infrastructure_apply"],
    )
    res = HumanBoundaryGate.evaluate(task, planned_actions=["terraform apply"])
    assert res.requires_human_approval is True
    assert "infrastructure_apply" in res.triggered_boundaries
    assert res.decision_packet is not None

    md = res.decision_packet.render_markdown()
    assert "Human Authority Decision Packet" in md
    assert "infrastructure_apply" in md


def test_human_boundary_gate_clean():
    task = TaskSpec(
        task_id="SAFE-01",
        repository="repo",
        objective="Add docstrings",
        risk_level="low",
    )
    res = HumanBoundaryGate.evaluate(task, planned_actions=["edit docs.py", "pytest"])
    assert res.requires_human_approval is False
    assert res.decision_packet is None


# ============================================================================
# 11. Control Plane CLI Subcommand Tests
# ============================================================================

def test_cli_init_task_and_route(tmp_path):
    task_file = tmp_path / "task.yaml"
    init_code = cli_main([
        "init-task",
        "--task-id", "CLI-01",
        "--repo", "test_repo",
        "--objective", "CLI end to end test",
        "--risk", "medium",
        "--output", str(task_file),
    ])
    assert init_code == 0
    assert task_file.exists()

    route_code = cli_main(["route-task", "--task-file", str(task_file)])
    assert route_code == 0


def test_cli_briefs_and_boundary(tmp_path):
    task_file = tmp_path / "task.yaml"
    diff_file = tmp_path / "diff.patch"
    diff_file.write_text("+ def test(): pass\n", encoding="utf-8")

    cli_main([
        "init-task",
        "--task-id", "CLI-02",
        "--repo", "test_repo",
        "--objective", "Brief test",
        "--output", str(task_file),
    ])

    briefs_dir = tmp_path / "briefs"
    brief_code = cli_main([
        "briefs",
        "--task-file", str(task_file),
        "--diff-file", str(diff_file),
        "--output-dir", str(briefs_dir),
    ])
    assert brief_code == 0
    assert (briefs_dir / "brief_correctness-reviewer.md").exists()

    bound_code = cli_main([
        "check-boundary",
        "--task-file", str(task_file),
        "--actions", "pytest",
    ])
    assert bound_code == 0


def test_cli_prepare_run(tmp_path):
    task_file = tmp_path / "task.yaml"
    diff_file = tmp_path / "diff.patch"
    diff_file.write_text("+ def calculate_metrics(): pass\n", encoding="utf-8")

    cli_main([
        "init-task",
        "--task-id", "PREP-01",
        "--repo", "test_repo",
        "--objective", "Prepare run test",
        "--risk", "high",
        "--output", str(task_file),
    ])

    run_dir = tmp_path / "task_run"
    code = cli_main([
        "prepare-run",
        "--task-file", str(task_file),
        "--diff-file", str(diff_file),
        "--run-dir", str(run_dir),
    ])
    assert code == 0
    assert (run_dir / "task.yaml").exists()
    assert (run_dir / "diff.patch").exists()
    assert (run_dir / "findings_template.yaml").exists()
    assert (run_dir / "reviews" / "correctness-reviewer.md").exists()
    assert (run_dir / "reviews" / "security-reviewer.md").exists()
    assert (run_dir / "reviews" / "test-falsifier.md").exists()


def test_resolve_python_interpreter_precedence(tmp_path, monkeypatch):
    # 1. Active virtualenv
    fake_venv = tmp_path / "active_venv"
    fake_bin = fake_venv / "bin"
    fake_bin.mkdir(parents=True)
    fake_py = fake_bin / "python"
    fake_py.write_text("#!/bin/sh\nexit 0\n")
    fake_py.chmod(0o755)

    monkeypatch.setenv("VIRTUAL_ENV", str(fake_venv))
    resolved = resolve_python_interpreter(tmp_path)
    assert resolved == str(fake_py)

    # 2. Local ./venv
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    local_venv = tmp_path / "venv" / "bin"
    local_venv.mkdir(parents=True)
    local_py = local_venv / "python"
    local_py.write_text("#!/bin/sh\nexit 0\n")
    local_py.chmod(0o755)

    resolved_local = resolve_python_interpreter(tmp_path)
    assert resolved_local == str(local_py)


def test_metrics_dispatch_precedence_integrity():
    # Verify that independent properties on task_closed are not suppressed by if/elif
    entries = [
        EvidenceEntry(
            task_id="TASK-DISP-01",
            agent_id="claude_code",
            action="task_created",
            repository="Career_Agent_Core",
            is_override=True,
            override_reason="Operator preference for Go AST work",
        ),
        EvidenceEntry(
            task_id="TASK-DISP-01",
            agent_id="claude_code",
            action="review_submitted",
            reviewing_agents=["test-falsifier", "correctness-reviewer"],
            metadata={
                "reviewers_breakdown": {
                    "test-falsifier": {
                        "findings_total": 1,
                        "confirmed": 1,
                        "unique": 1,
                        "triggered_remediation": 1,
                        "prevented_bad_completion": 1,
                        "blockers": 0,
                    },
                    "correctness-reviewer": {
                        "findings_total": 1,
                        "confirmed": 1,
                        "unique": 1,
                        "triggered_remediation": 0,
                        "prevented_bad_completion": 0,
                        "blockers": 0,
                    }
                }
            }
        ),
        EvidenceEntry(
            task_id="TASK-DISP-01",
            agent_id="claude_code",
            action="remediation_completed",
            orchestration_action="manual_prompt_rewrite",
            remediation_cycles=1,
            control_plane_caught_defect=True,
            defect_type="review_caught_defect",
        ),
        EvidenceEntry(
            task_id="TASK-DISP-01",
            agent_id="claude_code",
            action="task_closed",
            result="complete",
            remediation_cycles=1,
            control_plane_caught_defect=True,
            findings_summary={"total": 2, "blocker": 0, "high": 1, "confirmed": 2},
            orchestration_action="manual_context_handoff",
        )
    ]

    summary = MetricsCalculator.calculate(entries)
    assert summary.total_tasks == 1
    assert summary.completed_tasks == 1
    assert summary.first_pass_successes == 0  # Remediation took place
    assert summary.tasks_requiring_remediation == 1
    assert summary.control_plane_caught_defects >= 1
    assert summary.review_caught_defects == 1
    assert summary.routing_overrides == 1
    assert summary.repositories_exercised.get("Career_Agent_Core") == 1
    assert summary.orchestration_counts.get("manual_prompt_rewrite") == 1
    assert summary.orchestration_counts.get("manual_context_handoff") == 1

    # Check marginal value for test-falsifier
    tf_sum = summary.reviewer_summaries["test-falsifier"]
    assert tf_sum.marginal_value >= 3  # unique (1) + triggered (1) + prevented (1)


def test_routing_reviewer_rationales():
    router = TaskRouter()
    spec = TaskSpec(
        task_id="TASK-R-01",
        repository="howlframe",
        objective="Security and VM memory limits",
        task_class="security_patch",
        risk_level="high",
        required_skills=["cyber_security", "software_development"],
        recommended_reasoning_tier="tier_1",
    )
    decision = router.route(spec)
    assert "security-reviewer" in decision.recommended_reviewers
    assert "architecture-reviewer" in decision.recommended_reviewers
    assert "security-reviewer" in decision.reviewer_selection_reasons
    assert len(decision.reviewer_selection_reasons["security-reviewer"]) > 5


def test_reviewer_requirements_replace_baseline():
    """Explicit reviewer_requirements override the default baseline set."""
    router = TaskRouter()
    spec = TaskSpec(
        task_id="TASK-R-02",
        repository="howlframe",
        objective="Evidence-only canary journal",
        task_class="bug_fix",
        risk_level="medium",
        reviewer_requirements=["correctness-reviewer", "regression-reviewer", "simplicity-reviewer"],
    )
    decision = router.route(spec)
    assert "test-falsifier" not in decision.recommended_reviewers
    assert "correctness-reviewer" in decision.recommended_reviewers
    assert "regression-reviewer" in decision.recommended_reviewers
    assert "simplicity-reviewer" in decision.recommended_reviewers


# ============================================================================
# 12. Repository Hygiene & SlopsLint Gate Tests
# ============================================================================

def test_repository_hygiene_verification_category():
    step = VerificationStep(
        step_id="step-hygiene-01",
        name="SlopsLint repository hygiene gate",
        command=["slopslint", "check", "--classify", "--enforce"],
        category="repository_hygiene",
    )
    assert step.category == "repository_hygiene"
    assert step.status == "claimed"

    plan = VerificationPlan(task_id="TASK-HYGIENE-01")
    plan.add_step(
        step_id="step-01",
        name="Hygiene Check",
        command=["python3", "-c", "import sys; sys.exit(0)"],
        category="repository_hygiene",
    )
    status = plan.execute_all()
    assert status == "passed"
    assert plan.steps[0].status == "verified"


def test_project_adapter_hygiene_unconfigured(tmp_path):
    ctx = ProjectAdapter.discover(tmp_path)
    assert ctx.hygiene_status == "not_configured"
    assert len(ctx.hygiene_commands) == 0

    plan = ProjectAdapter.create_verification_plan(ctx, "TASK-NO-SLOP")
    assert not any(s.category == "repository_hygiene" for s in plan.steps)


def test_project_adapter_hygiene_configured(tmp_path):
    slop_dir = tmp_path / ".slop"
    slop_dir.mkdir(parents=True)
    (slop_dir / "config.yml").write_text("schema: 1\n", encoding="utf-8")
    (slop_dir / "ceilings.yml").write_text("schema: 1\n", encoding="utf-8")

    ctx = ProjectAdapter.discover(tmp_path)
    assert ctx.hygiene_status in ("configured_and_passed", "configured_tool_missing")
    assert ctx.hygiene_commands == [["slopslint", "check", "--classify", "--enforce"]]

    plan = ProjectAdapter.create_verification_plan(ctx, "TASK-SLOP-01")
    hygiene_steps = [s for s in plan.steps if s.category == "repository_hygiene"]
    assert len(hygiene_steps) == 1
    assert hygiene_steps[0].command == ["slopslint", "check", "--classify", "--enforce"]


def test_project_adapter_hygiene_invalid_config(tmp_path):
    slop_dir = tmp_path / ".slop"
    slop_dir.mkdir(parents=True)
    (slop_dir / "config.yml").write_text("schema: 1\n", encoding="utf-8")
    # Ceilings file missing -> invalid_configuration

    ctx = ProjectAdapter.discover(tmp_path)
    assert ctx.hygiene_status == "invalid_configuration"


def test_human_boundary_gate_slop_debt_acceptance():
    # Agent attempts to add a tombstone to silently exempt debt -> MUST trigger human authority
    task = TaskSpec(
        task_id="TASK-SLOP-DEBT",
        repository="Career_Agent_Core",
        objective="Refactor parser with duplicate helper",
        risk_level="medium",
    )
    res = HumanBoundaryGate.evaluate(
        task,
        planned_actions=["slopslint tombstone add --id T-TEST --status accepted", "git commit"],
    )
    assert res.requires_human_approval is True
    assert "slop_debt_acceptance" in res.triggered_boundaries
    assert res.decision_packet is not None

    md = res.decision_packet.render_markdown()
    assert "slop_debt_acceptance" in md
    assert "Accepting new repository debt tombstone" in md


def test_tombstone_human_boundary_lifecycle_fail_closed():
    # Prove that an agent cannot bypass debt acceptance without human approval
    task = TaskSpec(
        task_id="TASK-TOMBSTONE-LIFECYCLE",
        repository="howlplane",
        objective="Propose tombstone for shared fixture",
        risk_level="medium",
    )
    task.transition_to("planned")
    task.transition_to("implementing")
    task.transition_to("reviewing")
    task.transition_to("remediating")
    task.transition_to("verifying")

    # Step 1: Verification gate detects hygiene failure
    plan = VerificationPlan(task_id="TASK-TOMBSTONE-LIFECYCLE")
    plan.add_step(
        step_id="step-hygiene",
        name="SlopsLint",
        command=["python3", "-c", "import sys; sys.exit(1)"],  # Simulated slop failure
        category="repository_hygiene",
    )
    v_status = plan.execute_all()
    assert v_status == "failed"

    # Step 2: Agent proposes tombstone creation rather than fixing
    proposed_actions = ["write .slop/tombstones/T-FIXTURE.yml"]
    boundary_res = HumanBoundaryGate.evaluate(task, planned_actions=proposed_actions, verification=plan)
    assert boundary_res.requires_human_approval is True
    assert "slop_debt_acceptance" in boundary_res.triggered_boundaries

    # Task enters awaiting_human
    task.transition_to("awaiting_human", reason="Debt tombstone proposed")
    assert task.current_state == "awaiting_human"

    # Invariant: Without explicit approval, silence is denial (fail-closed)
    # Simulated denial / rejection: Task cannot complete; it routes back to remediating or fails
    denial_decision = "rejected"
    if denial_decision == "rejected":
        # Task must remediate debt rather than accepting it
        task.transition_to("remediating", reason="Human rejected tombstone proposal; must eliminate duplicate code")
        assert task.current_state == "remediating"

    # Step 3: Human authorizes the debt (simulated approval on a separate approved task) -> transitions to verifying, tombstone applied, verif passes
    approved_task = TaskSpec(
        task_id="TASK-TOMBSTONE-APPROVED",
        repository="howlplane",
        objective="Approved legacy tombstone",
        risk_level="medium",
    )
    approved_task.transition_to("planned")
    approved_task.transition_to("implementing")
    approved_task.transition_to("reviewing")
    approved_task.transition_to("remediating")
    approved_task.transition_to("verifying")
    approved_task.transition_to("awaiting_human", reason="Awaiting human debt sign-off")

    plan_after_approval = VerificationPlan(task_id="TASK-TOMBSTONE-APPROVED")
    plan_after_approval.add_step(
        step_id="step-hygiene",
        name="SlopsLint",
        command=["python3", "-c", "import sys; sys.exit(0)"],
        category="repository_hygiene",
    )
    v_status_after = plan_after_approval.execute_all()
    assert v_status_after == "passed"

    approved_task.transition_to("complete", reason="Verification green and human explicitly approved tombstone")
    assert approved_task.current_state == "complete"


def test_slop_metrics_calculation():
    entries = [
        EvidenceEntry(
            task_id="TASK-METRIC-SLOP-01",
            agent_id="agy",
            action="verification_executed",
            command="slopslint check --classify --enforce",
            result="failed",
            defect_type="verification_caught_defect",
            control_plane_caught_defect=True,
            metadata={"category": "repository_hygiene", "failure_mode": "duplication_regression"},
        ),
        EvidenceEntry(
            task_id="TASK-METRIC-SLOP-01",
            agent_id="agy",
            action="debt_acceptance_requested",
            metadata={"boundary_triggered": ["slop_debt_acceptance"]},
        ),
        EvidenceEntry(
            task_id="TASK-METRIC-SLOP-01",
            agent_id="operator",
            action="human_decision",
            result="rejected",
            metadata={"boundary_triggers": ["slop_debt_acceptance"]},
        ),
        EvidenceEntry(
            task_id="TASK-METRIC-SLOP-01",
            agent_id="agy",
            action="slop_finding_remediated",
            result="Eliminated duplicate helper",
        ),
        EvidenceEntry(
            task_id="TASK-METRIC-SLOP-01",
            agent_id="agy",
            action="verification_executed",
            command="slopslint check --classify --enforce",
            result="passed",
            metadata={"category": "repository_hygiene"},
        ),
        EvidenceEntry(
            task_id="TASK-METRIC-SLOP-01",
            agent_id="agy",
            action="task_closed",
            result="complete",
        ),
    ]

    summary = MetricsCalculator.calculate(entries)
    assert summary.slop_checks_run == 2
    assert summary.slop_checks_failed == 1
    assert summary.duplication_regressions_caught == 1
    assert summary.debt_acceptance_requests == 1
    assert summary.debt_acceptance_rejected == 1
    assert summary.slop_findings_remediated == 1

    md = summary.render_markdown()
    assert "Repository Hygiene & Slop Gate Tracking" in md
    assert "Hygiene checks executed:** 2 (1 failed)" in md
    assert "Duplication regressions caught:** 1" in md
    assert "Debt acceptance requests:** 1 (0 approved, 1 rejected)" in md



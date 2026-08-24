"""
test_closed_loop_orchestrator.py

End-to-end deterministic integration tests for the closed-loop AI Engineering
Control Plane orchestrator, covering the complete governed lifecycle:
Discovery -> Shadow Audit -> Plan -> Implement -> Delta -> Adversarial Review ->
Reconciliation -> Remediation Loop -> Targeted Re-review -> Deterministic Verification ->
Human Authority Boundary Gate -> Governed Completion & Evidence.
"""

from pathlib import Path
import subprocess

from src.control_plane.agent_execution import FakeAgentBackend
from src.control_plane.launcher import cmd_work, cmd_status, build_parser
from src.control_plane.orchestrator import GovernedTaskOrchestrator, OrchestrationConfig
from src.control_plane.task_spec import TaskSpec


def _init_test_git_repo(path: Path) -> Path:
    """Helper to initialize a real git repository with pyproject and sample code."""
    path.mkdir(parents=True, exist_ok=True)
    for cmd in [
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "ci@howlplane.local"],
        ["git", "config", "user.name", "HowlPlane CI"],
    ]:
        subprocess.run(cmd, cwd=str(path), check=True, capture_output=True)

    (path / "AGENTS.md").write_text("# Test Project Engineering Context\n", encoding="utf-8")
    (path / "pyproject.toml").write_text(
        '[project]\nname = "auth_service"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (path / "src").mkdir()
    (path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (path / "src" / "auth.py").write_text(
        "def authenticate(username, token):\n    return False\n",
        encoding="utf-8",
    )
    (path / "tests").mkdir()
    (path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (path / "tests" / "test_auth.py").write_text(
        "from src.auth import authenticate\n\n\ndef test_stub():\n    assert authenticate('user', 'tok') is False\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(path), check=True)
    return path


# ============================================================================
# 1. Canonical End-to-End Demo Fixture: Multi-Defect Remediation to COMPLETE
# ============================================================================

def test_canonical_multi_defect_closed_loop_to_complete(tmp_path):
    repo = _init_test_git_repo(tmp_path / "canonical_repo")
    spec = TaskSpec(
        task_id="AUTH-101",
        repository="auth_service",
        objective="Fix authentication validation and add secure token verification",
        task_class="bug_fix",
        risk_level="high",
        required_skills=["software_development", "cyber_security"],
        recommended_reasoning_tier="tier_1",
    )

    cycle_state = {"attempt": 0}

    # Step 1: Implementation introduces buggy code with security flaw and missing test
    def initial_impl_side_effect(task, cwd, prompt):
        # Implementation has a security vulnerability (hardcoded secret bypass) and logic bug
        (cwd / "src" / "auth.py").write_text(
            "def authenticate(username, token):\n"
            "    if token == 'SUPER_SECRET_BACKDOOR_KEY':\n"
            "        return True\n"
            "    if username == '' or token == '':\n"
            "        return False\n"
            "    return True\n",
            encoding="utf-8",
        )
        (cwd / "tests" / "test_auth.py").write_text(
            "from src.auth import authenticate\n\n\n"
            "def test_auth_valid():\n"
            "    assert authenticate('admin', 'valid_token') is True\n",
            encoding="utf-8",
        )

    # Step 2: Reviewers discover defects on Cycle 1, then pass on Cycle 2
    def reviewer_fn(role_id, diff_content, task):
        if cycle_state["attempt"] == 0:
            if role_id == "security-reviewer":
                return """
findings:
  - id: "SEC-001"
    reviewer_role: "security-reviewer"
    title: "Hardcoded backdoor key in authentication handler"
    severity: "high"
    category: "security"
    location: "src/auth.py:2"
    claim: "Hardcoded secret token allows complete authentication bypass"
    evidence: "if token == 'SUPER_SECRET_BACKDOOR_KEY': return True"
    suggested_fix: "Remove backdoor check and use constant-time cryptographic token verification"
"""
            elif role_id == "test-falsifier":
                return """
findings:
  - id: "TST-001"
    reviewer_role: "test-falsifier"
    title: "Missing negative test cases for empty username or token"
    severity: "medium"
    category: "test_gap"
    location: "tests/test_auth.py"
    claim: "Test suite only tests the positive path"
    evidence: "test_auth.py has no assertions for invalid/empty inputs"
    suggested_fix: "Add test_auth_empty_credentials_rejected and test_auth_invalid_token"
"""
            return "findings: []\n"
        else:
            # Re-review after remediation is completely clean!
            return "findings: []\n"

    # Step 3: Remediation fixes all defects
    def remediation_fn(task, cwd, findings):
        cycle_state["attempt"] += 1
        # Remediation produces clean, secure implementation and thorough tests
        (cwd / "src" / "auth.py").write_text(
            "import hmac\n\n\n"
            "EXPECTED_TOKEN = 'secure_server_token_hash'\n\n\n"
            "def authenticate(username, token):\n"
            "    if not username or not token:\n"
            "        return False\n"
            "    # Constant-time comparison\n"
            "    return hmac.compare_digest(token, EXPECTED_TOKEN)\n",
            encoding="utf-8",
        )
        (cwd / "tests" / "test_auth.py").write_text(
            "from src.auth import authenticate, EXPECTED_TOKEN\n\n\n"
            "def test_auth_valid():\n"
            "    assert authenticate('admin', EXPECTED_TOKEN) is True\n\n\n"
            "def test_auth_empty_username_rejected():\n"
            "    assert authenticate('', EXPECTED_TOKEN) is False\n\n\n"
            "def test_auth_empty_token_rejected():\n"
            "    assert authenticate('admin', '') is False\n\n\n"
            "def test_auth_invalid_token_rejected():\n"
            "    assert authenticate('admin', 'bad_token') is False\n",
            encoding="utf-8",
        )

    impl_backend = FakeAgentBackend(
        agent_id="claude_code",
        side_effect=initial_impl_side_effect,
    )

    orchestrator = GovernedTaskOrchestrator(
        target_repo=repo,
        config=OrchestrationConfig(
            custom_backend=impl_backend,
            custom_reviewer_fn=reviewer_fn,
            custom_remediation_fn=remediation_fn,
            max_remediation_cycles=3,
        ),
    )

    res = orchestrator.run(spec)

    # 1. State machine earned COMPLETE
    assert res.final_state == "complete"
    assert res.exit_code == 0
    assert spec.current_state == "complete"

    # 2. Remediation cycle occurred
    assert res.remediation_cycles_count == 1
    assert len(res.review_cycles) == 2  # Cycle 1 (findings) -> Remediation -> Cycle 2 (clean)

    # 3. Final review is clean
    assert res.review_cycles[-1].status == "clean"
    assert len(res.review_cycles[-1].all_findings) == 0

    # 4. Verification plan passed
    assert res.verification_plan is not None
    assert res.verification_plan.overall_status == "passed"

    # 5. Delta captured accurately
    assert res.final_delta is not None
    assert "src/auth.py" in res.final_delta.files_modified
    assert "tests/test_auth.py" in res.final_delta.files_modified

    # 6. Artifacts persisted in .task_runs/AUTH-101/
    run_dir = repo / ".task_runs" / "AUTH-101"
    assert (run_dir / "task.yaml").exists()
    assert (run_dir / "baseline.json").exists()
    assert (run_dir / "route.json").exists()
    assert (run_dir / "diff.patch").exists()
    assert (run_dir / "findings.yaml").exists()
    assert (run_dir / "reconciliation.json").exists()
    assert (run_dir / "verification_result.json").exists()
    assert (run_dir / "summary.md").exists()


# ============================================================================
# 2. Human Authority Boundary Fixture: Terraform Apply -> AWAITING_HUMAN
# ============================================================================

def test_human_authority_boundary_blocks_complete(tmp_path):
    repo = _init_test_git_repo(tmp_path / "infra_repo")
    spec = TaskSpec(
        task_id="INFRA-201",
        repository="infra_service",
        objective="Apply Terraform production cluster configuration: terraform apply -auto-approve",
        task_class="infrastructure",
        risk_level="critical",
    )

    def impl_side_effect(task, cwd, prompt):
        (cwd / "main.tf").write_text('resource "aws_instance" "prod" {}\n', encoding="utf-8")

    impl_backend = FakeAgentBackend(
        agent_id="claude_code",
        side_effect=impl_side_effect,
    )

    orchestrator = GovernedTaskOrchestrator(
        target_repo=repo,
        config=OrchestrationConfig(
            custom_backend=impl_backend,
            custom_reviewer_fn=lambda role, diff, task: "findings: []",
        ),
    )

    res = orchestrator.run(spec, planned_actions=["terraform apply -auto-approve"])

    # Must pause at awaiting_human and return exit code 2
    assert res.final_state == "awaiting_human"
    assert res.exit_code == 2
    assert spec.current_state == "awaiting_human"

    # Decision packet generated
    run_dir = repo / ".task_runs" / "INFRA-201"
    assert (run_dir / "decision_packet.md").exists()
    decision_text = (run_dir / "decision_packet.md").read_text(encoding="utf-8")
    assert "Human Authority Decision Packet" in decision_text
    assert "infrastructure_apply" in decision_text


# ============================================================================
# 3. Failure Fixture: Implementation Agent Non-Zero Exit -> FAILED
# ============================================================================

def test_implementation_failure_transitions_to_failed(tmp_path):
    repo = _init_test_git_repo(tmp_path / "fail_impl_repo")
    spec = TaskSpec(
        task_id="FAIL-001",
        repository="auth_service",
        objective="Crash during implementation",
    )

    failing_backend = FakeAgentBackend(
        agent_id="claude_code",
        default_exit_code=1,
        default_stderr="Internal compiler error in agent backend",
    )

    orchestrator = GovernedTaskOrchestrator(
        target_repo=repo,
        config=OrchestrationConfig(custom_backend=failing_backend),
    )

    res = orchestrator.run(spec)

    assert res.final_state == "failed"
    assert res.exit_code == 1
    assert spec.current_state == "failed"
    assert "Internal compiler error" in (res.error_message or "")


# ============================================================================
# 4. Failure Fixture: Verification Suite Failure -> FAILED
# ============================================================================

def test_verification_failure_blocks_completion(tmp_path):
    repo = _init_test_git_repo(tmp_path / "fail_verif_repo")
    spec = TaskSpec(
        task_id="FAIL-002",
        repository="auth_service",
        objective="Introduce code that breaks pytest",
    )

    def broken_impl(task, cwd, prompt):
        (cwd / "src" / "auth.py").write_text("SYNTAX ERROR IN PYTHON CODE :::\n", encoding="utf-8")

    impl_backend = FakeAgentBackend(
        agent_id="claude_code",
        side_effect=broken_impl,
    )

    orchestrator = GovernedTaskOrchestrator(
        target_repo=repo,
        config=OrchestrationConfig(
            custom_backend=impl_backend,
            custom_reviewer_fn=lambda role, diff, task: "findings: []",
        ),
    )

    res = orchestrator.run(spec)

    assert res.final_state == "failed"
    assert res.exit_code == 1
    assert spec.current_state == "failed"
    assert res.verification_plan.overall_status == "failed"


# ============================================================================
# 5. Failure Fixture: Repeated Remediation Failure -> AWAITING_HUMAN
# ============================================================================

def test_repeated_remediation_failure_exceeds_max_cycles(tmp_path):
    repo = _init_test_git_repo(tmp_path / "repeated_fail_repo")
    spec = TaskSpec(
        task_id="FAIL-003",
        repository="auth_service",
        objective="Fix persistent security defect",
        risk_level="high",
    )

    # Reviewer always produces the same HIGH security finding
    def persistent_bad_reviewer(role_id, diff, task):
        return """
findings:
  - id: "PERSIST-01"
    reviewer_role: "security-reviewer"
    title: "Unresolved secret leak"
    severity: "high"
    category: "security"
"""

    impl_backend = FakeAgentBackend(agent_id="claude_code")

    orchestrator = GovernedTaskOrchestrator(
        target_repo=repo,
        config=OrchestrationConfig(
            custom_backend=impl_backend,
            custom_reviewer_fn=persistent_bad_reviewer,
            max_remediation_cycles=2,
        ),
    )

    res = orchestrator.run(spec)

    # Convergence safeguard triggers awaiting_human
    assert res.final_state == "awaiting_human"
    assert res.exit_code == 2
    assert res.remediation_cycles_count == 2
    assert "Remediation limit reached" in (res.error_message or "")


# ============================================================================
# 6. Path Scope Enforcement: out-of-scope edits are reverted before review/commit
# ============================================================================

def test_allowed_paths_enforcement_reverts_out_of_scope_edits(tmp_path):
    """
    A task with TaskSpec.allowed_paths must never present or commit changes to
    disallowed paths, both during initial implementation and during
    remediation. Regression for live acceptance canary where the agent
    expanded scope into src/control_plane/*.py.
    """
    repo = _init_test_git_repo(tmp_path / "scope_repo")
    journal = repo / "docs" / "journal.md"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("# initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/journal.md"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "Add journal"], cwd=str(repo), check=True)

    spec = TaskSpec(
        task_id="SCOPE-001",
        repository="scope_repo",
        objective="Update the journal",
        task_class="bug_fix",
        risk_level="medium",
        allowed_paths=["docs/journal.md"],
    )

    def impl_side_effect(task, cwd, prompt):
        # Agent touches both in-scope journal and out-of-scope production file.
        (cwd / "docs" / "journal.md").write_text("# updated\n", encoding="utf-8")
        (cwd / "src" / "auth.py").write_text("def authenticate(u, t):\n    return True\n", encoding="utf-8")

    review_pass = {"done": False}

    def reviewer_fn(role_id, diff, task):
        if not review_pass["done"] and "src/auth.py" not in diff:
            # First clean review forces remediation, which will try to expand scope.
            review_pass["done"] = True
            return """
findings:
  - id: "FMT-001"
    reviewer_role: "correctness-reviewer"
    title: "Missing header"
    severity: "high"
    category: "correctness"
    location: "docs/journal.md"
    claim: "Journal needs a title header"
    evidence: "Content lacks '# Title'"
    suggested_fix: "Add '# Title' to docs/journal.md"
"""
        # Re-review: flag only if scope enforcement leaked an out-of-scope edit.
        if "src/auth.py" in diff:
            return """
findings:
  - id: "BAD-001"
    reviewer_role: "correctness-reviewer"
    title: "Injected out-of-scope file"
    severity: "high"
    category: "correctness"
    location: "src/auth.py"
    claim: "Agent modified a disallowed file"
    evidence: "src/auth.py appears in the diff"
    suggested_fix: "Revert src/auth.py"
"""
        return "findings: []\n"

    def remediation_fn(task, cwd, findings):
        # Remediation fixes the in-scope finding and tries to expand scope.
        (cwd / "docs" / "journal.md").write_text("# Title\n# updated\n", encoding="utf-8")
        (cwd / "src" / "auth.py").write_text("# backdoor\n", encoding="utf-8")

    impl_backend = FakeAgentBackend(
        agent_id="claude_code",
        side_effect=impl_side_effect,
    )

    orchestrator = GovernedTaskOrchestrator(
        target_repo=repo,
        config=OrchestrationConfig(
            custom_backend=impl_backend,
            custom_reviewer_fn=reviewer_fn,
            custom_remediation_fn=remediation_fn,
            max_remediation_cycles=2,
        ),
    )

    res = orchestrator.run(spec)

    assert res.final_state == "complete"
    assert res.exit_code == 0
    assert res.remediation_cycles_count >= 1
    assert res.final_delta is not None
    assert "docs/journal.md" in res.final_delta.files_modified
    assert "src/auth.py" not in res.final_delta.files_modified
    # Both the implementation and remediation out-of-scope edits were reverted.
    assert (repo / "src" / "auth.py").read_text(encoding="utf-8") == (
        "def authenticate(username, token):\n    return False\n"
    )


# ============================================================================
# 7. CLI Integration: `ai work --execute` and `ai status`
# ============================================================================

def test_cli_work_execute_and_status(tmp_path, monkeypatch, capsys):
    repo = _init_test_git_repo(tmp_path / "cli_repo")
    parser = build_parser()

    # Test work dry-run mode (no --execute)
    args_dry = parser.parse_args(["work", "Improve auth logging", "--repo", str(repo), "--dry-run", "--skip-doctor"])
    ret_dry = cmd_work(args_dry)
    assert ret_dry == 0
    out_dry = capsys.readouterr().out
    assert "AI ENGINEERING CONTROL PLANE — TASK INITIALIZED" in out_dry

    # Test status output
    args_status = parser.parse_args(["status", "--repo", str(repo)])
    ret_status = cmd_status(args_status)
    assert ret_status == 0
    out_status = capsys.readouterr().out
    assert "AI CONTROL PLANE — PROJECT STATUS" in out_status
    assert "ACTIVE TASK RUNS (1):" in out_status

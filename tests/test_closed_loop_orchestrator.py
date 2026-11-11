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

from howlplane.control_plane.agent_execution import FakeAgentBackend
from howlplane.control_plane.launcher import cmd_work, cmd_status, build_parser
from howlplane.control_plane.orchestrator import GovernedTaskOrchestrator, OrchestrationConfig
from howlplane.control_plane.task_spec import TaskSpec
from tests._control_plane_home import private_control_plane
from tests._git_test_helpers import git_in_repo, init_git_repo
from tests._tia_helpers import record_valid_tia


def _orchestrator(repo: Path, tmp_path: Path, **config) -> GovernedTaskOrchestrator:
    """An orchestrator whose evidence and trajectories stay under tmp_path."""
    return GovernedTaskOrchestrator(
        target_repo=repo,
        control_plane_root=tmp_path / "control_plane",
        config=OrchestrationConfig(**config),
    )


def _init_test_git_repo(path: Path) -> Path:
    """Helper to initialize a real git repository with pyproject and sample code."""
    return init_git_repo(
        path,
        files={
            "AGENTS.md": "# Test Project Engineering Context\n",
            "pyproject.toml": '[project]\nname = "auth_service"\nversion = "0.1.0"\n',
            "src/__init__.py": "",
            "src/auth.py": "def authenticate(username, token):\n    return False\n",
            "tests/__init__.py": "",
            "tests/test_auth.py": (
                "from src.auth import authenticate\n\n\ndef test_stub():\n"
                "    assert authenticate('user', 'tok') is False\n"
            ),
        },
    )


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

    orchestrator = _orchestrator(
        repo, tmp_path,
        custom_backend=impl_backend,
        custom_reviewer_fn=reviewer_fn,
        custom_remediation_fn=remediation_fn,
        max_remediation_cycles=3,
    )
    record_valid_tia(orchestrator.ledger, spec.task_id)  # the completion gate requires a TIA

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

    orchestrator = _orchestrator(
        repo, tmp_path,
        custom_backend=impl_backend,
        custom_reviewer_fn=lambda role, diff, task: "findings: []",
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

    orchestrator = _orchestrator(repo, tmp_path, custom_backend=failing_backend)

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

    orchestrator = _orchestrator(
        repo, tmp_path,
        custom_backend=impl_backend,
        custom_reviewer_fn=lambda role, diff, task: "findings: []",
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

    orchestrator = _orchestrator(
        repo, tmp_path,
        custom_backend=impl_backend,
        custom_reviewer_fn=persistent_bad_reviewer,
        max_remediation_cycles=2,
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
    git_in_repo(repo, ["add", "docs/journal.md"])
    git_in_repo(repo, ["commit", "-m", "Add journal"])

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

    orchestrator = _orchestrator(
        repo, tmp_path,
        custom_backend=impl_backend,
        custom_reviewer_fn=reviewer_fn,
        custom_remediation_fn=remediation_fn,
        max_remediation_cycles=2,
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

def _use_unprobed_pool(monkeypatch):
    """Route CLI commands through a pool that never probes the real agent CLIs."""
    from howlplane.control_plane import launcher as launcher_module
    from howlplane.control_plane.synthesis.provider_pool import ProviderPoolManager

    pool = ProviderPoolManager(probe_on_start=False)
    monkeypatch.setattr(
        launcher_module.ProviderPoolManager,
        "from_config",
        classmethod(lambda cls, **kwargs: pool),
    )


def test_cli_work_execute_and_status(tmp_path, monkeypatch, capsys):
    _use_unprobed_pool(monkeypatch)
    repo = _init_test_git_repo(tmp_path / "cli_repo")
    private_control_plane(monkeypatch, tmp_path)
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


def test_cli_work_dry_run_with_agent_override_reports_the_override(tmp_path, monkeypatch, capsys):
    """`work --agent X --dry-run` must print the override note, not crash."""
    _use_unprobed_pool(monkeypatch)
    repo = _init_test_git_repo(tmp_path / "override_repo")
    private_control_plane(monkeypatch, tmp_path)
    parser = build_parser()

    args = parser.parse_args(
        ["work", "Improve auth logging", "--repo", str(repo), "--agent", "codex", "--dry-run", "--skip-doctor"]
    )
    ret = cmd_work(args)
    out = capsys.readouterr().out
    assert ret == 0, out
    assert "Override Note:" in out
    assert "(`codex`)" in out


# ============================================================================
# 8. Remediation limit policy (issues.md #23): LOW nits become follow-ups, and
#    anything that still reaches the owner arrives with verification evidence.
# ============================================================================

def _new_nit_every_review(severity):
    """A test-falsifier that raises a fresh finding on every review, as observed live."""
    calls = {"n": 0}

    def reviewer(role_id, diff, task):
        if role_id != "test-falsifier":
            return "findings: []\n"
        calls["n"] += 1
        return f"""
findings:
  - id: "NIT-{calls['n']}"
    reviewer_role: "test-falsifier"
    title: "Edge case {calls['n']} untested"
    severity: "{severity}"
    category: "test_gap"
    location: "tests/test_auth.py"
    claim: "One more input is not pinned"
    evidence: "no assertion for input {calls['n']}"
    suggested_fix: "Add a test for input {calls['n']}"
"""
    return reviewer


def _run_at_the_limit(tmp_path, task_id, severity, impl=None):
    repo = _init_test_git_repo(tmp_path / task_id)
    spec = TaskSpec(task_id=task_id, repository="auth_service", objective="Tighten auth input checks")
    orchestrator = _orchestrator(
        repo, tmp_path,
        custom_backend=FakeAgentBackend(agent_id="claude_code", side_effect=impl),
        custom_reviewer_fn=_new_nit_every_review(severity),
        custom_remediation_fn=lambda task, cwd, findings: None,
        max_remediation_cycles=2,
    )
    record_valid_tia(orchestrator.ledger, task_id)
    res = orchestrator.run(spec)
    res.ledger = orchestrator.ledger  # for tests that continue to approve and resume
    return res, repo / ".task_runs" / task_id


def _touch_auth(task, cwd, prompt):
    (cwd / "src" / "auth.py").write_text(
        "def authenticate(username, token):\n    return bool(username) and False\n", encoding="utf-8"
    )


def test_low_nits_left_at_the_remediation_limit_become_follow_ups_and_the_task_completes(tmp_path):
    res, run_dir = _run_at_the_limit(tmp_path, "LIMIT-LOW", "low", impl=_touch_auth)
    assert res.final_state == "complete", res.error_message
    assert res.remediation_cycles_count == 2
    assert res.verification_plan.overall_status == "passed"
    follow_ups = (run_dir / "follow_ups.md").read_text(encoding="utf-8")
    assert "[LOW] Edge case 3 untested" in follow_ups and "Add a test for input 3" in follow_ups
    assert "Follow-ups" in (run_dir / "summary.md").read_text(encoding="utf-8")
    assert not (run_dir / "decision_packet.md").exists()


def test_a_medium_finding_at_the_limit_reaches_the_owner_with_verification_evidence(tmp_path):
    res, run_dir = _run_at_the_limit(tmp_path, "LIMIT-MED", "medium", impl=_touch_auth)
    assert res.final_state == "awaiting_human" and res.exit_code == 2
    assert "Remediation limit reached" in (res.error_message or "")
    packet = (run_dir / "decision_packet.md").read_text(encoding="utf-8")
    assert "**Automated Verification:** `passed`" in packet
    assert "[MEDIUM] Edge case 3 untested (test-falsifier)" in packet
    assert "None recorded" not in packet.split("## Key Evidence")[1].split("##")[0]
    # Persisted as terminal, so approve + resume uses this result instead of re-running.
    import json
    assert json.loads((run_dir / "verification_plan.json").read_text())["overall_status"] == "passed"
    assert not (run_dir / "follow_ups.md").exists()


def test_failing_verification_at_the_limit_fails_the_task_instead_of_asking_the_owner(tmp_path):
    def broken(task, cwd, prompt):
        (cwd / "src" / "auth.py").write_text("SYNTAX ERROR :::\n", encoding="utf-8")

    res, run_dir = _run_at_the_limit(tmp_path, "LIMIT-FAIL", "medium", impl=broken)
    assert res.final_state == "failed" and res.exit_code == 1
    assert res.verification_plan.overall_status == "failed"
    assert "Review also left findings open" in (res.error_message or "")


# Independent review unavailable (issues.md #29): verification runs before the
# task parks, and the park reason names the true cause.

_UNAVAILABLE = "Independent review unavailable: no independent provider family completed review"
_RAISED_FINDING = """
findings:
  - id: "COR-1"
    reviewer_role: "correctness-reviewer"
    title: "Extra helper is untested"
    severity: "medium"
    category: "test_gap"
    location: "src/feature.py"
    claim: "extra() has no test"
    evidence: "no test imports extra"
    suggested_fix: "Add a test for extra"
"""


def _run_review_unavailable(tmp_path, task_id, source):
    """Run with only a same-family reviewer that raises a real finding."""
    from howlplane.control_plane.orchestrator import GovernedTaskOrchestrator, OrchestrationConfig
    from tests._dogfood_test_helpers import init_minimal_python_repo
    from tests.test_provider_failover import _FakeBackendResolver, _profile
    from tests.test_provider_lifecycle_defects import _make_pool

    repo = init_minimal_python_repo(tmp_path / task_id)
    resolver = _FakeBackendResolver({
        "g_impl": {
            "success": True,
            "side_effect": lambda _task, cwd, _prompt: (cwd / "src" / "feature.py").write_text(
                source, encoding="utf-8"
            ),
        },
        "g_rev": {"success": True, "stdout": _RAISED_FINDING},
    })
    pool = _make_pool(
        [_profile("g_impl", "GI", "google"), _profile("g_rev", "GR", "google")],
        resolver=resolver,
    )
    spec = TaskSpec(
        task_id=task_id, repository="test_repo", objective="Implement feature",
        acceptance_criteria=["Works"], reviewer_requirements=["correctness-reviewer"],
    )
    orchestrator = GovernedTaskOrchestrator(
        target_repo=repo,
        control_plane_root=tmp_path / "control_plane",
        config=OrchestrationConfig(
            provider_pool=pool, backend_resolver=resolver, acquire_locks=False,
            enable_howlframe_audit=False, preserve_independent_review=True,
        ),
    )
    record_valid_tia(orchestrator.ledger, task_id)
    res = orchestrator.run(spec)
    actions = [e.action for e in orchestrator.ledger.read_entries()[0] if e.task_id == task_id]
    return res, repo / ".task_runs" / task_id, actions


def _task_yaml_text(run_dir):
    """task.yaml re-serialised unwrapped, so a sentence is not split across lines."""
    import yaml
    data = yaml.safe_load((run_dir / "task.yaml").read_text(encoding="utf-8"))
    return yaml.dump(data, width=10**6)


_GOOD_SOURCE = "def run():\n    return True\n\n\ndef extra():\n    return 1\n"


def test_unavailable_review_with_passing_verification_parks_with_the_result(tmp_path):
    res, run_dir, actions = _run_review_unavailable(tmp_path, "UNAVAIL-OK", _GOOD_SOURCE)
    assert res.final_state == "awaiting_human" and res.exit_code == 2
    assert res.error_message == _UNAVAILABLE
    assert res.verification_plan.overall_status == "passed"
    steps = res.verification_plan.steps
    assert steps
    task_yaml = _task_yaml_text(run_dir)
    assert task_yaml.count(_UNAVAILABLE) == 1
    assert "Max remediation cycles exceeded" not in task_yaml
    packet = (run_dir / "decision_packet.md").read_text(encoding="utf-8")
    assert "**Automated Verification:** `passed`" in packet
    assert "independent_review_unavailable" in packet and "non_independent_review" in packet
    assert "[MEDIUM] Extra helper is untested (correctness-reviewer)" in packet
    for step in steps:
        assert packet.count(f"verification {step.name}: {step.status}") == 1
    assert "Extra helper is untested" in (run_dir / "reconciliation_report.md").read_text(encoding="utf-8")
    assert actions.count("verification_completed") == 1


def test_unavailable_review_with_failing_verification_fails_instead_of_parking(tmp_path):
    res, run_dir, actions = _run_review_unavailable(tmp_path, "UNAVAIL-FAIL", "SYNTAX ERROR :::\n")
    failed = [s.name for s in res.verification_plan.steps if s.status == "failed" and s.required]
    assert failed
    assert res.final_state == "failed" and res.exit_code == 1
    assert res.error_message == (
        f"Deterministic verification failed on required steps: {', '.join(failed)}. "
        "Independent review was also unavailable."
    )
    assert not (run_dir / "decision_packet.md").exists()
    assert actions.count("verification_completed") == 1


def test_remediation_limit_park_reason_is_unchanged(tmp_path):
    res, run_dir = _run_at_the_limit(tmp_path, "LIMIT-REASON", "medium", impl=_touch_auth)
    assert res.final_state == "awaiting_human"
    task_yaml = _task_yaml_text(run_dir)
    assert f"Max remediation cycles exceeded: {res.error_message}" in task_yaml
    assert _UNAVAILABLE not in task_yaml


def _cycle(*findings, status="has_findings", non_independent=()):
    from howlplane.control_plane.reconciliation import ReviewReconciler
    from howlplane.control_plane.review_runner import ReviewCycleResult
    return ReviewCycleResult(
        cycle_index=3, all_findings=list(findings),
        reconciliation=ReviewReconciler.reconcile(list(findings)),
        status=status, requires_remediation=True, non_independent_roles=list(non_independent),
    )


def _finding(fid, severity, category="test_gap", **extra):
    from howlplane.control_plane.reconciliation import ReviewFinding
    return ReviewFinding(id=fid, reviewer_role="test-falsifier", title=fid, severity=severity,
                         category=category, description="d", **extra)


def test_only_a_clean_low_only_review_is_deferred_to_follow_ups():
    from howlplane.control_plane.orchestrator import deferrable_follow_ups
    low, info = _finding("L", "low"), _finding("I", "informational")
    assert [f.id for f in deferrable_follow_ups(_cycle(low, info))] == ["L", "I"]
    # Anything the owner must judge is never deferred.
    assert deferrable_follow_ups(_cycle(_finding("L", "low"), _finding("M", "medium"))) is None
    assert deferrable_follow_ups(_cycle(_finding("H", "high"))) is None
    assert deferrable_follow_ups(_cycle(_finding("L", "low"), status="review_failure")) is None
    assert deferrable_follow_ups(_cycle(_finding("L", "low"), non_independent=["test-falsifier"])) is None
    assert deferrable_follow_ups(_cycle(_finding("S", "high", category="security"))) is None
    # A dismissed MEDIUM does not hold the task; a dismissed-only review has nothing to defer.
    dismissed = _finding("D", "medium", status="false_positive")
    assert [f.id for f in deferrable_follow_ups(_cycle(_finding("L", "low"), dismissed))] == ["L"]
    assert deferrable_follow_ups(_cycle(_finding("D", "medium", status="false_positive"))) is None


def test_approving_a_verified_escalation_completes_without_rerunning_the_gate(tmp_path):
    from howlplane.control_plane.human_boundary import HumanLifecycleManager
    res, run_dir = _run_at_the_limit(tmp_path, "LIMIT-OK", "medium", impl=_touch_auth)
    assert res.final_state == "awaiting_human"
    repo, ledger = run_dir.parent.parent, res.ledger
    HumanLifecycleManager.approve(target_repo=repo, task_id="LIMIT-OK", reason="LOW-risk nit",
                                  operator_source="cli", ledger=ledger)
    resumed = HumanLifecycleManager.resume(target_repo=repo, task_id="LIMIT-OK", ledger=ledger)
    assert resumed.final_state == "complete", resumed
    actions = [e.action for e in ledger.read_entries()[0] if e.task_id == "LIMIT-OK"]
    assert actions.count("verification_completed") == 1
    assert "verification_executed_on_resume" not in actions

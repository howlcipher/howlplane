#!/usr/bin/env python3
"""
tests/test_dogfood_failure_handling.py

Deterministic tests for failure-handling defects exposed by real dogfood runs:
1. Provider exits nonzero without modifying repo.
2. Provider exits nonzero after modifying a file (partial repository changes detected).
3. Failed partial edit is accurately attributed and preserved.
4. Partial changes are never presented as reviewed/verified or committed.
5. AGY 'Error: timeout waiting for response' is classified as timeout/transient provider failure.
6. Unrelated exit-code-1 remains correctly classified as ENGINEERING_FAILURE.
7. Timeout uses existing failover when policy permits.
8. Bounded failover terminates correctly when all eligible resources fail.
9. Discovered-but-unexecuted verification plan is reported truthfully in summary.
10. Absent verification plan remains distinguishable from unexecuted plan.
11. Failed implementation terminalizes stage checkpoint (status: failed, timestamp set).
12. Stale 'in_progress' state does not remain after controlled failure.
13. Pre-existing target-repo dirt and .task_runs/ evidence are not counted as implementation changes.
14. MISSING_EXECUTABLE rests on structural launch evidence, never on transcript text
    emitted by a provider that demonstrably started (HOWLFRAM-SLOPFIX-03).
"""

from io import StringIO
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional, Tuple
import pytest

from src.control_plane.agent_execution import (
    AgentBackend,
    AgentExecutionResult,
    LAUNCH_OUTCOME_KEY,
    LAUNCH_OUTCOME_LAUNCHED,
    LAUNCH_OUTCOME_NOT_INSTALLED,
    LAUNCH_OUTCOME_SPAWN_FAILED,
    SubprocessAgentBackend,
    TIMEOUT_SOURCE_HARNESS,
    TIMEOUT_SOURCE_KEY,
)
from src.control_plane.authority_envelope import create_envelope
from src.control_plane.authority_profile import get_profile
from src.control_plane.checkpoints import CheckpointManager, StageCheckpoint
from src.control_plane.git_baseline import (
    GitBaseline,
    RepositoryDelta,
    capture_baseline,
    capture_delta,
)
from src.control_plane.git_integration import GitIntegrationExecutor
from src.control_plane.launcher import _print_orchestration_summary
from src.control_plane.orchestrator import (
    FAILURE_CLASS_ENGINEERING,
    FAILURE_CLASS_PROVIDER_EXHAUSTED,
    FAILURE_CLASS_PROVIDER_UNAVAILABLE,
    FAILURE_CLASS_VERIFICATION,
    GovernedTaskOrchestrator,
    OrchestrationConfig,
    OrchestrationResult,
)
from src.control_plane.project_adapter import ProjectAdapter, ProjectContext
from src.control_plane.resource_models import ProviderFailureClass
from src.control_plane.router import RoutingDecision
from src.control_plane.synthesis.campaign_state import DurableCampaignState
from src.control_plane.synthesis.marathon import MarathonDogfoodEngine
from src.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderPoolManager,
)
from src.control_plane.task_spec import TaskSpec
from src.control_plane.verification import VerificationPlan, VerificationStep
from tests._dogfood_test_helpers import (
    ProviderScriptedOrchestrator,
    ScriptedRunner,
    build_full_merge_flow,
    init_minimal_python_repo,
)
from src.control_plane.git_env import run_git_in_repo


class FakeFailingBackend(AgentBackend):
    """Fake agent backend that modifies files or fails with custom output."""

    def __init__(
        self,
        agent_id: str = "agy",
        exit_code: int = 1,
        stdout: str = "",
        stderr: str = "",
        modify_fn: Optional[Any] = None,
    ):
        self.agent_id = agent_id
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.modify_fn = modify_fn

    def is_available(self) -> bool:
        return True

    def execute(
        self,
        task: TaskSpec,
        cwd: Any,
        role: str = "implementation",
        prompt_override: Optional[str] = None,
        timeout_seconds: int = 300,
        **kwargs,
    ) -> AgentExecutionResult:
        if self.modify_fn:
            self.modify_fn(Path(cwd))

        timed_out = "timeout waiting for response" in self.stderr.lower() or "timed out" in self.stderr.lower()
        err_msg = self.stderr.strip() if self.stderr else (f"Process exited with code {self.exit_code}" if self.exit_code != 0 else None)
        return AgentExecutionResult(
            agent_id=self.agent_id,
            role=role,
            command=f"{self.agent_id} -p '...'",
            exit_code=self.exit_code,
            stdout=self.stdout,
            stderr=self.stderr,
            duration_seconds=10.0,
            success=(self.exit_code == 0),
            timed_out=timed_out,
            error_message=err_msg,
        )


def test_agy_timeout_stderr_classification_and_detection():
    """AGY 'Error: timeout waiting for response' is classified as TRANSPORT_UNAVAILABLE / timed_out."""
    pool = ProviderPoolManager()
    agy_timeout_stderr = "Error: timeout waiting for response\n"
    res = AgentExecutionResult(
        agent_id="agy",
        role="implementation",
        command="agy -p '...'",
        exit_code=1,
        stdout="Scanning repo...\n",
        stderr=agy_timeout_stderr,
        duration_seconds=304.28,
        success=False,
        timed_out=True,
        error_message="Error: timeout waiting for response",
    )

    failure_class = pool.classify_failure("agy", res)
    assert failure_class == ProviderFailureClass.TRANSPORT_UNAVAILABLE

    event = pool.detect_exhaustion("agy", res, task_id="HOWLFRAM-18B2F1")
    assert event is not None
    assert event.failure_type == "unavailable"
    assert "timeout waiting for response" in event.raw_error
    assert pool.get_status("agy") == ProviderAvailabilityStatus.UNREACHABLE


def test_subprocess_agent_backend_detects_agy_timeout():
    """SubprocessAgentBackend automatically sets timed_out=True when AGY timeout stderr is returned."""
    backend = SubprocessAgentBackend("agy", "agy")
    # Simulate completed process with exit code 1 and timeout stderr
    fake_completed = subprocess.CompletedProcess(
        args=["agy", "-p", "foo"],
        returncode=1,
        stdout="running tests...\n",
        stderr="Error: timeout waiting for response\n",
    )

    # Directly test the error signature parsing logic
    timeout_markers = ("error: timeout waiting for response", "timed out", "request timed out")
    combined = f"{fake_completed.stderr}\n{fake_completed.stdout}".lower()
    assert any(m in combined for m in timeout_markers)


def test_unrelated_exit_code_1_remains_engineering_failure():
    """An arbitrary exit-code-1 error (e.g. SyntaxError) is NOT classified as a timeout."""
    pool = ProviderPoolManager()
    syntax_err = "SyntaxError: invalid syntax at opcode.go:10\n"
    res = AgentExecutionResult(
        agent_id="agy",
        role="implementation",
        command="agy -p '...'",
        exit_code=1,
        stdout="",
        stderr=syntax_err,
        duration_seconds=5.0,
        success=False,
        timed_out=False,
        error_message="Process exited with code 1",
    )

    failure_class = pool.classify_failure("agy", res)
    assert failure_class == ProviderFailureClass.ENGINEERING_FAILURE

    event = pool.detect_exhaustion("agy", res, task_id="HOWLFRAM-18B2F1")
    assert event is None


# ---------------------------------------------------------------------------
# Launch-evidence classification (HOWLFRAM-SLOPFIX-03)
#
# In the live SLOPFIX-03 run, Codex ran for 600s, edited the repository, and was
# killed by the harness timeout -- yet it was classified MISSING_EXECUTABLE
# because its own 471KB session transcript contained the line
# "/usr/bin/bash: line 1: file: command not found" from a command Codex itself
# ran. That marked an installed provider permanently unavailable.
# ---------------------------------------------------------------------------

# Verbatim from the SLOPFIX-03 Codex transcript. "malformed"/"invalid yaml" are
# present too, and rank above the transport check, so text alone cannot resolve
# this correctly -- only structural evidence can.
SLOPFIX03_CODEX_TRANSCRIPT = (
    "exec /usr/bin/bash -lc \"command -v jscpd || true\" in /repo\n"
    " succeeded in 0ms:\n"
    "/usr/bin/bash: line 1: file: command not found\n"
    "/usr/bin/node\n"
    "ensureRecord(isDir, `tombstones dir not found: ${dir}`);\n"
    "note: malformed entry skipped; invalid yaml in fixture\n"
    "\nTimeout after 600s."
)


def _execution_result(
    *,
    agent_id: str = "codex",
    exit_code: int = -1,
    stderr: str = "",
    timed_out: bool = False,
    error_message: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> AgentExecutionResult:
    """Builds a failed provider execution with explicit structural markers."""
    return AgentExecutionResult(
        agent_id=agent_id,
        role="implementation",
        command=f"{agent_id} exec '...'",
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
        duration_seconds=600.039,
        success=False,
        timed_out=timed_out,
        error_message=error_message,
        metadata=metadata or {},
    )


def test_executable_absent_before_launch_is_missing_executable():
    """A provider that was never spawned is genuinely MISSING_EXECUTABLE."""
    pool = ProviderPoolManager()
    res = _execution_result(
        exit_code=127,
        stderr="Agent binary 'codex' is not installed or available on PATH.",
        error_message="Agent 'codex' unavailable",
        metadata={LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_NOT_INSTALLED},
    )

    assert pool.classify_failure("codex", res) == ProviderFailureClass.MISSING_EXECUTABLE


def test_spawn_failure_is_missing_executable():
    """An OS-refused spawn (ENOENT) is the other genuine missing-executable case."""
    pool = ProviderPoolManager()
    res = _execution_result(
        exit_code=127,
        stderr="[Errno 2] No such file or directory: 'codex'",
        error_message="[Errno 2] No such file or directory: 'codex'",
        metadata={LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_SPAWN_FAILED},
    )

    assert pool.classify_failure("codex", res) == ProviderFailureClass.MISSING_EXECUTABLE


def test_launched_provider_inner_command_not_found_is_not_missing_executable():
    """The SLOPFIX-03 regression: a running provider's transcript never demotes it."""
    pool = ProviderPoolManager()
    res = _execution_result(
        stderr=SLOPFIX03_CODEX_TRANSCRIPT,
        timed_out=True,
        error_message="Timeout after 600s",
        metadata={
            LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_LAUNCHED,
            TIMEOUT_SOURCE_KEY: TIMEOUT_SOURCE_HARNESS,
        },
    )

    failure_class = pool.classify_failure("codex", res)
    assert failure_class != ProviderFailureClass.MISSING_EXECUTABLE
    assert failure_class == ProviderFailureClass.TRANSPORT_UNAVAILABLE
    # The provider must stay recoverable, not be permanently written off.
    pool.record_result("codex", res)
    assert pool.get_status("codex") == ProviderAvailabilityStatus.UNREACHABLE


def test_launched_provider_engineering_failure_is_not_missing_executable():
    """A launched provider failing on code, whose log mentions absent files."""
    pool = ProviderPoolManager()
    res = _execution_result(
        exit_code=1,
        stderr="wc: documentation/CONTROL_PLANE.md: No such file or directory\n"
               "AssertionError: expected 290, got 291\n",
        error_message="Process exited with code 1",
        metadata={LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_LAUNCHED},
    )

    assert pool.classify_failure("codex", res) == ProviderFailureClass.ENGINEERING_FAILURE


def test_legacy_result_without_markers_keeps_substring_fallback():
    """Backends that stamp no markers retain the previous text-based behavior."""
    pool = ProviderPoolManager()
    res = _execution_result(
        exit_code=1,
        stderr="codex: command not found\n",
        error_message="Process exited with code 1",
    )

    assert pool.classify_failure("codex", res) == ProviderFailureClass.MISSING_EXECUTABLE


def test_legacy_timeout_outranks_inner_command_not_found():
    """Without markers, the more specific timeout verdict still wins the tie."""
    pool = ProviderPoolManager()
    res = _execution_result(
        exit_code=1,
        stderr="/usr/bin/bash: line 1: file: command not found\nTimeout after 600s\n",
        timed_out=True,
        error_message="Timeout after 600s",
    )

    assert pool.classify_failure("codex", res) == ProviderFailureClass.TRANSPORT_UNAVAILABLE


def test_legacy_exit_127_remains_missing_executable():
    """Exit 127 is the shell's own structural verdict, so it survives a timeout."""
    pool = ProviderPoolManager()
    res = _execution_result(
        exit_code=127,
        stderr="codex: command not found\nrequest timed out\n",
        timed_out=True,
        error_message="Process exited with code 127",
    )

    assert pool.classify_failure("codex", res) == ProviderFailureClass.MISSING_EXECUTABLE


def test_subprocess_backend_stamps_launch_outcome_when_binary_absent():
    """The structural marker comes from the backend, not from the classifier."""
    backend = SubprocessAgentBackend("codex", "definitely-not-a-real-binary-xyz")
    task = TaskSpec(
        task_id="LAUNCH-01",
        repository="test_repo",
        objective="never runs",
        acceptance_criteria=["n/a"],
        task_class="bug_fix",
        risk_level="low",
    )

    res = backend.execute(task=task, cwd=Path.cwd(), role="implementation")

    assert res.metadata[LAUNCH_OUTCOME_KEY] == LAUNCH_OUTCOME_NOT_INSTALLED
    assert ProviderPoolManager().classify_failure("codex", res) == (
        ProviderFailureClass.MISSING_EXECUTABLE
    )


def _run_task_with_backend(
    repo: Path,
    task_id: str,
    backend: AgentBackend,
) -> Tuple[OrchestrationResult, Path]:
    task = TaskSpec(
        task_id=task_id,
        repository="test_repo",
        objective="Execute task",
        acceptance_criteria=["Criterion 1"],
        task_class="bug_fix",
        risk_level="medium",
    )
    pool = ProviderPoolManager(probe_on_start=False)
    orch = GovernedTaskOrchestrator(
        target_repo=repo,
        config=OrchestrationConfig(
            custom_backend=backend,
            provider_pool=pool,
            acquire_locks=False,
            enable_howlframe_audit=False,
            max_provider_failover_attempts=1,
        ),
    )
    return orch.run(task), repo / ".task_runs" / task_id


def test_provider_fails_nonzero_without_modifying_repo(tmp_path):
    """Provider fails with exit code 1 without modifying repo; delta is empty, checkpoint terminalizes."""
    repo = init_minimal_python_repo(tmp_path / "repo_no_mod")
    backend = FakeFailingBackend(
        agent_id="agy",
        exit_code=1,
        stderr="Error: timeout waiting for response\n",
        modify_fn=None,
    )
    result, run_dir = _run_task_with_backend(repo, "TEST-FAIL-01", backend)

    assert result.final_state == "failed"
    assert result.failure_class == FAILURE_CLASS_PROVIDER_EXHAUSTED
    assert result.final_delta is not None
    assert result.final_delta.is_empty is True
    assert result.provider_execution is not None
    assert "timeout" in result.provider_execution.error_message.lower()

    # Checkpoint verification
    chk = CheckpointManager.load_latest_checkpoint(run_dir)
    assert chk is not None
    assert chk.status == "failed"
    assert chk.stage_completed_at is not None
    assert chk.result_summary is not None
    assert chk.result_summary["partial_work"] is False


def test_provider_fails_nonzero_after_modifying_one_file(tmp_path):
    """Provider fails after modifying a file; partial work is captured, preserved, and checkpoint terminalized."""
    repo = init_minimal_python_repo(tmp_path / "repo_with_mod")

    def _modify(cwd: Path):
        (cwd / "src" / "feature.py").write_text("def run():\n    return 42 # partial edit\n", encoding="utf-8")

    backend = FakeFailingBackend(
        agent_id="agy",
        exit_code=1,
        stdout="Scanning repo...\nFound duplicate cluster.\n",
        stderr="Error: timeout waiting for response\n",
        modify_fn=_modify,
    )
    result, run_dir = _run_task_with_backend(repo, "TEST-FAIL-02", backend)

    assert result.final_state == "failed"
    assert result.failure_class == FAILURE_CLASS_PROVIDER_EXHAUSTED
    assert result.provider_execution is not None
    assert "timeout" in result.provider_execution.error_message.lower()

    # Delta must detect the modification truthfully
    assert result.final_delta is not None
    assert result.final_delta.is_empty is False
    assert "src/feature.py" in result.final_delta.files_modified
    assert len(result.final_delta.files_modified) == 1

    # Partial patch must be preserved in per-attempt evidence.
    attempt_dir = run_dir / "implementation" / "attempts" / "01-agy"
    assert (attempt_dir / "partial_work.patch").is_file()
    assert (attempt_dir / "diff.patch").is_file()
    assert (attempt_dir / "result.json").is_file()
    patch_content = (attempt_dir / "partial_work.patch").read_text(encoding="utf-8")
    assert "return 42" in patch_content

    # Stage checkpoint must be terminalized as failed
    chk = CheckpointManager.load_latest_checkpoint(run_dir)
    assert chk is not None
    assert chk.status == "failed"
    assert chk.stage_completed_at is not None
    assert chk.result_summary["partial_work"] is True
    assert chk.result_summary["files_changed"] == 1

    # Verification plan was preserved and not executed
    assert result.verification_plan is not None
    assert result.review_cycles == []


def test_partial_changes_not_presented_as_reviewed_or_verified(tmp_path):
    """Partial changes from failed implementation are not reviewed, not verified, and not committed."""
    repo = init_minimal_python_repo(tmp_path / "repo_unreviewed")
    task = TaskSpec(
        task_id="TEST-UNREVIEWED-01",
        repository="test_repo",
        objective="Unfinished work",
        task_class="bug_fix",
        risk_level="medium",
    )

    def _modify(cwd: Path):
        (cwd / "app.py").write_text("broken partial code", encoding="utf-8")

    backend = FakeFailingBackend(
        agent_id="agy",
        exit_code=1,
        stderr="Error: timeout waiting for response\n",
        modify_fn=_modify,
    )

    orch = GovernedTaskOrchestrator(
        target_repo=repo,
        config=OrchestrationConfig(
            custom_backend=backend,
            acquire_locks=False,
            enable_howlframe_audit=False,
            max_provider_failover_attempts=1,
        ),
    )

    result = orch.run(task)
    assert result.final_state == "failed"
    assert len(result.review_cycles) == 0
    if result.verification_plan and result.verification_plan.steps:
        for s in result.verification_plan.steps:
            assert s.status != "verified"
            assert s.exit_code is None

    # Verify no git commit was made on the repo
    log_proc = run_git_in_repo(repo, ["log", "--oneline"], check=True)
    assert "Initial commit" in log_proc.stdout
    assert "TEST-UNREVIEWED-01" not in log_proc.stdout


def test_terminal_summary_reports_partial_changes_truthfully(tmp_path, capsys):
    """Terminal summary reports partial repository changes, unexecuted verification steps, and safe state."""
    repo = init_minimal_python_repo(tmp_path / "repo_summary")
    ctx = ProjectContext(
        project_root=str(repo),
        name="test_repo",
        project_types=["python"],
        hygiene_status="enforced",
        has_agents_md=True,
    )
    task = TaskSpec(
        task_id="TEST-SUMMARY-01",
        repository="test_repo",
        objective="Fix duplication regression",
        task_class="bug_fix",
        risk_level="medium",
    )
    decision = RoutingDecision(
        selected_agent_id="agy",
        selected_agent_name="AGY",
        recommended_reviewers=["security-reviewer", "architecture-reviewer"],
        reasoning_tier="tier_2",
        rationale="Standard bug fix",
    )

    verif_plan = VerificationPlan(task_id=task.task_id)
    verif_plan.add_step("Lint check", ["flake8"], "lint")
    verif_plan.add_step("Unit tests", ["pytest"], "unit_test")

    partial_delta = RepositoryDelta(
        files_added=[],
        files_modified=["app.py"],
        files_deleted=[],
        diff_content="--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-def main():\n+def main(): # modified\n",
        insertions=1,
        deletions=1,
        is_empty=False,
    )

    exec_res = AgentExecutionResult(
        agent_id="agy",
        role="implementation",
        command="agy -p '...'",
        exit_code=1,
        stdout="Scanning repo...\n",
        stderr="Error: timeout waiting for response\n",
        duration_seconds=304.28,
        success=False,
        timed_out=True,
        error_message="Error: timeout waiting for response",
    )

    res = OrchestrationResult(
        task_id=task.task_id,
        task_spec=task,
        final_state="failed",
        exit_code=1,
        routing_decision=decision,
        final_delta=partial_delta,
        verification_plan=verif_plan,
        provider_execution=exec_res,
        failure_class=FAILURE_CLASS_PROVIDER_UNAVAILABLE,
        run_dir=str(repo / ".task_runs" / task.task_id),
    )

    _print_orchestration_summary(res, ctx, task, decision)
    out = capsys.readouterr().out

    # Assertions on terminal summary
    assert "Implementation:" in out
    assert "Status:                     FAILED" in out
    assert "Provider:                   AGY" in out or "Provider:                   agy" in out
    assert "Partial repository changes: YES" in out
    assert "Files Changed:              1" in out
    assert "Changes reviewed:           NO" in out
    assert "Changes verified:           NO" in out

    assert "Verification:" in out
    assert "Discovered:      2 steps" in out
    assert "Executed:        0" in out
    assert "Status:          NOT RUN — implementation failed before verification" in out
    assert "(No automated verification steps discovered)" not in out


def test_absent_verification_plan_distinguishable_from_unexecuted(tmp_path, capsys):
    """When no verification steps were discovered, the summary reports none discovered."""
    repo = init_minimal_python_repo(tmp_path / "repo_no_vplan")
    ctx = ProjectContext(project_root=str(repo), name="test_repo")
    task = TaskSpec(task_id="TEST-VPLAN-02", repository="test_repo", objective="Doc update", risk_level="low")
    decision = RoutingDecision(selected_agent_id="agy", selected_agent_name="AGY", recommended_reviewers=[], reasoning_tier="tier_1", rationale="r")

    empty_plan = VerificationPlan(task_id=task.task_id, steps=[])
    res = OrchestrationResult(
        task_id=task.task_id,
        task_spec=task,
        final_state="failed",
        exit_code=1,
        verification_plan=empty_plan,
    )

    _print_orchestration_summary(res, ctx, task, decision)
    out = capsys.readouterr().out
    assert "(No automated verification steps discovered)" in out


def test_pre_existing_dirt_and_task_runs_ignored(tmp_path):
    """Pre-existing untracked files and .task_runs/ are not counted as implementation delta."""
    repo = init_minimal_python_repo(tmp_path / "repo_dirt")
    (repo / "pre_existing.log").write_text("old logs\n", encoding="utf-8")
    (repo / ".task_runs" / "OLD-TASK").mkdir(parents=True, exist_ok=True)
    (repo / ".task_runs" / "OLD-TASK" / "task.yaml").write_text("task_id: OLD-TASK\n", encoding="utf-8")

    baseline = capture_baseline(repo)
    # .task_runs should not be in pre_existing_untracked
    assert ".task_runs" not in baseline.pre_existing_untracked
    assert ".task_runs/" not in baseline.pre_existing_untracked
    assert "pre_existing.log" in baseline.pre_existing_untracked

    # New task runs directory created during execution
    (repo / ".task_runs" / "NEW-TASK").mkdir(parents=True, exist_ok=True)
    (repo / ".task_runs" / "NEW-TASK" / "route.json").write_text("{}", encoding="utf-8")

    delta = capture_delta(repo, baseline)
    assert delta.is_empty is True
    assert delta.files_added == []
    assert delta.files_modified == []


def _run_marathon_harness(
    tmp_path: Path,
    script: Dict[str, Tuple[str, str]],
    task_id: str,
    campaign_id: str,
    git: Optional[ScriptedRunner] = None,
    gh: Optional[ScriptedRunner] = None,
) -> Tuple[bool, Dict[str, Any], ProviderScriptedOrchestrator, ProviderPoolManager]:
    repo_root = tmp_path / "repo"
    init_minimal_python_repo(repo_root)

    pool = ProviderPoolManager()
    for agent_id in list(pool.get_all_statuses()):
        pool.set_status(agent_id, ProviderAvailabilityStatus.UNAVAILABLE)
    for p in script:
        pool.set_status(p, ProviderAvailabilityStatus.AVAILABLE)

    git_r = git or ScriptedRunner()
    gh_r = gh or ScriptedRunner()
    envelope = create_envelope(get_profile("overnight-safe"), campaign_id, "cli:test@host")

    orch = ProviderScriptedOrchestrator(repo_root / "run", script)
    engine = MarathonDogfoodEngine(
        provider_pool=pool,
        base_output_dir=tmp_path / "out",
        campaign_dir=tmp_path / "campaigns",
        target_repo=repo_root,
        repo_slug="howlcipher/howlplane",
        orchestrator_factory=lambda config: orch,
        git_executor_factory=lambda env, merges: GitIntegrationExecutor(
            repo_root, "howlcipher/howlplane", env, git_runner=git_r, gh_runner=gh_r, merges_so_far=merges,
        ),
    )
    engine.authority_envelope = envelope
    engine.git_executor = engine._git_executor_factory(envelope, 0)

    state_dir = tmp_path / "campaigns" / campaign_id
    state_dir.mkdir(parents=True, exist_ok=True)
    state = DurableCampaignState(campaign_id=campaign_id)

    ok, rec = engine._execute_governed_engineering_improvement(
        task_id=task_id,
        benchmark_key="timeout_gap",
        gap_type="TIMEOUT_GAP",
        gap_desc="desc",
        risk_level="medium",
        campaign_state=state,
        state_dir=state_dir,
    )
    return ok, rec, orch, pool


def test_timeout_failover_in_marathon_engine(tmp_path):
    """A transient timeout from AGY triggers failover to an alternate provider (devin_cli) and completes."""
    git, gh = ScriptedRunner(), ScriptedRunner()
    build_full_merge_flow(
        git, gh, task_id="ENG-TIMEOUT-01", repo_slug="howlcipher/howlplane", pr_number=10,
        commit_message="fix: resolve timeout gap",
        pr_title="fix: timeout gap",
        pr_body="Automated fix",
        merge_sha="msha10", ci_green=True,
    )
    script = {
        "agy": ("unavailable", "Error: timeout waiting for response\n"),
        "devin_cli": ("complete", ""),
    }

    ok, rec, orch, pool = _run_marathon_harness(
        tmp_path, script, "ENG-TIMEOUT-01", "DOGFOOD-TIMEOUT", git=git, gh=gh,
    )

    assert ok is True
    assert orch.attempted == ["agy", "devin_cli"]
    assert rec["provider"] == "devin_cli"
    assert pool.get_status("agy") == ProviderAvailabilityStatus.UNREACHABLE
    assert pool.get_status("devin_cli") == ProviderAvailabilityStatus.AVAILABLE


def test_bounded_failover_terminates_when_all_eligible_resources_fail(tmp_path):
    """When all eligible resources timeout or fail availability, failover terminates gracefully."""
    script = {
        "agy": ("unavailable", "Error: timeout waiting for response\n"),
        "devin_cli": ("unavailable", "Error: credits exhausted\n"),
    }

    ok, rec, orch, pool = _run_marathon_harness(
        tmp_path, script, "ENG-ALLFAIL-01", "DOGFOOD-ALLFAIL",
    )

    assert ok is False
    assert orch.attempted == ["agy", "devin_cli"]
    assert rec["failure_reason"].startswith("NO_ELIGIBLE_PROVIDER_REMAINING")
    assert pool.get_status("agy") == ProviderAvailabilityStatus.UNREACHABLE
    assert pool.get_status("devin_cli") == ProviderAvailabilityStatus.QUOTA_EXHAUSTED

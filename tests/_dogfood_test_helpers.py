#!/usr/bin/env python3
"""
tests/_dogfood_test_helpers.py

Shared subprocess-boundary and orchestrator fakes for #59 real-integration
tests (test_git_integration.py, test_dogfood_hardening.py,
test_authority_profile.py, test_dogfood_crash_recovery_git.py,
test_dogfood_parking.py). Not itself a test module -- pytest only collects
`test_*.py`/`*_test.py` files, so this is never collected directly.

Fakes live ONLY at the external boundary (git/gh subprocess calls, the
GovernedTaskOrchestrator seam) -- the production GitIntegrationExecutor/
marathon.py logic exercised through them is real (#59 Phase 20).
"""

from dataclasses import dataclass, field
from pathlib import Path
import subprocess
from typing import Callable, Dict, List, Optional, Tuple, Union

from src.control_plane.agent_execution import AgentExecutionResult, FakeAgentBackend
from src.control_plane.git_baseline import RepositoryDelta
from src.control_plane.git_integration import PR_MERGE_FIELDS, GitIntegrationExecutor
from src.control_plane.orchestrator import (
    FAILURE_CLASS_AUTHORITY_BLOCKED,
    FAILURE_CLASS_ENGINEERING,
    GovernedTaskOrchestrator,
    OrchestrationConfig,
    OrchestrationResult,
)
from src.control_plane.reasoning import (
    ExecutionTrajectory,
    ReasoningExperiment,
    StrategyDefinition,
    StrategySnapshot,
    TrajectoryStore,
)
from src.control_plane.router import RoutingDecision
from src.control_plane.task_spec import TaskSpec
from src.control_plane.verification import VerificationPlan

from tests._git_test_helpers import init_git_repo


def init_minimal_python_repo(path: Path) -> Path:
    """Create the shared minimal real Git repository used by reasoning tests."""
    return init_git_repo(
        path,
        files={
            "AGENTS.md": "# Test Project\n",
            "pyproject.toml": (
                "[project]\nname = \"test_service\"\nversion = \"0.1.0\"\n"
            ),
            "src/__init__.py": "",
            "src/feature.py": "def run():\n    return True\n",
            "tests/__init__.py": "",
            "tests/test_feature.py": (
                "from src.feature import run\n\n\ndef test_run():\n"
                "    assert run() is True\n"
            ),
        },
    )


def reasoning_strategy(
    strategy_id: str,
    strategy_type: Optional[str] = None,
    immutable_config: Optional[Dict] = None,
) -> StrategyDefinition:
    """Build a compact strategy definition with a type inferred from its ID."""
    inferred_type = strategy_type or strategy_id.split(".", 1)[0]
    if inferred_type == "planning":
        inferred_type = "task_decomposition"
    if inferred_type == "review":
        inferred_type = "review_topology"
    return StrategyDefinition(
        strategy_id=strategy_id,
        version="v1",
        strategy_type=inferred_type,
        description="test strategy",
        immutable_config=immutable_config or {},
    )


def reasoning_experiment(
    experiment_id: str,
    *,
    experiment_type: str = "CONTEXT",
    baseline_id: Optional[str] = None,
    candidate_id: Optional[str] = None,
    strategy_type: Optional[str] = None,
    expected_outcome: str = "",
    falsification_criteria: Optional[List[str]] = None,
    metrics: Optional[List[str]] = None,
) -> ReasoningExperiment:
    """Build an experiment while keeping arm definitions explicit at call sites."""
    baseline = reasoning_strategy(baseline_id, strategy_type) if baseline_id else None
    candidate = reasoning_strategy(candidate_id, strategy_type) if candidate_id else None
    return ReasoningExperiment(
        experiment_id=experiment_id,
        experiment_type=experiment_type,
        baseline_strategy=StrategySnapshot.from_definition(baseline) if baseline else None,
        candidate_strategy=StrategySnapshot.from_definition(candidate) if candidate else None,
        expected_outcome=expected_outcome,
        falsification_criteria=falsification_criteria or [],
        metrics=metrics or [],
    )


def trajectory_summary(
    trajectory_id: str,
    *,
    verification_status: str = "passed",
    repair_cycles_count: int = 0,
    outcome: str = "success",
    **observable_metrics,
) -> Dict:
    """Build a compact observable trajectory summary for evaluator tests."""
    return {
        "trajectory_id": trajectory_id,
        "verification_status": verification_status,
        "repair_cycles_count": repair_cycles_count,
        "outcome": outcome,
        **observable_metrics,
    }


def record_reasoning_results(
    experiment: ReasoningExperiment,
    baseline_results: Optional[List[Dict]] = None,
    candidate_results: Optional[List[Dict]] = None,
) -> ReasoningExperiment:
    """Freeze an experiment and record explicit or neutral result summaries."""
    experiment.mark_started()
    experiment.record_results(
        baseline_results or [trajectory_summary("b1")],
        candidate_results or [trajectory_summary("c1")],
    )
    return experiment


def execution_trajectory(
    trajectory_id: str,
    *,
    task_id: Optional[str] = None,
    **fields,
) -> ExecutionTrajectory:
    """Build a trajectory with a deterministic test task identifier."""
    return ExecutionTrajectory(
        trajectory_id=trajectory_id,
        task_id=task_id or f"TASK-{trajectory_id}",
        **fields,
    )


def orchestration_result(
    tmp_path: Path,
    *,
    final_state: str = "complete",
    failure_class: str = "",
    review_cycles: Optional[List] = None,
    provider_execution=None,
) -> OrchestrationResult:
    """Build an observable result without running the governed loop."""
    init_minimal_python_repo(tmp_path / "repo")
    spec = TaskSpec(
        task_id="TEST-001",
        repository="test_service",
        objective="Test feature",
        task_class="feature",
        risk_level="medium",
    )
    verification = VerificationPlan(task_id=spec.task_id)
    verification.overall_status = (
        "passed" if final_state == "complete" else "failed"
    )
    routing = RoutingDecision(
        selected_agent_id="claude_code",
        selected_agent_name="Claude Code",
        reasoning_tier="tier_2",
        rationale="test routing",
        recommended_reviewers=["correctness-reviewer", "test-falsifier"],
    )
    return OrchestrationResult(
        task_id=spec.task_id,
        task_spec=spec,
        final_state=final_state,
        exit_code=0 if final_state == "complete" else 1,
        routing_decision=routing,
        review_cycles=review_cycles or [],
        verification_plan=verification,
        provider_execution=provider_execution,
        failure_class=failure_class or None,
        duration_seconds=1.5,
    )


def run_reasoning_task(
    target_repo: Path,
    trajectory_dir: Path,
    task_spec: TaskSpec,
    implementation,
    **config_overrides,
):
    """Run a governed task and load its single durable trajectory."""
    options = {
        "custom_backend": FakeAgentBackend(
            agent_id="claude_code", side_effect=implementation,
        ),
        "custom_reviewer_fn": lambda role, diff, task: "findings: []\n",
        "record_trajectory": True,
        "trajectory_store_dir": trajectory_dir,
    }
    options.update(config_overrides)
    orchestrator = GovernedTaskOrchestrator(
        target_repo=target_repo,
        config=OrchestrationConfig(**options),
    )
    result = orchestrator.run(task_spec)
    trajectories = TrajectoryStore(trajectory_dir).list_all()
    assert len(trajectories) == 1
    return result, trajectories[0]


def scripted_result(
    role: str, provider: str, exit_code: int = 0, stdout: str = "", stderr: str = "",
) -> AgentExecutionResult:
    """
    A single AgentExecutionResult with the given outcome, for fakes that
    script per-call behavior (#59.2 Phase 4). Keeping this construction in
    one place avoids every scripted fake reimplementing the same 5-line
    dataclass call.
    """
    return AgentExecutionResult(
        agent_id=provider, role=role, command=provider,
        exit_code=exit_code, stdout=stdout, stderr=stderr,
        duration_seconds=0.02, success=(exit_code == 0),
    )


def clean_review_result(role: str, provider: str) -> AgentExecutionResult:
    """
    A trivially valid, clean independent-review result for provider-failover
    tests that fake implementation/remediation execution but don't otherwise
    care about review content (#59.2 Phase 4). Keeping this in one place
    avoids every such fake reimplementing the same reviewer-role branch.
    """
    return scripted_result(role, provider, exit_code=0, stdout="findings: []")


@dataclass
class ScriptedRunner:
    """
    Stands in for `run_git`/`run_gh`. Responses are registered per exact
    argument list; each registration may be consumed once (FIFO) if
    registered multiple times for the same args, letting a test express
    "the Nth call to X returns Y, the N+1th call returns Z".
    """

    responses: Dict[Tuple[str, ...], List[subprocess.CompletedProcess]] = field(default_factory=dict)
    calls: List[Tuple[str, ...]] = field(default_factory=list)
    default_returncode: int = 0

    def on(self, args: List[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> "ScriptedRunner":
        key = tuple(args)
        self.responses.setdefault(key, []).append(
            subprocess.CompletedProcess(args=list(args), returncode=returncode, stdout=stdout, stderr=stderr)
        )
        return self

    def __call__(self, repo_root, args, timeout=60) -> subprocess.CompletedProcess:
        key = tuple(args)
        self.calls.append(key)
        queue = self.responses.get(key)
        if queue:
            return queue[0] if len(queue) == 1 else queue.pop(0)
        return subprocess.CompletedProcess(args=list(args), returncode=self.default_returncode, stdout="", stderr="")


def build_full_merge_flow(
    git: ScriptedRunner,
    gh: ScriptedRunner,
    *,
    task_id: str,
    repo_slug: str,
    pr_number: int,
    commit_message: str,
    pr_title: str,
    pr_body: str,
    modified_path: str = "src/x.py",
    base_branch: str = "main",
    final_sha: str = "finalsha",
    merge_sha: str = "mergesha",
    commit_sha: str = "",
    ci_green: bool = True,
) -> str:
    """
    Registers the standard branch->commit->push->PR->CI-observed sequence on
    `git`/`gh` (#59) -- the same call shape every real-lifecycle test needs,
    differing only in these identifiers. When `ci_green` is True, also
    registers the merge->remote-verify->local-sync tail; when False, CI
    reports a failing check and no merge step is registered (a merge call
    would be a test bug, not something to silently accept). Returns the
    branch name.
    """
    branch = f"fix/{task_id}"
    commit_sha = commit_sha or f"{task_id}-csha"

    git.on(["fetch", "origin", base_branch], returncode=0)
    git.on(["switch", "-c", branch, f"origin/{base_branch}"], returncode=0)
    git.on(["rev-parse", "--verify", branch], returncode=0, stdout="bsha\n")
    git.on(["add", "--", modified_path], returncode=0)
    git.on(["commit", "-m", commit_message], returncode=0)
    git.on(["rev-parse", "HEAD"], returncode=0, stdout=f"{commit_sha}\n")
    git.on(["push", "-u", "origin", branch], returncode=0)
    git.on(["rev-parse", branch], returncode=0, stdout=f"{commit_sha}\n")
    git.on(["ls-remote", "origin", branch], returncode=0, stdout=f"{commit_sha}\trefs/heads/{branch}\n")

    gh.on(
        ["pr", "create", "--repo", repo_slug, "--base", base_branch, "--head", branch,
         "--title", pr_title, "--body", pr_body],
        returncode=0,
    )
    gh.on(
        ["pr", "list", "--repo", repo_slug, "--head", branch, "--json", "number,url"],
        returncode=0, stdout=f'[{{"number": {pr_number}, "url": "https://github.com/{repo_slug}/pull/{pr_number}"}}]',
    )
    gh.on(["pr", "view", str(pr_number), "--json", "number"], returncode=0, stdout=f'{{"number": {pr_number}}}')
    # Rulesets are consulted first in production (#59.1): `main` on the real
    # repository is protected by a ruleset and the legacy protection endpoint
    # 404s. Shape matches the live `gh api repos/{slug}/rules/branches/main`
    # response.
    gh.on(
        ["api", f"repos/{repo_slug}/rules/branches/{base_branch}"],
        returncode=0,
        stdout=(
            '[{"type": "required_status_checks", "parameters": '
            '{"required_status_checks": [{"context": "test-python"}]}}]'
        ),
    )
    check_state = "SUCCESS" if ci_green else "FAILURE"
    check_bucket = "pass" if ci_green else "fail"
    gh.on(
        ["pr", "checks", str(pr_number), "--json", "name,state,bucket,link"],
        returncode=0, stdout=f'[{{"name": "test-python", "state": "{check_state}", "bucket": "{check_bucket}"}}]',
    )

    if not ci_green:
        return branch

    git.on(["merge-base", "--is-ancestor", merge_sha, f"origin/{base_branch}"], returncode=0)
    git.on(["switch", base_branch], returncode=0)
    git.on(["merge", "--ff-only", f"origin/{base_branch}"], returncode=0)
    git.on(["status", "--porcelain"], returncode=0, stdout="")
    git.on(["rev-parse", "HEAD"], returncode=0, stdout=f"{final_sha}\n")
    git.on(["rev-parse", f"origin/{base_branch}"], returncode=0, stdout=f"{final_sha}\n")

    gh.on(["pr", "view", str(pr_number), "--json", "headRefName"], returncode=0, stdout=f'{{"headRefName": "{branch}"}}')
    gh.on(["pr", "merge", str(pr_number), "--repo", repo_slug, "--squash", "--delete-branch"], returncode=0)
    gh.on(
        ["pr", "view", str(pr_number), "--repo", repo_slug, "--json", PR_MERGE_FIELDS],
        returncode=0,
        stdout=f'{{"state": "MERGED", "mergedAt": "2026-08-22T20:21:08Z", "mergeCommit": {{"oid": "{merge_sha}"}}}}',
    )
    return branch


def complete_result(task_spec, run_dir, modified_files, provider_execution=None) -> OrchestrationResult:
    """A successful governed implementation with a non-empty task-owned delta."""
    return OrchestrationResult(
        task_id=task_spec.task_id, task_spec=task_spec, final_state="complete", exit_code=0,
        final_delta=RepositoryDelta(
            files_modified=list(modified_files), diff_content="--- a/x\n+++ b/x\n",
            insertions=1, is_empty=False,
        ),
        run_dir=str(run_dir),
        provider_execution=provider_execution,
    )


def scripted_git_executor_factory(
    target_repo: Union[str, Path], repo_slug: str, git_runner: "ScriptedRunner", gh_runner: "ScriptedRunner",
) -> Callable[..., GitIntegrationExecutor]:
    """
    A `git_executor_factory` for MarathonDogfoodEngine that fakes only the
    git/gh subprocess boundary -- the production GitIntegrationExecutor logic
    it constructs is real. Factored out since multiple real-integration test
    modules (test_dogfood_hardening.py, test_acceptance_canary.py) build this
    exact factory shape.
    """
    return lambda envelope, merges_so_far: GitIntegrationExecutor(
        target_repo, repo_slug, envelope, git_runner=git_runner, gh_runner=gh_runner, merges_so_far=merges_so_far,
    )


def assert_fully_integrated(rec, merge_sha: str, commit_sha: str) -> None:
    """
    Asserts a GitIntegrationRecord reflects a complete, independently-verified
    real lifecycle (branch through remote-verified merge). Shared by every
    real-integration test that drives a governed task all the way through
    merge, so the same invariant isn't pinned as a near-identical assertion
    block in each one.
    """
    assert rec.integration_mode == "real"
    assert rec.is_fully_integrated() is True
    assert rec.branch_observed and rec.commit_observed and rec.push_observed
    assert rec.pr_observed and rec.required_checks_green and rec.merge_observed
    assert rec.remote_main_contains_merge is True
    assert rec.merge_sha == merge_sha
    assert rec.commit_sha == commit_sha


class FakeOrchestrator:
    """
    Fakes GovernedTaskOrchestrator at the boundary marathon.py actually
    calls through `orchestrator_factory`. Always reports a complete governed
    implementation with a non-empty delta. The orchestrator's own lifecycle
    is exhaustively covered elsewhere (test_closed_loop_orchestrator.py,
    test_operational_resilience.py).
    """

    def __init__(self, run_dir: Union[str, Path], modified_files: Union[str, List[str]] = "src/x.py"):
        self.run_dir = run_dir
        self.modified_files = [modified_files] if isinstance(modified_files, str) else list(modified_files)

    def run(self, task_spec, planned_actions=None) -> OrchestrationResult:
        return complete_result(task_spec, self.run_dir, self.modified_files)


class ProviderScriptedOrchestrator:
    """
    Fakes GovernedTaskOrchestrator with a per-provider script, so a test can
    say "agy returns this quota stderr, devin_cli succeeds" and then assert
    which providers were actually attempted, in order (#59.1 Phase 2).

    Each script entry is keyed by the provider the marathon layer selected
    (`task_spec.preferred_agent`) and is one of:
      ("exhausted", stderr)   -> failed run whose AgentExecutionResult carries
                                 provider-availability text
      ("engineering", stderr) -> failed run that is genuinely bad output
      ("blocked", reason)     -> orchestrator's own authority gate tripped
      ("complete", None)      -> successful governed implementation

    A provider with no script entry defaults to ("complete", None).
    """

    def __init__(
        self,
        run_dir: Union[str, Path],
        script: Dict[str, Tuple[str, Optional[str]]],
        modified_files: Union[str, List[str]] = "src/x.py",
        on_attempt: Optional[Callable[[str], None]] = None,
    ):
        self.run_dir = run_dir
        self.script = dict(script)
        self.modified_files = [modified_files] if isinstance(modified_files, str) else list(modified_files)
        self.attempted: List[str] = []
        self.on_attempt = on_attempt

    def run(self, task_spec, planned_actions=None) -> OrchestrationResult:
        provider = task_spec.preferred_agent or "unknown"
        self.attempted.append(provider)
        if self.on_attempt is not None:
            self.on_attempt(provider)
        outcome, detail = self.script.get(provider, ("complete", None))

        def _exec(exit_code: int, stderr: str, success: bool) -> AgentExecutionResult:
            return AgentExecutionResult(
                agent_id=provider, role="implementation", command=f"{provider} -p '...'",
                exit_code=exit_code, stdout="", stderr=stderr, duration_seconds=1.0,
                success=success, error_message=None if success else f"Process exited with code {exit_code}",
            )

        if outcome == "complete":
            return complete_result(task_spec, self.run_dir, self.modified_files, _exec(0, "", True))
        if outcome == "blocked":
            return OrchestrationResult(
                task_id=task_spec.task_id, task_spec=task_spec, final_state="awaiting_human", exit_code=2,
                run_dir=str(self.run_dir), error_message=detail or "authority boundary",
                provider_execution=_exec(0, "", True),
                failure_class=FAILURE_CLASS_AUTHORITY_BLOCKED,
            )
        return OrchestrationResult(
            task_id=task_spec.task_id, task_spec=task_spec, final_state="failed", exit_code=1,
            run_dir=str(self.run_dir), error_message=detail or "failed",
            provider_execution=_exec(1, detail or "", False),
            failure_class=FAILURE_CLASS_ENGINEERING,
        )

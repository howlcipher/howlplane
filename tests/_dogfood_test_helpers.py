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

from src.control_plane.agent_execution import AgentExecutionResult
from src.control_plane.git_baseline import RepositoryDelta
from src.control_plane.git_integration import PR_MERGE_FIELDS
from src.control_plane.orchestrator import (
    FAILURE_CLASS_AUTHORITY_BLOCKED,
    FAILURE_CLASS_ENGINEERING,
    OrchestrationResult,
)


def clean_review_result(role: str, provider: str) -> AgentExecutionResult:
    """
    A trivially valid, clean independent-review result for provider-failover
    tests that fake implementation/remediation execution but don't otherwise
    care about review content (#59.2 Phase 4). Keeping this in one place
    avoids every such fake reimplementing the same reviewer-role branch.
    """
    return AgentExecutionResult(
        agent_id=provider, role=role, command=provider,
        exit_code=0, stdout="findings: []", stderr="",
        duration_seconds=0.02, success=True,
    )


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

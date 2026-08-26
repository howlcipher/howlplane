#!/usr/bin/env python3
"""
git_integration.py

Real git/GitHub integration for governed engineering tasks (#59 Phases 3-6).
Replaces the fabricated branch/commit/PR/CI/merge values previously
hand-built in synthesis/marathon.py with actual `git`/`gh` subprocess calls,
each independently verified before the corresponding GitIntegrationRecord
flag is set.

Design principles carried over from the rest of the control plane:
  - Never `shell=True` (mirrors git_baseline.py's `_run_git_cmd`).
  - Never `git add -A` / `git add .` -- only the explicit path list from the
    orchestrator's already-computed RepositoryDelta is staged.
  - Every step is independently re-verified against observable reality
    (`git ls-remote`, `gh pr view`, `git merge-base --is-ancestor`) rather
    than trusting a prior command's exit code alone.
  - `merged=True` is only ever reachable after verify_remote_main_contains()
    returns True -- there is no other code path that sets it.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from src.control_plane.executor import (
    AuthorityExecutor,
    ExecutionReceipt,
    ExecutionResult,
    EXECUTION_RECEIPT_SCHEMA_VERSION,
)
from src.control_plane.authority_envelope import (
    AuthorityDecision,
    AuthorityEnvelope,
    evaluate_action_against_envelope,
)
from src.control_plane.git_env import run_git_in_repo, sanitized_git_env
from src.control_plane.proposed_action import ProposedAction

GIT_INTEGRATION_EXECUTOR_VERSION = "1.0"

# Why the required-check policy could not authorize an unattended merge.
# A stale hard-coded list must never stand in for live policy (#59.1
# Blocker 3): there is deliberately no fallback constant here any more.
CI_POLICY_UNAVAILABLE = "CI_POLICY_UNAVAILABLE"
NO_REQUIRED_CHECKS_ENFORCED = "NO_REQUIRED_CHECKS_ENFORCED"

# Fields requested when independently verifying a merge. `gh pr view` exposes
# no boolean `merged` field -- asking for one makes gh exit non-zero with
# "Unknown JSON field", so verification failed *after* a real merge had already
# happened: the PR was merged but HowlPlane recorded merge_observed=False and
# the task never reached full integration. Observed live on gh 2.97.0 while
# merging PR #28 (#59.1).
PR_MERGE_FIELDS = "state,mergedAt,mergeCommit"


def pr_is_merged(data: Dict[str, Any]) -> bool:
    """
    Whether `gh pr view` reports a PR as genuinely merged. Requires GitHub's
    own MERGED state or a mergedAt timestamp -- never inferred from the merge
    command's exit code alone.
    """
    return str(data.get("state") or "").upper() == "MERGED" or bool(data.get("mergedAt"))


POLICY_SOURCE_RULESET = "ruleset"
POLICY_SOURCE_BRANCH_PROTECTION = "branch_protection"
POLICY_SOURCE_UNAVAILABLE = "unavailable"

# Branch-name pattern for campaign/task-owned branches. Anything not
# matching this is never eligible for the automated merge gate.
TASK_BRANCH_PATTERN = re.compile(r"^fix/[A-Za-z0-9_.\-]+$")

# The live acceptance journal is written before its governed integration
# lifecycle begins.  It must therefore never claim that merge success is an
# established fact.  This check deliberately lives in the production commit
# boundary so a path scoped task cannot commit a fabricated outcome.
LIVE_ACCEPTANCE_JOURNAL_PATTERN = re.compile(
    r"^documentation/task_journals/\d{4}-\d{2}-\d{2}_live_autonomous_acceptance\.md$"
)
# This must reject outcome assertions, not merely a few preferred phrasings.
# An acceptance journal is created before the lifecycle begins, so claims that
# a PR landed, GitHub incorporated a change into main, or remote main was
# successfully reconciled are equally premature even when they avoid "merge".
PREMATURE_MERGE_SUCCESS_PATTERN = re.compile(
    r"\b(?:"
    r"(?:the\s+)?merge(?:\s+lifecycle)?\s+(?:(?:has\s+)?succeed(?:ed|s)?|"
    r"(?:is|was)\s+successful|(?:is|was|has\s+been)\s+complet(?:e|ed)|completed)|"
    r"(?:the\s+)?(?:[\w-]+\s+){0,5}[\w-]*merge[\w-]*\s+lifecycle\s+"
    r"(?:was\s+)?(?:executed|completed)\s+successfully|"
    r"(?:successfully\s+)?merged(?:\s+successfully)?|"
    r"(?:github\s+)?(?:has\s+)?incorporated\s+(?:this\s+)?(?:change|pull\s+request|pr)\s+into\s+(?:the\s+)?main|"
    r"(?:the\s+)?(?:change|pull\s+request|pr)\s+(?:has\s+)?landed\s+(?:on|in)\s+(?:the\s+)?main|"
    r"(?:the\s+)?(?:change|pull\s+request|pr)\s+(?:has\s+been\s+)?(?:integrated|merged)\s+into\s+(?:the\s+)?main|"
    r"(?:the\s+)?(?:change|pull\s+request|pr)\s+(?:is\s+)?now\s+(?:on|in)\s+(?:the\s+)?main|"
    r"(?:the\s+)?remote\s+main\s+(?:(?:has\s+been|was)\s+)?(?:successfully\s+)?(?:verified|updated|synchronized|synced|reconciled)|"
    r"(?:the\s+)?remote[-\s]?main[-\s]?(?:verification|sync)\s+(?:has\s+)?succeed(?:ed|s)?|"
    r"(?:the\s+)?merge\s+(?:has\s+)?landed|"
    r"(?:the\s+)?pull\s+request\s+(?:has\s+)?landed"
    r")\b",
    re.IGNORECASE,
)

CommandRunner = Callable[[Union[str, Path], List[str], int], subprocess.CompletedProcess]


class GitIntegrationError(Exception):
    """Base exception for real git/GitHub integration failures."""
    pass


def run_git(repo_root: Union[str, Path], args: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Executes a git command deterministically without shell=True.

    An inherited GIT_DIR overrides `git -C`, so the environment is sanitized
    before every invocation (see git_env.GIT_REPOSITORY_SELECTION_ENV_VARS).
    """
    return run_git_in_repo(repo_root, args, timeout=timeout)


def run_gh(repo_root: Union[str, Path], args: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Executes a `gh` CLI command deterministically without shell=True."""
    return subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        ["gh"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(repo_root),
        check=False,
        # `gh` shells out to git for repo detection; the same inherited
        # repository-selection variables would redirect it.
        env=sanitized_git_env(),
    )


@dataclass
class GhAuthStatus:
    authenticated: bool
    account: Optional[str] = None
    scopes: List[str] = field(default_factory=list)
    detail: str = ""


def check_gh_auth(
    repo_root: Union[str, Path] = ".",
    gh_runner: CommandRunner = run_gh,
) -> GhAuthStatus:
    """
    Verifies GitHub CLI authentication by actually invoking `gh auth status`
    -- never assumes credentials are usable merely because the `gh` binary
    is present on PATH (#59 Phase 3).
    """
    try:
        proc = gh_runner(repo_root, ["auth", "status"], 15)
    except (OSError, subprocess.SubprocessError) as exc:
        return GhAuthStatus(authenticated=False, detail=f"gh auth status failed to execute: {exc}")

    combined = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0 or "Logged in to" not in combined:
        return GhAuthStatus(authenticated=False, detail=combined.strip()[:500])

    account_match = re.search(r"account\s+(\S+)", combined)
    scopes_match = re.search(r"Token scopes:\s*(.+)", combined)
    scopes = []
    if scopes_match:
        scopes = [s.strip().strip("'") for s in scopes_match.group(1).split(",")]

    return GhAuthStatus(
        authenticated=True,
        account=account_match.group(1) if account_match else None,
        scopes=scopes,
        detail=combined.strip()[:500],
    )


@dataclass
class GitPreflightResult:
    is_git_repo: bool
    remote_ok: bool
    remote_url: str
    expected_repo_slug: str
    push_capable: bool
    gh_auth: GhAuthStatus
    blocked_reason: Optional[str] = None


def _remote_slug(remote_url: str) -> Optional[str]:
    """Extracts `owner/repo` from an https or ssh GitHub remote URL."""
    m = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(\.git)?/?$", remote_url.strip())
    return m.group("slug") if m else None


def detect_repo_slug(repo_root: Union[str, Path], git_runner: CommandRunner = run_git) -> Optional[str]:
    """Reads `owner/repo` from the `origin` remote, or None if undeterminable."""
    proc = git_runner(repo_root, ["remote", "get-url", "origin"], 15)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return _remote_slug(proc.stdout.strip())


def run_preflight(
    repo_root: Union[str, Path],
    expected_repo_slug: str,
    git_runner: CommandRunner = run_git,
    gh_runner: CommandRunner = run_gh,
) -> GitPreflightResult:
    """
    Real git/GitHub preflight (#59 Phase 3): confirms this is a git repo, the
    remote points at the expected repository, and GitHub credentials
    actually permit PR/CI/merge operations. Never fabricates capability --
    any failure produces an actionable `blocked_reason` (e.g.
    "BLOCKED_GITHUB_AUTH") instead of proceeding.
    """
    is_repo_proc = git_runner(repo_root, ["rev-parse", "--is-inside-work-tree"], 15)
    is_git_repo = is_repo_proc.returncode == 0 and is_repo_proc.stdout.strip() == "true"
    if not is_git_repo:
        return GitPreflightResult(
            is_git_repo=False, remote_ok=False, remote_url="", expected_repo_slug=expected_repo_slug,
            push_capable=False, gh_auth=GhAuthStatus(authenticated=False),
            blocked_reason="NOT_A_GIT_REPOSITORY",
        )

    remote_proc = git_runner(repo_root, ["remote", "get-url", "origin"], 15)
    remote_url = remote_proc.stdout.strip() if remote_proc.returncode == 0 else ""
    remote_slug = _remote_slug(remote_url) if remote_url else None
    remote_ok = remote_slug is not None and remote_slug.lower() == expected_repo_slug.lower()

    gh_auth = check_gh_auth(repo_root, gh_runner=gh_runner)

    blocked_reason = None
    if not remote_ok:
        blocked_reason = "REMOTE_MISMATCH"
    elif not gh_auth.authenticated:
        blocked_reason = "BLOCKED_GITHUB_AUTH"
    elif "repo" not in gh_auth.scopes and gh_auth.scopes:
        # Some gh auth outputs omit scopes entirely (older gh versions); only
        # flag missing 'repo' scope when scopes were actually parsed.
        blocked_reason = "BLOCKED_GITHUB_AUTH"

    return GitPreflightResult(
        is_git_repo=is_git_repo,
        remote_ok=remote_ok,
        remote_url=remote_url,
        expected_repo_slug=expected_repo_slug,
        push_capable=remote_ok and gh_auth.authenticated,
        gh_auth=gh_auth,
        blocked_reason=blocked_reason,
    )


@dataclass
class RequiredCheckPolicy:
    """
    The repository's live required-status-check policy for a branch (#59.1
    Blocker 3). `available` records whether the policy was actually observed
    -- never whether HowlPlane could guess at it. An unattended merge requires
    `authorizes_merge_gate()`; anything else parks.
    """

    available: bool
    contexts: List[str] = field(default_factory=list)
    source: str = POLICY_SOURCE_UNAVAILABLE
    reason: str = ""
    detail: str = ""

    def authorizes_merge_gate(self) -> bool:
        """
        True only when live policy was read AND it enforces at least one
        required check. A readable policy enforcing zero checks is a known
        policy with no CI evidence gate -- delegated merge authority is
        premised on green required checks, so that fails closed too.
        """
        return self.available and bool(self.contexts)


@dataclass
class CIObservation:
    all_required_observed: bool
    all_required_green: bool
    # A required check must be OBSERVED *and* TERMINAL before polling may
    # finish normally -- a queued job satisfies "observed" but proves nothing
    # (#59.1 Blocker 4).
    all_required_terminal: bool = False
    checks: List[Dict[str, str]] = field(default_factory=list)
    # Terminal and definitively not green. Pending work belongs in
    # pending_jobs, never here.
    failed_jobs: List[Dict[str, str]] = field(default_factory=list)
    pending_jobs: List[Dict[str, str]] = field(default_factory=list)
    policy: Optional[RequiredCheckPolicy] = None
    timed_out: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    def authorizes_merge(self) -> bool:
        """Every gate an unattended merge depends on, in one place."""
        return (
            self.policy is not None
            and self.policy.authorizes_merge_gate()
            and self.all_required_observed
            and self.all_required_terminal
            and self.all_required_green
            and not self.failed_jobs
            and not self.pending_jobs
            and not self.timed_out
        )


# `gh pr checks --json` reports a normalized `bucket` alongside the raw
# `state`; `gh pr checks --help` documents the bucket vocabulary as
# pass | fail | pending | skipping | cancel. Bucket is authoritative when
# present; the state map covers older `gh` builds that omit it.
_BUCKET_CLASS = {
    "pass": "green", "skipping": "green",
    "fail": "failed", "cancel": "failed",
    "pending": "pending", "waiting": "pending",
}
_STATE_CLASS = {
    "SUCCESS": "green", "NEUTRAL": "green", "SKIPPED": "green",
    "FAILURE": "failed", "CANCELLED": "failed", "TIMED_OUT": "failed",
    "ACTION_REQUIRED": "failed", "STARTUP_FAILURE": "failed", "STALE": "failed",
    "QUEUED": "pending", "IN_PROGRESS": "pending", "WAITING": "pending",
    "REQUESTED": "pending", "EXPECTED": "pending", "PENDING": "pending",
}


def classify_check(check: Dict[str, str]) -> str:
    """
    Maps one `gh pr checks` entry onto "green", "failed" or "pending".

    Anything unrecognized falls through to "pending" rather than "green": an
    unknown state must never be read as permission to merge.
    """
    bucket = str(check.get("bucket") or "").strip().lower()
    state = str(check.get("state") or "").strip().upper()
    return _BUCKET_CLASS.get(bucket) or _STATE_CLASS.get(state) or "pending"


class GitHubCIObserver:
    """
    Observes real GitHub CI state for a PR, never assuming local test exit 0
    means GitHub CI is green (#59 Phase 5).

    Required-check discovery reads live repository policy on every call, so a
    ruleset that gains or loses a required check is picked up with no code
    change (#59.1 Blocker 3). GitHub rulesets are consulted first -- they are
    what protects `main` on this repository, and the legacy branch-protection
    endpoint 404s under a ruleset -- with legacy branch protection as the
    fallback for repositories still using it. When neither can be read the
    policy is reported unavailable and the caller must fail closed; a
    hard-coded list must never authorize an unattended merge.

    A deterministic test or fake mode may inject a known policy via
    `policy_override`; production unattended mode leaves it unset and requires
    observed policy.
    """

    def __init__(
        self,
        gh_runner: CommandRunner = run_gh,
        git_runner: CommandRunner = run_git,
        policy_override: Optional[RequiredCheckPolicy] = None,
    ):
        self._gh = gh_runner
        self._git = git_runner
        self._policy_override = policy_override

    def required_check_policy(
        self,
        repo_root: Union[str, Path],
        repo_slug: str,
        branch: str = "main",
    ) -> RequiredCheckPolicy:
        if self._policy_override is not None:
            return self._policy_override

        contexts = self._contexts_from_rulesets(repo_root, repo_slug, branch)
        source = POLICY_SOURCE_RULESET
        if contexts is None:
            contexts = self._contexts_from_branch_protection(repo_root, repo_slug, branch)
            source = POLICY_SOURCE_BRANCH_PROTECTION
        if contexts is None:
            return RequiredCheckPolicy(
                available=False,
                source=POLICY_SOURCE_UNAVAILABLE,
                reason=CI_POLICY_UNAVAILABLE,
                detail=(
                    f"neither rulesets nor branch protection could be read for "
                    f"{repo_slug}@{branch}"
                ),
            )
        if not contexts:
            return RequiredCheckPolicy(
                available=True,
                contexts=[],
                source=source,
                reason=NO_REQUIRED_CHECKS_ENFORCED,
                detail=f"{source} readable but enforces no required status checks",
            )
        return RequiredCheckPolicy(available=True, contexts=contexts, source=source)

    def _gh_json(self, repo_root: Union[str, Path], endpoint: str) -> Optional[Any]:
        """
        Reads one `gh api` endpoint as JSON. Returns None when the endpoint
        could not be read or parsed at all -- which the caller must treat as
        "policy unknown", never as "policy empty".
        """
        proc = self._gh(repo_root, ["api", endpoint], 30)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None

    def _contexts_from_rulesets(
        self, repo_root: Union[str, Path], repo_slug: str, branch: str
    ) -> Optional[List[str]]:
        """
        Reads the rules GitHub itself reports as applying to `branch`, so
        repository- and organization-level rulesets are both covered without
        HowlPlane re-implementing ruleset applicability. Returns None when the
        endpoint could not be read at all (so the caller can try legacy
        protection), and [] when it was read but enforces no status checks.

        Response shape confirmed against this repository's live API:
          [{"type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "lint"}, ...]}}]
        """
        rules = self._gh_json(repo_root, f"repos/{repo_slug}/rules/branches/{branch}")
        if not isinstance(rules, list):
            return None

        contexts: List[str] = []
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
                continue
            params = rule.get("parameters") or {}
            for entry in params.get("required_status_checks") or []:
                context = entry.get("context") if isinstance(entry, dict) else entry
                if context and context not in contexts:
                    contexts.append(context)
        return contexts

    def _contexts_from_branch_protection(
        self, repo_root: Union[str, Path], repo_slug: str, branch: str
    ) -> Optional[List[str]]:
        """Legacy branch protection, for repositories not using rulesets."""
        data = self._gh_json(repo_root, f"repos/{repo_slug}/branches/{branch}/protection")
        if not isinstance(data, dict):
            return None
        required = data.get("required_status_checks") or {}
        if not isinstance(required, dict):
            return []
        return [c for c in (required.get("contexts") or []) if c]

    def required_check_names(
        self, repo_root: Union[str, Path], repo_slug: str, branch: str = "main"
    ) -> List[str]:
        """Convenience wrapper; callers gating a merge must use the policy itself."""
        return list(self.required_check_policy(repo_root, repo_slug, branch).contexts)

    def observe_once(
        self,
        repo_root: Union[str, Path],
        pr_number: int,
        repo_slug: str,
        policy: Optional[RequiredCheckPolicy] = None,
    ) -> CIObservation:
        policy = policy or self.required_check_policy(repo_root, repo_slug)
        required = set(policy.contexts)
        proc = self._gh(
            repo_root,
            ["pr", "checks", str(pr_number), "--json", "name,state,bucket,link"],
            30,
        )
        checks: List[Dict[str, str]] = []
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                checks = json.loads(proc.stdout)
            except json.JSONDecodeError:
                checks = []

        observed_names = {c.get("name") for c in checks}
        all_required_observed = bool(required) and required.issubset(observed_names)

        required_checks = [c for c in checks if c.get("name") in required]
        failed_jobs = [c for c in required_checks if classify_check(c) == "failed"]
        pending_jobs = [c for c in required_checks if classify_check(c) == "pending"]

        all_required_terminal = all_required_observed and not pending_jobs
        all_required_green = (
            all_required_observed
            and all_required_terminal
            and not failed_jobs
            and all(classify_check(c) == "green" for c in required_checks)
        )

        return CIObservation(
            all_required_observed=all_required_observed,
            all_required_green=all_required_green,
            all_required_terminal=all_required_terminal,
            checks=checks,
            failed_jobs=failed_jobs,
            pending_jobs=pending_jobs,
            policy=policy,
            raw={
                "required": sorted(required),
                "pr_number": pr_number,
                "policy_source": policy.source,
                "policy_reason": policy.reason,
            },
        )

    def poll_until_terminal(
        self,
        repo_root: Union[str, Path],
        pr_number: int,
        repo_slug: str,
        timeout_seconds: int = 900,
        poll_interval: int = 30,
        sleep_fn: Callable[[float], None] = time.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> CIObservation:
        """
        Bounded, terminal-aware CI polling (#59 Phase 5, #59.1 Blocker 4).

        Previously this returned as soon as every required check *name* had
        appeared -- but a QUEUED job already has a name, so polling could
        finish before CI had actually run. A required check must now be both
        observed and terminal.

        Returns early on a terminal failure (no point waiting out the rest),
        returns once every required check is terminal, and otherwise keeps
        polling until the deadline. On timeout it returns the last observation
        with timed_out set, which never authorizes a merge.
        """
        policy = self.required_check_policy(repo_root, repo_slug)
        observation = self.observe_once(repo_root, pr_number, repo_slug, policy=policy)
        if not policy.authorizes_merge_gate():
            # Nothing to wait for: the policy itself is unknown or enforces no
            # required checks. Either way the merge gate is already closed.
            return observation

        deadline = clock_fn() + timeout_seconds
        while clock_fn() < deadline:
            if observation.failed_jobs or observation.all_required_terminal:
                return observation
            sleep_fn(poll_interval)
            observation = self.observe_once(repo_root, pr_number, repo_slug, policy=policy)

        observation.timed_out = not observation.all_required_terminal
        return observation


class GitIntegrationExecutor(AuthorityExecutor):
    """
    Bounded-authority executor for real git/GitHub task integration
    (#59 Phase 2). Unlike HowlChangeOpsExecutor (which wraps an external,
    out-of-repo binary scoped to release-candidate actions), this executor
    performs `git`/`gh` subprocess calls natively and derives its `evaluate`
    verdict solely from the campaign's AuthorityEnvelope via
    evaluate_action_against_envelope() -- the single deterministic choke
    point for delegated authority.

    Instantiated per-campaign (never a module-level singleton): the envelope
    is campaign-scoped and must not leak across campaigns or tests.
    """

    SUPPORTED_ACTIONS = {
        "create_task_branch",
        "commit_task_changes",
        "push_task_branch",
        "create_pull_request",
        "merge_pull_request",
        "sync_local_main",
    }

    def __init__(
        self,
        repo_root: Union[str, Path],
        repo_slug: str,
        envelope: Optional[AuthorityEnvelope],
        git_runner: CommandRunner = run_git,
        gh_runner: CommandRunner = run_gh,
        ci_observer: Optional[GitHubCIObserver] = None,
        merges_so_far: int = 0,
    ):
        self.repo_root = Path(repo_root)
        self.repo_slug = repo_slug
        self.envelope = envelope
        self._git = git_runner
        self._gh = gh_runner
        self.ci_observer = ci_observer or GitHubCIObserver(gh_runner=gh_runner, git_runner=git_runner)
        # Seeded from durable campaign_state.merges_this_campaign at
        # construction time so the merge budget is enforced correctly across
        # process restarts/resumes, not just within a single process's
        # in-memory lifetime.
        self._merges_so_far = merges_so_far

    @property
    def name(self) -> str:
        return "git_integration"

    def is_available(self) -> bool:
        preflight = run_preflight(self.repo_root, self.repo_slug, git_runner=self._git, gh_runner=self._gh)
        return preflight.push_capable

    def supports_action(self, action_type: str) -> bool:
        return action_type in self.SUPPORTED_ACTIONS

    def evaluate(
        self,
        action: ProposedAction,
        repo_path: Union[str, Path],
        task_run_dir: Union[str, Path],
    ) -> Tuple[str, Optional[str], Optional[str]]:
        decision, reason = evaluate_action_against_envelope(
            self.envelope,
            action.action_type,
            self.repo_slug,
            merges_so_far=self._merges_so_far,
        )
        if decision == AuthorityDecision.DELEGATED_AUTHORITY_ALLOW:
            decision_id = f"git-integration:{action.action_type}:{int(time.time() * 1000)}"
            return "ALLOW", decision_id, reason
        if decision in (AuthorityDecision.DENIED_BY_ENVELOPE,):
            return "DENY", None, reason
        # ENVELOPE_ABSENT / OUTSIDE_ENVELOPE_SCOPE / ENVELOPE_EXPIRED / ENVELOPE_TAMPERED
        return "REQUIRE_APPROVAL", None, reason

    def approve(
        self,
        decision_id: str,
        repo_path: Union[str, Path],
        task_run_dir: Union[str, Path],
    ) -> Tuple[bool, Optional[str]]:
        # The envelope IS the pre-authorization; there is no separate
        # cryptographic approval step to invoke for a decision that already
        # evaluated to ALLOW under a verified, unexpired envelope.
        if decision_id and decision_id.startswith("git-integration:"):
            return True, "Authorized by verified campaign authority envelope."
        return False, "No valid ALLOW decision to approve."

    def execute(
        self,
        decision_id: str,
        repo_path: Union[str, Path],
        task_run_dir: Union[str, Path],
        action: ProposedAction,
        task_id: str,
    ) -> ExecutionResult:
        start = time.monotonic()
        try:
            metadata = self._dispatch(action, task_id)
            duration = time.monotonic() - start
            receipt = ExecutionReceipt(
                task_id=task_id,
                executor=self.name,
                executor_version=GIT_INTEGRATION_EXECUTOR_VERSION,
                decision_id=decision_id or "",
                action_type=action.action_type,
                repository=self.repo_slug,
                commit_sha=metadata.get("commit_sha", ""),
                status="success",
                executed_at=datetime.now(timezone.utc).isoformat(),
                verification_status="PASS",
                native_receipt=metadata,
            )
            return ExecutionResult(
                executor_id=self.name,
                status="success",
                action_type=action.action_type,
                decision_id=decision_id,
                receipt=receipt,
                verification_status="PASS",
                duration_seconds=duration,
                metadata=metadata,
            )
        except GitIntegrationError as exc:
            duration = time.monotonic() - start
            return ExecutionResult(
                executor_id=self.name,
                status="failure",
                action_type=action.action_type,
                decision_id=decision_id,
                verification_status="FAIL",
                duration_seconds=duration,
                error_message=str(exc),
            )

    def _dispatch(self, action: ProposedAction, task_id: str) -> Dict[str, Any]:
        args = action.arguments or {}
        if action.action_type == "create_task_branch":
            branch = self.create_task_branch(task_id, args.get("base_branch", "main"))
            return {"branch": branch}
        if action.action_type == "commit_task_changes":
            sha = self.stage_and_commit(
                args["paths"], args["message"], args.get("baseline_sha"), args.get("allowed_paths"),
            )
            return {"commit_sha": sha}
        if action.action_type == "push_task_branch":
            self.push_branch(args["branch"])
            return {"branch": args["branch"], "pushed": True}
        if action.action_type == "create_pull_request":
            pr_number, pr_url = self.open_pull_request(
                args["branch"], args["title"], args["body"], args.get("base", "main")
            )
            return {"pr_number": pr_number, "pr_url": pr_url}
        if action.action_type == "merge_pull_request":
            merge_sha = self.merge_pull_request(args["pr_number"])
            self._merges_so_far += 1
            return {"merge_sha": merge_sha}
        if action.action_type == "sync_local_main":
            synced = self.sync_local_main()
            return {"local_main_synced": synced}
        raise GitIntegrationError(f"Unsupported action_type: {action.action_type}")

    def verify_receipt(
        self,
        receipt: ExecutionReceipt,
        expected_action: str,
        expected_repo: str,
        expected_commit: Optional[str] = None,
        run_dir: Optional[Union[str, Path]] = None,
    ) -> Tuple[bool, Optional[str]]:
        if receipt.schema != EXECUTION_RECEIPT_SCHEMA_VERSION:
            return False, f"Unexpected receipt schema: {receipt.schema}"
        if receipt.action_type != expected_action:
            return False, f"Receipt action_type '{receipt.action_type}' != expected '{expected_action}'"
        if receipt.repository != expected_repo:
            return False, f"Receipt repository '{receipt.repository}' != expected '{expected_repo}'"
        if expected_commit and receipt.commit_sha != expected_commit:
            return False, f"Receipt commit_sha '{receipt.commit_sha}' != expected '{expected_commit}'"
        if receipt.status != "success":
            return False, f"Receipt status is '{receipt.status}', not 'success'"
        return True, None

    def query_execution_status(
        self,
        decision_id: str,
        repo_path: Union[str, Path],
        task_run_dir: Union[str, Path],
        action: ProposedAction,
        task_id: str,
    ) -> Tuple[str, Optional[ExecutionReceipt], Optional[str]]:
        """
        Crash-recovery reconciliation (#59 Phase 22): discovers whether the
        real-world side effect already happened by checking observable
        GitHub/git truth, never trusting a possibly-stale local record.
        """
        branch = f"fix/{task_id}"
        if action.action_type in ("create_task_branch", "commit_task_changes", "push_task_branch"):
            ls_remote = self._git(self.repo_root, ["ls-remote", "origin", branch], 30)
            if ls_remote.returncode == 0 and ls_remote.stdout.strip():
                return "already_executed", None, f"Remote branch '{branch}' already exists."
            return "not_executed", None, None
        if action.action_type == "create_pull_request":
            proc = self._gh(self.repo_root, ["pr", "list", "--head", branch, "--json", "number,url,state"], 30)
            if proc.returncode == 0 and proc.stdout.strip():
                try:
                    prs = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    prs = []
                if prs:
                    return "already_executed", None, f"PR already exists for branch '{branch}': {prs[0]}"
            return "not_executed", None, None
        if action.action_type == "merge_pull_request":
            pr_number = (action.arguments or {}).get("pr_number")
            if pr_number:
                proc = self._gh(self.repo_root, ["pr", "view", str(pr_number), "--json", PR_MERGE_FIELDS], 30)
                if proc.returncode == 0 and proc.stdout.strip():
                    try:
                        data = json.loads(proc.stdout)
                    except json.JSONDecodeError:
                        data = {}
                    if pr_is_merged(data):
                        return "already_executed", None, f"PR #{pr_number} is already merged."
            return "not_executed", None, None
        return "not_executed", None, None

    # ---- real git/GitHub step implementations ----

    def create_task_branch(self, task_id: str, base_branch: str = "main") -> str:
        branch = f"fix/{task_id}"
        fetch = self._git(self.repo_root, ["fetch", "origin", base_branch], 60)
        if fetch.returncode != 0:
            raise GitIntegrationError(f"git fetch origin {base_branch} failed: {fetch.stderr}")
        create = self._git(self.repo_root, ["switch", "-c", branch, f"origin/{base_branch}"], 30)
        if create.returncode != 0:
            raise GitIntegrationError(f"git switch -c {branch} failed: {create.stderr}")
        verify = self._git(self.repo_root, ["rev-parse", "--verify", branch], 15)
        if verify.returncode != 0:
            raise GitIntegrationError(f"branch '{branch}' does not exist after creation attempt")
        return branch

    def stage_and_commit(
        self,
        paths: List[str],
        message: str,
        baseline_sha: Optional[str] = None,
        allowed_paths: Optional[List[str]] = None,
    ) -> str:
        if not paths:
            raise GitIntegrationError("commit_task_changes called with an empty path list -- nothing to stage")
        if allowed_paths:
            violations = [p for p in paths if p not in allowed_paths]
            if violations:
                raise GitIntegrationError(
                    f"commit_task_changes rejected: path(s) outside task-declared scope {allowed_paths}: {violations}"
                )
        self._reject_premature_acceptance_merge_claims(paths)
        add_proc = self._git(self.repo_root, ["add", "--"] + list(paths), 30)
        if add_proc.returncode != 0:
            raise GitIntegrationError(f"git add of task-owned paths failed: {add_proc.stderr}")
        commit_proc = self._git(self.repo_root, ["commit", "-m", message], 30)
        if commit_proc.returncode != 0:
            raise GitIntegrationError(f"git commit failed: {commit_proc.stderr}")
        sha_proc = self._git(self.repo_root, ["rev-parse", "HEAD"], 15)
        if sha_proc.returncode != 0 or not sha_proc.stdout.strip():
            raise GitIntegrationError("could not resolve HEAD after commit")
        sha = sha_proc.stdout.strip()
        if baseline_sha and sha == baseline_sha:
            raise GitIntegrationError("HEAD did not move after commit -- commit did not actually happen")
        return sha

    def _reject_premature_acceptance_merge_claims(self, paths: List[str]) -> None:
        """Reject unverified merge success claims before any git subprocess runs."""
        for path in paths:
            if not LIVE_ACCEPTANCE_JOURNAL_PATTERN.fullmatch(path):
                continue
            journal_path = self.repo_root / path
            try:
                content = journal_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise GitIntegrationError(
                    f"commit_task_changes rejected: could not read live acceptance journal '{path}': {exc}"
                ) from exc
            if PREMATURE_MERGE_SUCCESS_PATTERN.search(content):
                raise GitIntegrationError(
                    "commit_task_changes rejected: live acceptance journal contains a premature merge success claim"
                )

    def push_branch(self, branch: str) -> bool:
        # Pre-push hooks may run the full deterministic verification suite,
        # which takes well over the previous 90-second budget. Allow 300s.
        push_proc = self._git(self.repo_root, ["push", "-u", "origin", branch], 300)
        if push_proc.returncode != 0:
            raise GitIntegrationError(f"git push -u origin {branch} failed: {push_proc.stderr}")
        local_sha = self._git(self.repo_root, ["rev-parse", branch], 15).stdout.strip()
        ls_remote = self._git(self.repo_root, ["ls-remote", "origin", branch], 30)
        remote_sha = ls_remote.stdout.split()[0] if ls_remote.returncode == 0 and ls_remote.stdout.strip() else None
        if not remote_sha or remote_sha != local_sha:
            raise GitIntegrationError(
                f"push reported success but remote branch '{branch}' SHA "
                f"('{remote_sha}') does not match local SHA ('{local_sha}')"
            )
        return True

    def open_pull_request(self, branch: str, title: str, body: str, base: str = "main") -> Tuple[int, str]:
        create_proc = self._gh(
            self.repo_root,
            ["pr", "create", "--repo", self.repo_slug, "--base", base, "--head", branch,
             "--title", title, "--body", body],
            60,
        )
        if create_proc.returncode != 0:
            raise GitIntegrationError(f"gh pr create failed: {create_proc.stderr}")

        list_proc = self._gh(
            self.repo_root,
            ["pr", "list", "--repo", self.repo_slug, "--head", branch, "--json", "number,url"],
            30,
        )
        if list_proc.returncode != 0 or not list_proc.stdout.strip():
            raise GitIntegrationError(f"could not confirm PR exists for branch '{branch}' after creation")
        try:
            prs = json.loads(list_proc.stdout)
        except json.JSONDecodeError as exc:
            raise GitIntegrationError(f"unparseable PR list response: {exc}") from exc
        if not prs:
            raise GitIntegrationError(f"gh pr create reported success but no PR found for branch '{branch}'")
        pr_number, pr_url = prs[0]["number"], prs[0]["url"]

        view_proc = self._gh(self.repo_root, ["pr", "view", str(pr_number), "--json", "number"], 30)
        if view_proc.returncode != 0:
            raise GitIntegrationError(f"gh pr view #{pr_number} failed to confirm PR existence: {view_proc.stderr}")
        return pr_number, pr_url

    def observe_required_checks(self, pr_number: int) -> CIObservation:
        return self.ci_observer.poll_until_terminal(self.repo_root, pr_number, self.repo_slug)

    def merge_pull_request(self, pr_number: int) -> str:
        if not TASK_BRANCH_PATTERN.match(self._pr_head_branch(pr_number) or ""):
            raise GitIntegrationError(f"PR #{pr_number} head branch is not a recognized campaign task branch")

        merge_proc = self._gh(
            self.repo_root,
            ["pr", "merge", str(pr_number), "--repo", self.repo_slug, "--squash", "--delete-branch"],
            120,
        )
        if merge_proc.returncode != 0:
            raise GitIntegrationError(f"gh pr merge --squash #{pr_number} failed: {merge_proc.stderr}")

        view_proc = self._gh(
            self.repo_root,
            ["pr", "view", str(pr_number), "--repo", self.repo_slug, "--json", PR_MERGE_FIELDS],
            30,
        )
        if view_proc.returncode != 0 or not view_proc.stdout.strip():
            raise GitIntegrationError(
                f"could not independently verify merge of PR #{pr_number}: {view_proc.stderr.strip()}"
            )
        try:
            data = json.loads(view_proc.stdout)
        except json.JSONDecodeError as exc:
            raise GitIntegrationError(f"unparseable gh pr view response: {exc}") from exc
        if not pr_is_merged(data):
            raise GitIntegrationError(f"gh pr merge exited 0 but PR #{pr_number} is not reported merged")
        merge_sha = (data.get("mergeCommit") or {}).get("oid")
        if not merge_sha:
            raise GitIntegrationError(f"PR #{pr_number} reported merged but no merge commit SHA was returned")
        return merge_sha

    def _pr_head_branch(self, pr_number: int) -> Optional[str]:
        proc = self._gh(self.repo_root, ["pr", "view", str(pr_number), "--json", "headRefName"], 30)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            return json.loads(proc.stdout).get("headRefName")
        except json.JSONDecodeError:
            return None

    def verify_remote_main_contains(self, merge_sha: str, base_branch: str = "main") -> bool:
        fetch = self._git(self.repo_root, ["fetch", "origin", base_branch], 60)
        if fetch.returncode != 0:
            raise GitIntegrationError(f"git fetch origin {base_branch} failed while verifying merge: {fetch.stderr}")
        ancestor = self._git(
            self.repo_root, ["merge-base", "--is-ancestor", merge_sha, f"origin/{base_branch}"], 30
        )
        return ancestor.returncode == 0

    def sync_local_main(self, base_branch: str = "main") -> bool:
        fetch = self._git(self.repo_root, ["fetch", "origin", base_branch], 60)
        if fetch.returncode != 0:
            raise GitIntegrationError(f"git fetch origin {base_branch} failed during local sync: {fetch.stderr}")
        switch = self._git(self.repo_root, ["switch", base_branch], 30)
        if switch.returncode != 0:
            raise GitIntegrationError(f"git switch {base_branch} failed during local sync: {switch.stderr}")
        merge = self._git(self.repo_root, ["merge", "--ff-only", f"origin/{base_branch}"], 30)
        if merge.returncode != 0:
            raise GitIntegrationError(f"git merge --ff-only origin/{base_branch} failed: {merge.stderr}")
        local_sha = self._git(self.repo_root, ["rev-parse", "HEAD"], 15).stdout.strip()
        remote_sha = self._git(self.repo_root, ["rev-parse", f"origin/{base_branch}"], 15).stdout.strip()
        status = self._git(self.repo_root, ["status", "--porcelain"], 15).stdout
        return bool(local_sha) and local_sha == remote_sha and not status.strip()

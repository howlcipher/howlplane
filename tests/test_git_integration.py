#!/usr/bin/env python3
"""
tests/test_git_integration.py

Phase 20 layered lifecycle tests for real git/GitHub integration (#59).
Fakes are injected ONLY at the subprocess boundary (a ScriptedRunner
standing in for `git`/`gh`) -- the production GitIntegrationExecutor logic
under test is the real code that will run against a genuine `git`/`gh` in
production. Each negative test proves a specific failure mode fails closed:
no fabricated success record is ever produced when a real-world step did not
actually happen.
"""

import pytest

from src.control_plane.authority_envelope import create_envelope
from src.control_plane.authority_profile import get_profile
from src.control_plane.git_integration import (
    GitIntegrationError,
    GitIntegrationExecutor,
    GitHubCIObserver,
    PR_MERGE_FIELDS,
    classify_check,
    pr_is_merged,
)
from src.control_plane.proposed_action import ProposedAction
from src.control_plane.synthesis.campaign_state import GitIntegrationRecord
from tests._dogfood_test_helpers import ScriptedRunner, build_full_merge_flow


REPO_SLUG = "howlcipher/howlplane"


def make_envelope(profile_id="overnight-safe", campaign_id="DOGFOOD-TEST-GIT"):
    profile = get_profile(profile_id)
    return create_envelope(profile, campaign_id, operator_origin="cli:test@host")


_UNSET = object()


def make_executor(git_runner=None, gh_runner=None, envelope=_UNSET):
    return GitIntegrationExecutor(
        repo_root="/fake/repo",
        repo_slug=REPO_SLUG,
        envelope=make_envelope() if envelope is _UNSET else envelope,
        git_runner=git_runner or ScriptedRunner(),
        gh_runner=gh_runner or ScriptedRunner(),
    )


def _ruleset_json(contexts):
    """
    The live shape of `gh api repos/{slug}/rules/branches/main` on this
    repository, including the neighbouring rules the parser must skip.
    """
    checks = ", ".join(f'{{"context": "{c}"}}' for c in contexts)
    return (
        '[{"type": "deletion"}, {"type": "non_fast_forward"}, '
        '{"type": "pull_request", "parameters": {"required_approving_review_count": 0}}, '
        '{"type": "required_status_checks", "parameters": '
        f'{{"strict_required_status_checks_policy": true, "required_status_checks": [{checks}]}}}}]'
    )


def _gh_with_ruleset(contexts, base_branch="main"):
    """A repo protected by a ruleset: legacy branch protection 404s, as live."""
    gh = ScriptedRunner()
    gh.on(
        ["api", f"repos/{REPO_SLUG}/rules/branches/{base_branch}"],
        returncode=0, stdout=_ruleset_json(contexts),
    )
    gh.on(
        ["api", f"repos/{REPO_SLUG}/branches/{base_branch}/protection"],
        returncode=1, stderr="gh: Branch not protected (HTTP 404)",
    )
    return gh


def _checks_json(*pairs):
    """(name, bucket, state) triples as `gh pr checks --json` would report them."""
    entries = ", ".join(
        f'{{"name": "{name}", "bucket": "{bucket}", "state": "{state}"}}'
        for name, bucket, state in pairs
    )
    return f"[{entries}]"


# ---------------------------------------------------------------------------
# Negative tests: each proves a specific failure fails closed.
# ---------------------------------------------------------------------------


def test_missing_commit_fails_closed():
    """If HEAD does not move after `git commit`, stage_and_commit must raise
    rather than report a fabricated commit SHA."""
    git = ScriptedRunner()
    git.on(["add", "--", "foo.py"], returncode=0)
    git.on(["commit", "-m", "fix: nothing"], returncode=0)
    # HEAD did not move: same SHA reported before and after (baseline == post-commit)
    git.on(["rev-parse", "HEAD"], returncode=0, stdout="deadbeef1234\n")

    executor = make_executor(git_runner=git)
    with pytest.raises(GitIntegrationError, match="did not actually happen"):
        executor.stage_and_commit(["foo.py"], "fix: nothing", baseline_sha="deadbeef1234")


def test_empty_path_list_fails_closed():
    """Never allowed to stage nothing and call it a commit."""
    executor = make_executor()
    with pytest.raises(GitIntegrationError, match="empty path list"):
        executor.stage_and_commit([], "fix: nothing")


def test_commit_outside_allowed_paths_fails_closed():
    """#59.2 Phase 8: a task scoped to specific paths must never commit
    anything outside that allowlist -- fails before any git add/commit runs."""
    git = ScriptedRunner()
    executor = make_executor(git_runner=git)
    with pytest.raises(GitIntegrationError, match="outside task-declared scope"):
        executor.stage_and_commit(
            ["documentation/task_journals/2026-08-22_acceptance.md", "src/unexpected.py"],
            "fix: scoped change",
            allowed_paths=["documentation/task_journals/2026-08-22_acceptance.md"],
        )
    # No git subprocess was ever invoked -- the check runs before staging.
    assert git.calls == []


def test_commit_within_allowed_paths_succeeds():
    """A commit touching only the declared allowlist proceeds normally."""
    git = ScriptedRunner()
    journal_path = "documentation/task_journals/2026-08-22_acceptance.md"
    git.on(["add", "--", journal_path], returncode=0)
    git.on(["commit", "-m", "fix: scoped change"], returncode=0)
    git.on(["rev-parse", "HEAD"], returncode=0, stdout="cafef00d\n")

    executor = make_executor(git_runner=git)
    sha = executor.stage_and_commit(
        [journal_path], "fix: scoped change", allowed_paths=[journal_path],
    )
    assert sha == "cafef00d"


def test_push_failure_blocks_downstream():
    """A failed `git push` must raise, never silently proceed to PR/merge."""
    git = ScriptedRunner()
    git.on(["push", "-u", "origin", "fix/T1"], returncode=1, stderr="remote rejected")
    executor = make_executor(git_runner=git)
    with pytest.raises(GitIntegrationError, match="git push"):
        executor.push_branch("fix/T1")


def test_push_reports_success_but_remote_sha_mismatch_fails_closed():
    """Push exits 0 but the remote SHA doesn't match local -- must fail closed,
    not just trust the exit code (#59 Phase 4)."""
    git = ScriptedRunner()
    git.on(["push", "-u", "origin", "fix/T1"], returncode=0)
    git.on(["rev-parse", "fix/T1"], returncode=0, stdout="aaaa1111\n")
    git.on(["ls-remote", "origin", "fix/T1"], returncode=0, stdout="bbbb2222\trefs/heads/fix/T1\n")
    executor = make_executor(git_runner=git)
    with pytest.raises(GitIntegrationError, match="does not match local SHA"):
        executor.push_branch("fix/T1")


def test_pr_creation_failure_blocks_merge():
    """`gh pr create` failing must raise, never proceed to a fabricated PR number."""
    gh = ScriptedRunner()
    gh.on(
        ["pr", "create", "--repo", REPO_SLUG, "--base", "main", "--head", "fix/T1", "--title", "t", "--body", "b"],
        returncode=1, stderr="validation failed",
    )
    executor = make_executor(gh_runner=gh)
    with pytest.raises(GitIntegrationError, match="gh pr create failed"):
        executor.open_pull_request("fix/T1", "t", "b")


def test_pr_create_reports_success_but_not_listed_fails_closed():
    """gh pr create exits 0 but a follow-up `gh pr list` finds nothing --
    must not trust the initial exit code alone."""
    gh = ScriptedRunner()
    gh.on(
        ["pr", "create", "--repo", REPO_SLUG, "--base", "main", "--head", "fix/T1", "--title", "t", "--body", "b"],
        returncode=0,
    )
    gh.on(["pr", "list", "--repo", REPO_SLUG, "--head", "fix/T1", "--json", "number,url"], returncode=0, stdout="[]")
    executor = make_executor(gh_runner=gh)
    with pytest.raises(GitIntegrationError, match="no PR found"):
        executor.open_pull_request("fix/T1", "t", "b")


def _observe(*checks, contexts=None):
    """One `observe_once` against a ruleset-protected repo with these checks."""
    gh = _gh_with_ruleset(list(contexts or [c[0] for c in checks]))
    gh.on(
        ["pr", "checks", "5", "--json", "name,state,bucket,link"],
        returncode=0, stdout=_checks_json(*checks),
    )
    observer = GitHubCIObserver(gh_runner=gh, git_runner=ScriptedRunner())
    return observer.observe_once("/fake/repo", 5, REPO_SLUG)


def test_ci_pending_blocks_merge():
    """A required check that is observed but still pending must not be green."""
    obs = _observe(("test-python", "pending", "PENDING"))

    assert obs.all_required_green is False
    assert obs.authorizes_merge() is False


def test_ci_failed_check_reported_not_green():
    obs = _observe(
        ("test-python", "pass", "SUCCESS"),
        ("test-go", "fail", "FAILURE"),
        ("lint", "pass", "SUCCESS"),
    )

    assert obs.all_required_green is False
    assert any(f["name"] == "test-go" for f in obs.failed_jobs)


def test_ci_all_green_reports_true():
    obs = _observe(
        ("test-python", "pass", "SUCCESS"),
        ("test-go", "pass", "SUCCESS"),
        ("lint", "pass", "SUCCESS"),
    )

    assert obs.all_required_green is True
    assert obs.all_required_observed is True
    assert obs.authorizes_merge() is True


def test_simulated_ci_evidence_rejected_in_overnight_mode():
    """A GitIntegrationRecord in simulated/legacy mode must never satisfy
    is_fully_integrated(), regardless of what ci_status string it carries --
    simulated_green (or any simulated value) can never authorize a merge."""
    rec = GitIntegrationRecord(
        task_id="T1", target_repo="howlplane", integration_mode="simulated",
        ci_status="passed", merged=True,  # even if some legacy code tried to force these
    )
    # merged/ci_status alone are not authoritative -- is_fully_integrated()
    # requires every *_observed flag, none of which a simulated record sets.
    assert rec.is_fully_integrated() is False


def test_merge_reported_success_but_remote_main_missing_sha_not_integrated():
    """gh pr merge exits 0 and reports merged=true, but the merge SHA is not
    yet reachable from origin/main -- must not be treated as integrated."""
    git = ScriptedRunner()
    git.on(["fetch", "origin", "main"], returncode=0)
    git.on(["merge-base", "--is-ancestor", "mergesha123", "origin/main"], returncode=1)
    executor = make_executor(git_runner=git)
    assert executor.verify_remote_main_contains("mergesha123") is False


def test_merge_pull_request_rejects_non_task_branch():
    gh = ScriptedRunner()
    gh.on(["pr", "view", "5", "--json", "headRefName"], returncode=0, stdout='{"headRefName": "main"}')
    executor = make_executor(gh_runner=gh)
    with pytest.raises(GitIntegrationError, match="not a recognized campaign task branch"):
        executor.merge_pull_request(5)


def test_merge_pull_request_gh_merge_failure_raises():
    gh = ScriptedRunner()
    gh.on(["pr", "view", "5", "--json", "headRefName"], returncode=0, stdout='{"headRefName": "fix/T1"}')
    gh.on(
        ["pr", "merge", "5", "--repo", REPO_SLUG, "--squash", "--delete-branch"],
        returncode=1, stderr="required checks have not passed",
    )
    executor = make_executor(gh_runner=gh)
    with pytest.raises(GitIntegrationError, match="gh pr merge --squash"):
        executor.merge_pull_request(5)


def _merge_executor(verify_rc=0, verify_stdout="", verify_stderr=""):
    """An executor whose merge succeeds and whose verification call is scripted."""
    gh = ScriptedRunner()
    gh.on(["pr", "view", "5", "--json", "headRefName"], returncode=0, stdout='{"headRefName": "fix/T1"}')
    gh.on(["pr", "merge", "5", "--repo", REPO_SLUG, "--squash", "--delete-branch"], returncode=0)
    gh.on(
        ["pr", "view", "5", "--repo", REPO_SLUG, "--json", PR_MERGE_FIELDS],
        returncode=verify_rc, stdout=verify_stdout, stderr=verify_stderr,
    )
    return make_executor(gh_runner=gh)


def test_merge_pull_request_success_path_returns_merge_sha():
    executor = _merge_executor(verify_stdout=(
        '{"state": "MERGED", "mergedAt": "2026-08-22T20:21:08Z", '
        '"mergeCommit": {"oid": "sha_merge_1"}}'
    ))
    assert executor.merge_pull_request(5) == "sha_merge_1"


# ---------------------------------------------------------------------------
# Positive lifecycle test: real call ordering through the whole sequence.
# ---------------------------------------------------------------------------


def test_real_lifecycle_calls_occur_in_order():
    git = ScriptedRunner()
    gh = ScriptedRunner()
    build_full_merge_flow(
        git, gh, task_id="T1", repo_slug=REPO_SLUG, pr_number=42,
        commit_message="fix: T1", pr_title="t", pr_body="b",
        modified_path="src/foo.py", commit_sha="commitsha1", merge_sha="mergesha1",
    )

    executor = make_executor(git_runner=git, gh_runner=gh)

    branch = executor.create_task_branch("T1")
    assert branch == "fix/T1"

    commit_sha = executor.stage_and_commit(["src/foo.py"], "fix: T1")
    assert commit_sha == "commitsha1"

    assert executor.push_branch(branch) is True

    pr_number, pr_url = executor.open_pull_request(branch, "t", "b")
    assert pr_number == 42

    merge_sha = executor.merge_pull_request(pr_number)
    assert merge_sha == "mergesha1"

    assert executor.verify_remote_main_contains(merge_sha) is True

    # Verify call ordering: branch -> add/commit -> push -> PR create/list/view -> merge -> verify
    branch_idx = git.calls.index(("switch", "-c", "fix/T1", "origin/main"))
    commit_idx = git.calls.index(("commit", "-m", "fix: T1"))
    push_idx = git.calls.index(("push", "-u", "origin", "fix/T1"))
    assert branch_idx < commit_idx < push_idx


def test_evaluate_allows_within_envelope_scope():
    executor = make_executor()
    action = ProposedAction(action_type="merge_pull_request", target_repo=REPO_SLUG)
    verdict, decision_id, reason = executor.evaluate(action, "/fake/repo", "/fake/run")
    assert verdict == "ALLOW"
    assert decision_id is not None


def test_evaluate_denies_force_push_regardless_of_envelope():
    executor = make_executor()
    action = ProposedAction(action_type="force_push", target_repo=REPO_SLUG)
    verdict, decision_id, reason = executor.evaluate(action, "/fake/repo", "/fake/run")
    assert verdict == "DENY"


def test_evaluate_requires_approval_with_no_envelope():
    executor = make_executor(envelope=None)
    action = ProposedAction(action_type="merge_pull_request", target_repo=REPO_SLUG)
    verdict, decision_id, reason = executor.evaluate(action, "/fake/repo", "/fake/run")
    assert verdict == "REQUIRE_APPROVAL"


# ---------------------------------------------------------------------------
# #59.1 Blocker 3: required-check discovery reads LIVE policy, never a
# hard-coded list. The five contexts below are the ones actually enforced on
# howlcipher/howlplane's `main` ruleset at the time of writing -- but the
# point of these tests is that nothing in production encodes them.
# ---------------------------------------------------------------------------

LIVE_CONTEXTS = ["test-python", "test-go", "lint", "SlopsLint Duplication & Ceiling Ratchet", "Analyze"]


def test_14_ruleset_required_checks_discovered_when_legacy_protection_404s():
    """
    `main` is protected by a GitHub Ruleset, so repos/{slug}/branches/main/
    protection returns 404. Discovery must read the ruleset instead of
    silently substituting a stale internal list.
    """
    observer = GitHubCIObserver(gh_runner=_gh_with_ruleset(LIVE_CONTEXTS), git_runner=ScriptedRunner())
    policy = observer.required_check_policy("/fake/repo", REPO_SLUG)

    assert policy.available is True
    assert policy.source == "ruleset"
    assert policy.contexts == LIVE_CONTEXTS
    assert policy.authorizes_merge_gate() is True


def test_15_legacy_branch_protection_still_discovered():
    """Repositories still using classic branch protection keep working."""
    gh = ScriptedRunner()
    gh.on(["api", f"repos/{REPO_SLUG}/rules/branches/main"], returncode=1, stderr="not found")
    gh.on(
        ["api", f"repos/{REPO_SLUG}/branches/main/protection"],
        returncode=0,
        stdout='{"required_status_checks": {"contexts": ["test-python", "test-go"]}}',
    )
    observer = GitHubCIObserver(gh_runner=gh, git_runner=ScriptedRunner())
    policy = observer.required_check_policy("/fake/repo", REPO_SLUG)

    assert policy.available is True
    assert policy.source == "branch_protection"
    assert policy.contexts == ["test-python", "test-go"]


def test_16_unreadable_policy_fails_closed_and_never_authorizes_merge():
    """
    If neither policy source can be read, an unattended merge must not happen.
    Previously this silently fell back to ["test-python","test-go","lint"].
    """
    gh = ScriptedRunner()
    gh.on(["api", f"repos/{REPO_SLUG}/rules/branches/main"], returncode=1, stderr="API rate limited")
    gh.on(["api", f"repos/{REPO_SLUG}/branches/main/protection"], returncode=1, stderr="404")
    gh.on(
        ["pr", "checks", "5", "--json", "name,state,bucket,link"],
        returncode=0, stdout=_checks_json(("test-python", "pass", "SUCCESS")),
    )
    observer = GitHubCIObserver(gh_runner=gh, git_runner=ScriptedRunner())
    policy = observer.required_check_policy("/fake/repo", REPO_SLUG)

    assert policy.available is False
    assert policy.reason == "CI_POLICY_UNAVAILABLE"
    assert policy.authorizes_merge_gate() is False
    # Even with every observed check green, an unknown policy blocks the merge.
    assert observer.observe_once("/fake/repo", 5, REPO_SLUG).authorizes_merge() is False


def test_readable_policy_enforcing_zero_checks_fails_closed():
    """
    A ruleset that protects the branch but enforces no status checks is a
    *known* policy with no CI evidence gate. Delegated merge authority is
    premised on green required checks, so this fails closed too -- and is
    reported distinctly from an unreadable policy.
    """
    observer = GitHubCIObserver(gh_runner=_gh_with_ruleset([]), git_runner=ScriptedRunner())
    policy = observer.required_check_policy("/fake/repo", REPO_SLUG)

    assert policy.available is True
    assert policy.contexts == []
    assert policy.reason == "NO_REQUIRED_CHECKS_ENFORCED"
    assert policy.authorizes_merge_gate() is False


def test_22_new_ruleset_context_is_observed_without_code_changes():
    """
    Discovery re-reads live policy every call, so a check added to the ruleset
    is picked up with no HowlPlane change. Same observer, same code, extra
    required context.
    """
    observer = GitHubCIObserver(
        gh_runner=_gh_with_ruleset(LIVE_CONTEXTS + ["brand-new-required-check"]),
        git_runner=ScriptedRunner(),
    )
    policy = observer.required_check_policy("/fake/repo", REPO_SLUG)

    assert "brand-new-required-check" in policy.contexts
    assert len(policy.contexts) == len(LIVE_CONTEXTS) + 1


# ---------------------------------------------------------------------------
# #59.1 Blocker 4: polling must wait for TERMINAL required checks, not merely
# for their names to appear.
# ---------------------------------------------------------------------------

def _poll(check_sequence, timeout_seconds=100, contexts=("test-python", "test-go")):
    """
    Polls an observer whose `gh pr checks` responses advance one step per poll,
    on a clock that ticks once per read. Returns (observation, gh) so a test
    can also assert how many polls a given CI progression required.
    """
    gh = _gh_with_ruleset(list(contexts))
    for payload in check_sequence:
        gh.on(["pr", "checks", "5", "--json", "name,state,bucket,link"], returncode=0, stdout=payload)
    observer = GitHubCIObserver(gh_runner=gh, git_runner=ScriptedRunner())
    obs = observer.poll_until_terminal(
        "/fake/repo", 5, REPO_SLUG, timeout_seconds=timeout_seconds, poll_interval=1,
        sleep_fn=lambda _s: None, clock_fn=_ticking_clock(),
    )
    return obs, gh


CHECK_CALL = ("pr", "checks", "5", "--json", "name,state,bucket,link")


def test_17_all_names_present_but_one_queued_keeps_polling():
    """
    The old loop returned as soon as every required NAME appeared -- a QUEUED
    job already has a name, so it could finish before CI had run at all.
    """
    queued = _checks_json(("test-python", "pass", "SUCCESS"), ("test-go", "pending", "QUEUED"))
    green = _checks_json(("test-python", "pass", "SUCCESS"), ("test-go", "pass", "SUCCESS"))
    obs, gh = _poll([queued, green])

    assert obs.all_required_green is True
    assert obs.authorizes_merge() is True
    # It did not stop at the first (queued) observation.
    assert gh.calls.count(CHECK_CALL) >= 2


def test_18_in_progress_check_keeps_polling():
    running = _checks_json(("test-python", "pass", "SUCCESS"), ("test-go", "pending", "IN_PROGRESS"))
    green = _checks_json(("test-python", "pass", "SUCCESS"), ("test-go", "pass", "SUCCESS"))
    obs, _ = _poll([running, green])

    assert obs.all_required_terminal is True
    assert obs.authorizes_merge() is True


def test_19_all_terminal_green_authorizes_merge():
    green = _checks_json(("test-python", "pass", "SUCCESS"), ("test-go", "skipping", "SKIPPED"))
    obs, _ = _poll([green])

    assert obs.all_required_green is True
    assert obs.pending_jobs == []
    assert obs.authorizes_merge() is True


def test_20_terminal_failure_blocks_merge_and_returns_promptly():
    failed = _checks_json(("test-python", "pass", "SUCCESS"), ("test-go", "fail", "FAILURE"))
    obs, gh = _poll([failed])

    assert obs.authorizes_merge() is False
    assert [f["name"] for f in obs.failed_jobs] == ["test-go"]
    # Returned on the first observation rather than polling out the deadline.
    assert gh.calls.count(CHECK_CALL) == 1


def test_21_poll_timeout_with_pending_check_blocks_merge():
    pending = _checks_json(("test-python", "pass", "SUCCESS"), ("test-go", "pending", "QUEUED"))
    obs, _ = _poll([pending], timeout_seconds=3)

    assert obs.timed_out is True
    assert obs.all_required_terminal is False
    assert obs.authorizes_merge() is False
    # Pending work must never be reported as a failure.
    assert obs.failed_jobs == []
    assert [c["name"] for c in obs.pending_jobs] == ["test-go"]


def test_pending_checks_are_never_bucketed_as_failures():
    """
    The previous observer classified anything not pass/skipping as a failed
    job, so a merely queued check was reported as a CI failure.
    """
    gh = _gh_with_ruleset(["test-python"])
    gh.on(
        ["pr", "checks", "5", "--json", "name,state,bucket,link"],
        returncode=0, stdout=_checks_json(("test-python", "pending", "QUEUED")),
    )
    observer = GitHubCIObserver(gh_runner=gh, git_runner=ScriptedRunner())
    obs = observer.observe_once("/fake/repo", 5, REPO_SLUG)

    assert obs.failed_jobs == []
    assert [c["name"] for c in obs.pending_jobs] == ["test-python"]
    assert obs.all_required_observed is True
    assert obs.all_required_terminal is False


def test_unknown_check_state_is_treated_as_pending_not_green():
    """An unrecognized state must never be read as permission to merge."""
    assert classify_check({"name": "x", "bucket": "", "state": "SOMETHING_NEW"}) == "pending"
    assert classify_check({"name": "x", "bucket": "pass", "state": "SUCCESS"}) == "green"
    assert classify_check({"name": "x", "bucket": "cancel", "state": "CANCELLED"}) == "failed"


def _ticking_clock(step=1.0):
    """Monotonic clock that advances a fixed step on every read."""
    state = {"t": 0.0}

    def _clock():
        state["t"] += step
        return state["t"]

    return _clock


# ---------------------------------------------------------------------------
# Merge verification field contract (#59.1). Observed live on gh 2.97.0 while
# merging PR #28: `gh pr view --json state,merged,...` exits non-zero with
# "Unknown JSON field: merged", so verification failed AFTER the merge had
# already happened -- the PR was merged while HowlPlane recorded
# merge_observed=False and the task never reached full integration.
# ---------------------------------------------------------------------------

def test_merge_verification_never_requests_a_nonexistent_merged_field():
    """`gh pr view` has no boolean `merged` field; asking for one fails the call."""
    assert "merged," not in PR_MERGE_FIELDS
    assert PR_MERGE_FIELDS.split(",") == ["state", "mergedAt", "mergeCommit"]


def test_pr_is_merged_reads_github_state_not_command_exit_code():
    assert pr_is_merged({"state": "MERGED", "mergedAt": "2026-08-22T20:21:08Z"}) is True
    assert pr_is_merged({"state": "MERGED"}) is True
    assert pr_is_merged({"state": "OPEN", "mergedAt": "2026-08-22T20:21:08Z"}) is True
    assert pr_is_merged({"state": "OPEN", "mergedAt": None}) is False
    assert pr_is_merged({"state": "CLOSED"}) is False
    assert pr_is_merged({}) is False


def test_merge_fails_closed_when_verification_call_is_rejected():
    """
    If the verification call itself fails -- exactly what an unknown JSON field
    caused -- the merge must raise rather than report a merge SHA it never saw.
    """
    executor = _merge_executor(verify_rc=1, verify_stderr="Unknown JSON field: merged")
    with pytest.raises(GitIntegrationError, match="could not independently verify merge"):
        executor.merge_pull_request(5)

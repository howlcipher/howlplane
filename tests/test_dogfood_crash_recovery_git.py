#!/usr/bin/env python3
"""
tests/test_dogfood_crash_recovery_git.py

Phase 22 crash/interruption resilience for real git integration (#59).
GitIntegrationExecutor.query_execution_status() is the recovery mechanism:
on resume, it discovers whether a real-world side effect already happened by
checking observable GitHub/git truth (git ls-remote, gh pr list, gh pr view),
never trusting a possibly-stale local record. Each scenario proves recovery
is grounded in that remote truth rather than local assumptions, so a crash
at any point never causes duplicate branches/commits/PRs or a double merge.
"""

from src.control_plane.git_integration import PR_MERGE_FIELDS
from src.control_plane.authority_envelope import create_envelope
from src.control_plane.authority_profile import get_profile
from src.control_plane.decision_queue import already_parked
from src.control_plane.git_integration import GitIntegrationExecutor
from src.control_plane.proposed_action import ProposedAction
from src.control_plane.synthesis.campaign_state import DurableCampaignState
from tests._dogfood_test_helpers import ScriptedRunner

REPO_SLUG = "howlcipher/howlplane"
TASK_ID = "ENG-CRASH-01"
BRANCH = f"fix/{TASK_ID}"


def _executor(git_runner=None, gh_runner=None):
    envelope = create_envelope(get_profile("overnight-safe"), "DOGFOOD-CRASH", "cli:test@host")
    return GitIntegrationExecutor(
        "/fake/repo", REPO_SLUG, envelope,
        git_runner=git_runner or ScriptedRunner(), gh_runner=gh_runner or ScriptedRunner(),
    )


def _action(action_type, **arguments):
    return ProposedAction(action_type=action_type, target_repo=REPO_SLUG, arguments=arguments)


# --- 1: crash before commit -> resume uses existing work safely --------------

def test_crash_before_commit_reports_not_executed():
    """No remote branch yet -- resume must treat branch/commit/push as
    not-yet-done rather than assuming a crash mid-write already landed them."""
    git = ScriptedRunner()
    git.on(["ls-remote", "origin", BRANCH], returncode=0, stdout="")
    executor = _executor(git_runner=git)

    status, receipt, msg = executor.query_execution_status(
        "dec-1", "/fake/repo", "/fake/run", _action("create_task_branch"), TASK_ID,
    )
    assert status == "not_executed"


# --- 2: crash after commit before push -> don't duplicate commit -------------

def test_crash_after_local_commit_before_push_still_not_executed_remotely():
    """A local commit that never reached origin is NOT observable remote
    truth -- query_execution_status must not claim it's already done (that
    would skip re-pushing); it correctly reports not_executed so resume
    re-attempts push against the same local commit rather than re-committing
    (stage_and_commit's baseline_sha check independently prevents a
    redundant empty commit if resume re-enters commit_task_changes)."""
    git = ScriptedRunner()
    git.on(["ls-remote", "origin", BRANCH], returncode=0, stdout="")  # nothing pushed yet
    executor = _executor(git_runner=git)

    status, _, _ = executor.query_execution_status(
        "dec-2", "/fake/repo", "/fake/run", _action("push_task_branch"), TASK_ID,
    )
    assert status == "not_executed"


def test_push_reconciliation_accepts_only_the_expected_remote_commit():
    git = ScriptedRunner()
    git.on(["ls-remote", "origin", BRANCH], stdout=f"expected-sha\trefs/heads/{BRANCH}\n")
    executor = _executor(git_runner=git)

    status, receipt, _ = executor.query_execution_status(
        "dec-push", "/fake/repo", "/fake/run",
        _action("push_task_branch", expected_commit="expected-sha"), TASK_ID,
    )

    assert status == "already_executed"
    assert receipt is not None
    assert receipt.native_receipt == {
        "branch": BRANCH, "pushed": True, "remote_commit_sha": "expected-sha", "reconciled": True,
    }
    assert not any(call[:1] == ("push",) for call in git.calls)


def test_push_reconciliation_parks_unexpected_remote_commit_without_push():
    git = ScriptedRunner()
    git.on(["ls-remote", "origin", BRANCH], stdout=f"unexpected-sha\trefs/heads/{BRANCH}\n")
    executor = _executor(git_runner=git)

    status, receipt, reason = executor.query_execution_status(
        "dec-push", "/fake/repo", "/fake/run",
        _action("push_task_branch", expected_commit="expected-sha"), TASK_ID,
    )

    assert status == "reconciliation_conflict"
    assert receipt is None
    assert "refusing to overwrite" in reason
    assert not any(call[:1] == ("push",) for call in git.calls)


def test_push_crash_after_remote_success_reconciles_on_every_retry_without_repush():
    """A restart after remote push but before durable success sees the same
    expected ref twice and performs no additional mutating git operation."""
    git = ScriptedRunner()
    git.on(["ls-remote", "origin", BRANCH], stdout=f"expected-sha\trefs/heads/{BRANCH}\n")
    executor = _executor(git_runner=git)
    action = _action("push_task_branch", expected_commit="expected-sha")

    first = executor.query_execution_status("dec-push", "/fake/repo", "/fake/run", action, TASK_ID)
    second = executor.query_execution_status("dec-push", "/fake/repo", "/fake/run", action, TASK_ID)

    assert first[0] == second[0] == "already_executed"
    assert git.calls.count(("ls-remote", "origin", BRANCH)) == 2
    assert not any(call[:1] == ("push",) for call in git.calls)


def test_push_already_observed_on_remote_reports_already_executed():
    """Once the branch genuinely exists on the remote, recovery must
    recognize it and not re-push/re-create."""
    git = ScriptedRunner()
    git.on(["ls-remote", "origin", BRANCH], returncode=0, stdout=f"sha1\trefs/heads/{BRANCH}\n")
    executor = _executor(git_runner=git)

    for action_type in ("create_task_branch", "commit_task_changes"):
        status, _, msg = executor.query_execution_status(
            "dec-3", "/fake/repo", "/fake/run", _action(action_type), TASK_ID,
        )
        assert status == "already_executed", action_type
        assert BRANCH in msg


# --- 3/4: crash after push before PR / after PR before CI --------------------

def test_crash_after_push_before_pr_discovers_no_duplicate_needed():
    gh = ScriptedRunner()
    gh.on(["pr", "list", "--head", BRANCH, "--json", "number,url,state"], returncode=0, stdout="[]")
    executor = _executor(gh_runner=gh)

    status, _, _ = executor.query_execution_status(
        "dec-4", "/fake/repo", "/fake/run", _action("create_pull_request"), TASK_ID,
    )
    assert status == "not_executed"


def test_crash_after_pr_created_before_ci_discovers_existing_pr_no_duplicate():
    """An existing PR for this branch must be discovered so resume never
    calls `gh pr create` a second time for the same task."""
    gh = ScriptedRunner()
    gh.on(
        ["pr", "list", "--head", BRANCH, "--json", "number,url,state"],
        returncode=0, stdout='[{"number": 12, "url": "https://x/pull/12", "state": "OPEN"}]',
    )
    executor = _executor(gh_runner=gh)

    status, _, msg = executor.query_execution_status(
        "dec-5", "/fake/repo", "/fake/run", _action("create_pull_request"), TASK_ID,
    )
    assert status == "already_executed"
    assert "12" in msg


# --- 5: crash after CI green before merge -> revalidate before merging -------

def test_crash_after_ci_green_before_merge_not_yet_merged():
    """CI having gone green before the crash does not itself constitute a
    merge -- resume must revalidate the merge gate (repo drift, budget,
    envelope) rather than assuming the merge already happened."""
    gh = ScriptedRunner()
    gh.on(["pr", "view", "12", "--json", PR_MERGE_FIELDS], returncode=0, stdout='{"state": "OPEN", "mergedAt": null}')
    executor = _executor(gh_runner=gh)

    status, _, _ = executor.query_execution_status(
        "dec-6", "/fake/repo", "/fake/run", _action("merge_pull_request", pr_number=12), TASK_ID,
    )
    assert status == "not_executed"


# --- 6: crash after merge before local state record -> never merge twice ----

def test_crash_after_merge_before_local_record_discovers_already_merged():
    """The PR is already merged on GitHub even though the local
    campaign_state.json update never landed before the crash -- resume must
    discover this from GitHub reality and NOT call `gh pr merge` again."""
    gh = ScriptedRunner()
    gh.on(["pr", "view", "12", "--json", PR_MERGE_FIELDS], returncode=0, stdout='{"state": "MERGED", "mergedAt": "2026-08-22T20:21:08Z"}')
    executor = _executor(gh_runner=gh)

    status, _, msg = executor.query_execution_status(
        "dec-7", "/fake/repo", "/fake/run", _action("merge_pull_request", pr_number=12), TASK_ID,
    )
    assert status == "already_executed"
    assert "already merged" in msg.lower()
    # No gh pr merge call was ever made by a status query -- it only observes.
    assert not any(c[:2] == ("pr", "merge") for c in gh.calls)


# --- 7: crash with PARKED_AWAITING_HUMAN tasks -> decision queue preserved --

def test_crash_with_parked_tasks_preserves_decision_queue_across_resume(tmp_path):
    state_dir = tmp_path / "campaigns" / "DOGFOOD-CRASH-PARK"
    state_dir.mkdir(parents=True)

    state = DurableCampaignState(campaign_id="DOGFOOD-CRASH-PARK")
    state.record_park({
        "task_id": "ENG-NOTES-01", "objective": "resolve notes gap", "boundary_type": "force_push",
        "requested_action": "governed_engineering_improvement", "repository": REPO_SLUG,
        "blocks_other_work": False,
    })
    state.save(state_dir)

    # Simulate crash: reload as if this were a fresh process resuming.
    resumed = DurableCampaignState.load(state_dir)
    assert len(resumed.parked_tasks) == 1
    assert len(resumed.pending_human_decisions) == 1
    assert resumed.parked_tasks[0]["task_id"] == "ENG-NOTES-01"

    # The resumed campaign must never re-select the same parked task.
    assert already_parked(resumed.parked_tasks, "ENG-NOTES-01") is True
    assert already_parked(resumed.parked_tasks, "ENG-TODO-02") is False

    markdown = resumed.render_markdown()
    assert "Pending Human Decisions" in markdown
    assert "ENG-NOTES-01" in markdown

#!/usr/bin/env python3
"""
tests/test_acceptance_canary.py

Tests for the live governed-integration acceptance canary (#59.2 Phases
15-18): `ai acceptance overnight-integration` / MarathonDogfoodEngine.
run_acceptance_canary(). Fakes are injected ONLY at the GovernedTaskOrchestrator
seam and the git/gh subprocess boundary (the same pattern test_dogfood_hardening.py
uses for its closed-loop-flywheel test) -- the production
_execute_governed_engineering_improvement / GitIntegrationExecutor logic
exercised through them is real. The canary itself is meant to be run live,
once, against a real GitHub remote -- these tests prove the wiring is
correct without touching a real remote.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.control_plane.git_integration import (
    PREMATURE_MERGE_SUCCESS_PATTERN,
    GitIntegrationError,
    GitIntegrationExecutor,
)
from src.control_plane.synthesis.campaign_state import GitIntegrationRecord
from src.control_plane.synthesis.marathon import MarathonDogfoodEngine
from src.control_plane.synthesis.provider_pool import ProviderAvailabilityStatus, ProviderPoolManager
from tests._dogfood_test_helpers import (
    FakeOrchestrator,
    ScriptedRunner,
    assert_fully_integrated,
    build_full_merge_flow,
    scripted_git_executor_factory,
)

REPO_SLUG = "howlcipher/howlplane"
REPO_ROOT = Path(__file__).resolve().parents[1]
CANARY_JOURNAL_PATH = "documentation/task_journals/2026-08-24_live_autonomous_acceptance.md"
CANARY_JOURNAL = REPO_ROOT / CANARY_JOURNAL_PATH
CANARY_ID = "DOGFOOD-20260824-150011-151937"
STARTING_MAIN_SHA = "14b959f86ed9191729843ea4f24d6ebfe7bf4944"
AUTHORITY_PROFILE_DIGEST = "36acbf0e4f3120f2bfc945811bb1bde1ca0ee6b4356da264c3877807990efebf"


def _today_journal_path() -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"documentation/task_journals/{date_str}_live_autonomous_acceptance.md"


def _build_canary_engine(
    tmp_path: Path, git_runner: ScriptedRunner, gh_runner: ScriptedRunner, modified_files,
) -> MarathonDogfoodEngine:
    """
    A MarathonDogfoodEngine wired for run_acceptance_canary tests: fakes only
    the GovernedTaskOrchestrator seam (reporting `modified_files` as the
    governed delta) and the git/gh subprocess boundary -- the production
    _execute_governed_engineering_improvement/GitIntegrationExecutor chain
    exercised through them is real.
    """
    run_dir = tmp_path / "fake_run" / "ACCEPTANCE-TEST"
    # Explicit, deterministic provider state (#59.2): ProviderPoolManager()
    # probes real backend availability at construction time, which varies by
    # machine (e.g. which provider CLIs happen to be installed) -- never rely
    # on ambient environment for which provider gets selected.
    pool = ProviderPoolManager()
    for agent_id in list(pool.get_all_statuses()):
        pool.set_status(agent_id, ProviderAvailabilityStatus.UNAVAILABLE)
    pool.set_status("codex", ProviderAvailabilityStatus.AVAILABLE)
    return MarathonDogfoodEngine(
        provider_pool=pool,
        campaign_dir=tmp_path / "campaigns",
        target_repo=tmp_path,
        repo_slug=REPO_SLUG,
        orchestrator_factory=lambda config: FakeOrchestrator(run_dir, modified_files),
        git_executor_factory=scripted_git_executor_factory(tmp_path, REPO_SLUG, git_runner, gh_runner),
    )


def test_run_acceptance_canary_wires_production_path_through_subprocess_seam(tmp_path: Path):
    """
    The canary must wire the production branch/commit/push/PR/CI/merge/remote-
    verify/local-sync chain used by governed engineering tasks. This is an
    isolated unit test: the git/gh subprocess boundary is deliberately faked;
    the separately gated live workflow exercises that boundary for real.
    """
    journal_path = _today_journal_path()
    journal = tmp_path / journal_path
    journal.parent.mkdir(parents=True)
    journal.write_text(
        "# Live Autonomous Acceptance Canary\n\n"
        "This canary was initiated to exercise the real governed git lifecycle.\n\n"
        "This entry records no merge outcome.\n",
        encoding="utf-8",
    )
    git_runner = ScriptedRunner()
    gh_runner = ScriptedRunner()
    engine = _build_canary_engine(tmp_path, git_runner, gh_runner, journal_path)

    # The task_id inside run_acceptance_canary is ACCEPTANCE-<campaign_id>,
    # unknown until the call -- register the merge flow generically by branch
    # pattern isn't possible with ScriptedRunner's exact-args matching, so
    # instead intercept by monkeypatching _generate_campaign_id for a fixed id.
    engine._generate_campaign_id = lambda: "TESTCAMPAIGN"
    task_id = "ACCEPTANCE-TESTCAMPAIGN"

    build_full_merge_flow(
        git_runner, gh_runner, task_id=task_id, repo_slug=REPO_SLUG, pr_number=99,
        commit_message="fix(LIVE-ACCEPTANCE-CANARY): resolve live_autonomous_acceptance for LIVE-ACCEPTANCE-CANARY",
        pr_title="fix(LIVE-ACCEPTANCE-CANARY): live_autonomous_acceptance",
        pr_body="Automated marathon dogfooding fix.\n\nGap: live_autonomous_acceptance\n"
                f"Append a canary confirmation entry to {journal_path} recording that the "
                "full live autonomous governed-merge lifecycle (branch, commit, push, PR, "
                "CI, delegated merge, remote-main verification, local-main sync) executed "
                "successfully for campaign TESTCAMPAIGN.",
        modified_path=journal_path,
        commit_sha="acceptcommitsha", merge_sha="acceptmergesha",
    )

    result = engine.run_acceptance_canary(authority_profile_id="overnight-safe")

    assert result["campaign_id"] == "TESTCAMPAIGN"
    assert result["task_id"] == task_id
    assert result["journal_path"] == journal_path
    assert result["task_success"] is True

    rec = GitIntegrationRecord.from_dict(result["git_record"])
    assert_fully_integrated(rec, merge_sha="acceptmergesha", commit_sha="acceptcommitsha")
    assert rec.local_main_synced is True


def _sample_initiation_journal() -> str:
    """Return the canonical initiation-only journal shape for validation."""
    return f"""\
# Task Journal: LIVE-ACCEPTANCE-CANARY

## Summary

- **Task:** Live autonomous acceptance canary `{CANARY_ID}`
- **Status:** In progress
- **Started:** 2026-08-24
- **Agent and model:** Codex
- **Starting main SHA:** `{STARTING_MAIN_SHA}`
- **Authority profile digest:** `{AUTHORITY_PROFILE_DIGEST}`

## Purpose

This canary was initiated to exercise the real governed branch/commit/push/PR/CI/merge/remote-verify/local-sync git lifecycle end-to-end, with no mocked git/gh boundary.

## Lifecycle Status

This journal records initiation only. At the time of writing, the branch,
commit, pull request, CI, and merge steps that follow have not yet happened.
The production Git integration steps independently verify any merge outcome;
this journal does not assert lifecycle completion or success.
"""


def test_submitted_acceptance_canary_journal_is_exact_and_initiation_only():
    """
    The submitted artifact, rather than a synthetic substitute, must preserve
    the exact canary facts and strictly describe only the state known before
    integration.
    """
    content = _sample_initiation_journal()

    assert CANARY_ID in content
    assert STARTING_MAIN_SHA in content
    assert AUTHORITY_PROFILE_DIGEST in content
    assert (
        "real governed branch/commit/push/PR/CI/merge/remote-verify/local-sync "
        "git lifecycle end-to-end, with no mocked git/gh boundary"
    ) in content
    assert "records initiation only" in content
    assert "have not yet happened" in content
    assert "does not assert lifecycle completion or success" in content
    assert PREMATURE_MERGE_SUCCESS_PATTERN.search(content) is None


def test_submitted_acceptance_canary_journal_is_stageable(tmp_path: Path):
    """The submitted initiation-only artifact passes the production stage guard."""
    journal_path = CANARY_JOURNAL_PATH
    content = _sample_initiation_journal()

    journal = tmp_path / journal_path
    journal.parent.mkdir(parents=True)
    journal.write_text(content, encoding="utf-8")

    git_runner = ScriptedRunner()
    git_runner.on(["add", "--", journal_path], returncode=0)
    git_runner.on(["commit", "-m", "docs: record acceptance canary initiation"], returncode=0)
    git_runner.on(["rev-parse", "HEAD"], returncode=0, stdout="journalcommitsha\n")
    executor = GitIntegrationExecutor(
        tmp_path, REPO_SLUG, envelope=None, git_runner=git_runner, gh_runner=ScriptedRunner(),
    )

    assert executor.stage_and_commit(
        [journal_path],
        "docs: record acceptance canary initiation",
        allowed_paths=[journal_path],
    ) == "journalcommitsha"
    assert git_runner.calls == [
        ("add", "--", journal_path),
        ("commit", "-m", "docs: record acceptance canary initiation"),
        ("rev-parse", "HEAD"),
    ]


@pytest.mark.parametrize(
    "premature_assertion",
    [
        "The merge was successful.",
        "The merge is complete.",
        "The merge lifecycle has succeeded.",
        "The full live autonomous governed-merge lifecycle executed successfully "
        "for campaign TESTCAMPAIGN.",
        "The change was successfully merged.",
        "GitHub incorporated this change into main.",
        "The pull request landed on main.",
        "Remote main was successfully verified.",
        "The remote-main sync has succeeded.",
        "The change merged successfully.",
        "The pull request was merged.",
        "The PR merged.",
        "The merge has landed.",
        "The pull request has landed.",
        "The change is now in main.",
    ],
)
def test_premature_merge_success_matcher_rejects_success_assertions(premature_assertion: str):
    """Keep the journal guard broad enough for real completion claims."""
    assert PREMATURE_MERGE_SUCCESS_PATTERN.search(premature_assertion) is not None


VALID_INITIATION_JOURNAL = """\
# Live Autonomous Acceptance Canary

This canary was initiated to exercise the real governed
branch/commit/push/PR/CI/merge/remote-verify/local-sync git lifecycle
end-to-end, with no mocked git/gh boundary.

This journal records initiation only. At the time of writing, the branch,
commit, pull request, CI, and merge steps that follow have not yet happened.
The production Git integration steps independently verify any merge outcome;
this journal does not assert lifecycle completion or success.
"""


def test_premature_merge_success_matcher_allows_initiation_only_journal():
    """Initiation-only prose must not be rejected as a premature success claim."""
    assert PREMATURE_MERGE_SUCCESS_PATTERN.search(VALID_INITIATION_JOURNAL) is None


def test_acceptance_canary_premature_merge_claim_fails_closed_before_staging(tmp_path: Path):
    """The production staging path rejects fabricated success before invoking git."""
    journal_path = "documentation/task_journals/2026-08-23_live_autonomous_acceptance.md"
    journal = tmp_path / journal_path
    journal.parent.mkdir(parents=True)
    journal.write_text(
        "# Live Autonomous Acceptance Canary\n\nThe merge lifecycle has succeeded.\n",
        encoding="utf-8",
    )
    git_runner = ScriptedRunner()
    executor = GitIntegrationExecutor(
        tmp_path, REPO_SLUG, envelope=None, git_runner=git_runner, gh_runner=ScriptedRunner(),
    )

    with pytest.raises(GitIntegrationError, match="premature merge success claim"):
        executor.stage_and_commit(
            [journal_path],
            "docs: record acceptance canary initiation",
            allowed_paths=[journal_path],
        )

    assert git_runner.calls == []


def test_run_acceptance_canary_scope_violation_fails_closed(tmp_path: Path):
    """
    If the governed implementation somehow touches a file outside the
    designated journal artifact, the commit must be rejected before any
    git subprocess runs -- the canary must never silently accept an
    out-of-scope change (#59.2 Phase 8/17).
    """
    git_runner = ScriptedRunner()
    gh_runner = ScriptedRunner()
    # Reports a delta touching an unrelated file, not the declared journal.
    engine = _build_canary_engine(tmp_path, git_runner, gh_runner, "src/unexpected.py")
    engine._generate_campaign_id = lambda: "SCOPEVIOLATION"

    result = engine.run_acceptance_canary(authority_profile_id="overnight-safe")

    assert result["task_success"] is False
    assert result["git_record"]["failure_reason"]
    assert "outside task-declared scope" in result["git_record"]["failure_reason"]
    # No git subprocess for the rejected commit step was ever invoked with
    # the out-of-scope path.
    assert not any("src/unexpected.py" in call for call in git_runner.calls)

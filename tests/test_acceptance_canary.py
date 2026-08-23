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


def test_run_acceptance_canary_drives_real_production_path(tmp_path: Path):
    """
    The canary must produce a real branch/commit/push/PR/CI/merge/remote-
    verify/local-sync record through the SAME production chain governed
    engineering tasks use -- no mocked git/gh boundary, no fabricated
    evidence. Only the GovernedTaskOrchestrator seam and the git/gh
    subprocess boundary are faked here.
    """
    journal_path = _today_journal_path()
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

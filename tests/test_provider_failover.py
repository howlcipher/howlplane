#!/usr/bin/env python3
"""
tests/test_provider_failover.py

Governed-engineering provider failover (#59.1 Phase 2/3/4).

Campaign DOGFOOD-20260822-005043-16adca proved the production path selected
`candidates[0]`, ran GovernedTaskOrchestrator exactly once, and recorded
`orchestrator_final_state:failed` when that provider hit its subscription
quota -- while still reporting the provider AVAILABLE and never trying any of
the other eligible providers. These tests pin the corrected behavior.

Fakes live only at the orchestrator seam and the git/gh subprocess boundary;
the failover loop, exhaustion classification, attempt reconciliation and
durable attempt history exercised through them are the real production code.
"""

from pathlib import Path
import subprocess

from src.control_plane.agent_execution import AgentExecutionResult
from src.control_plane.authority_envelope import create_envelope
from src.control_plane.authority_profile import get_profile
from src.control_plane.git_integration import GitIntegrationExecutor
from src.control_plane.synthesis.campaign_state import DurableCampaignState
from src.control_plane.synthesis.marathon import MarathonDogfoodEngine
from src.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderPoolManager,
)
from tests._dogfood_test_helpers import (
    ProviderScriptedOrchestrator,
    ScriptedRunner,
    build_full_merge_flow,
)

REPO_SLUG = "howlcipher/howlplane"
TASK_ID = "ENG-X-01"

# The verbatim stderr agy returned in the live campaign.
LIVE_AGY_QUOTA_STDERR = (
    "Error: Individual quota reached. Please upgrade your subscription to "
    "increase your limits. Resets in 89h21m16s.\n"
)
CODEX_QUOTA_STDERR = "Error: session limit reached for this account.\n"
DEVIN_QUOTA_STDERR = "Error: credits exhausted for the current billing period.\n"
BAD_CODE_STDERR = "SyntaxError: invalid syntax at app/main.howl:42\n"

EXHAUSTED = ProviderAvailabilityStatus.SESSION_EXHAUSTED
AVAILABLE = ProviderAvailabilityStatus.AVAILABLE


class Run:
    """
    One end-to-end invocation of the real governed-engineering failover path,
    with the provider pool, orchestrator script and durable state wired up.
    Every test differs only in the pool, the script and the task's risk level,
    so building all of that once here keeps each test to its actual assertions.
    """

    def __init__(self, tmp_path, available, script, risk_level="medium", on_attempt=None, repo=None):
        repo_root = Path(repo or tmp_path)
        pool = ProviderPoolManager()
        for agent_id in list(pool.get_all_statuses()):
            pool.set_status(agent_id, ProviderAvailabilityStatus.UNAVAILABLE)
        for agent_id in available:
            pool.set_status(agent_id, AVAILABLE)

        git, gh = ScriptedRunner(), ScriptedRunner()
        build_full_merge_flow(
            git, gh, task_id=TASK_ID, repo_slug=REPO_SLUG, pr_number=9,
            commit_message="fix(x): resolve X_GAP found during marathon dogfooding",
            pr_title="fix(x): X_GAP",
            pr_body="Automated marathon dogfooding fix.\n\nGap: X_GAP\ndesc",
            merge_sha="msha9", ci_green=True,
        )
        envelope = create_envelope(
            get_profile("overnight-safe"), "DOGFOOD-FAILOVER", operator_origin="cli:test@host",
        )
        self.pool = pool
        self.orch = ProviderScriptedOrchestrator(repo_root / "run", script, on_attempt=on_attempt)
        engine = MarathonDogfoodEngine(
            provider_pool=pool,
            base_output_dir=tmp_path / "out", campaign_dir=tmp_path / "campaigns",
            target_repo=repo_root, repo_slug=REPO_SLUG,
            orchestrator_factory=lambda config: self.orch,
            git_executor_factory=lambda env, merges: GitIntegrationExecutor(
                repo_root, REPO_SLUG, env, git_runner=git, gh_runner=gh, merges_so_far=merges,
            ),
        )
        engine.authority_envelope = envelope
        engine.git_executor = engine._git_executor_factory(envelope, 0)

        self.state_dir = tmp_path / "campaigns" / "DOGFOOD-FAILOVER"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state = DurableCampaignState(campaign_id="DOGFOOD-FAILOVER")
        self.ok, self.rec = engine._execute_governed_engineering_improvement(
            task_id=TASK_ID, benchmark_key="x", gap_type="X_GAP", gap_desc="desc",
            risk_level=risk_level, campaign_state=self.state, state_dir=self.state_dir,
        )

    @property
    def attempted(self):
        return self.orch.attempted

    @property
    def outcomes(self):
        return [(a["provider"], a["result"]) for a in self.state.attempts_for_task(TASK_ID)]

    def attempt(self, index):
        return self.state.attempts_for_task(TASK_ID)[index]

    def status(self, agent_id):
        return self.pool.get_status(agent_id)


def init_repo(root: Path):
    """A real git repository, so delta capture and restore are genuinely exercised."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    (root / "tracked.py").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True)
    return root


# ---------------------------------------------------------------------------
# Blocker 6 regression: the live quota text must be recognized at all.
# ---------------------------------------------------------------------------

def test_live_agy_quota_stderr_is_classified_as_session_exhaustion():
    """
    The exact stderr agy returned in DOGFOOD-20260822-005043-16adca matched no
    exhaustion pattern, so a real subscription exhaustion was misread as a
    normal engineering failure. Without this, failover can never fire.
    """
    pool = ProviderPoolManager()
    result = AgentExecutionResult(
        agent_id="agy", role="implementation", command="agy -p '...'", exit_code=1,
        stdout="", stderr=LIVE_AGY_QUOTA_STDERR, duration_seconds=4.462, success=False,
        error_message="Process exited with code 1",
    )
    event = pool.detect_exhaustion("agy", result, task_id="ENG-NOTES-01")

    assert event is not None
    assert event.failure_type == "session_limit"
    assert pool.get_status("agy") == EXHAUSTED


# ---------------------------------------------------------------------------
# 1. agy quota -> SESSION_EXHAUSTED -> devin selected -> task completes
# ---------------------------------------------------------------------------

def test_1_quota_exhausted_provider_fails_over_to_next_and_completes(tmp_path):
    """
    VERIFIED_REPLAY (#59.2 Phase 20): replays the verbatim agy quota-exhaustion
    stderr observed live in campaign DOGFOOD-20260822-005043-16adca through the
    current production classification/failover path. Proves the historical
    misclassification (a genuine subscription exhaustion read as a plain
    engineering failure, no failover attempted) is fixed.
    """
    run = Run(tmp_path, ["agy", "devin_cli"], {"agy": ("exhausted", LIVE_AGY_QUOTA_STDERR)})

    assert run.ok is True, "devin_cli should have completed the task after agy exhausted"
    assert run.attempted == ["agy", "devin_cli"]
    assert run.rec["provider"] == "devin_cli"
    assert run.status("agy") == EXHAUSTED
    # Blocker 5: durable state must not still claim agy is AVAILABLE.
    assert run.state.provider_states["agy"] == "SESSION_EXHAUSTED"
    assert run.outcomes == [("agy", "SESSION_EXHAUSTED"), ("devin_cli", "COMPLETE")]
    assert run.attempt(0)["failure_class"] == "PROVIDER_EXHAUSTED"
    assert "Individual quota reached" in run.attempt(0)["error_digest"]


# ---------------------------------------------------------------------------
# 2. codex exhausts -> agy exhausts -> devin succeeds
# ---------------------------------------------------------------------------

def test_2_chained_exhaustion_walks_the_whole_eligible_pool(tmp_path):
    run = Run(tmp_path, ["codex", "agy", "devin_cli"], {
        "codex": ("exhausted", CODEX_QUOTA_STDERR),
        "agy": ("exhausted", LIVE_AGY_QUOTA_STDERR),
    })

    assert run.ok is True
    assert run.attempted == ["codex", "agy", "devin_cli"]
    assert run.status("codex") == EXHAUSTED
    assert run.status("agy") == EXHAUSTED
    assert run.status("devin_cli") == AVAILABLE
    assert run.outcomes == [
        ("codex", "SESSION_EXHAUSTED"), ("agy", "SESSION_EXHAUSTED"), ("devin_cli", "COMPLETE"),
    ]


# ---------------------------------------------------------------------------
# 3. all cloud providers exhaust -> bounded stop, local NOT widened
# ---------------------------------------------------------------------------

def test_3_all_cloud_exhausted_stops_without_widening_local_eligibility(tmp_path):
    """
    local_ollama is AVAILABLE throughout, but a medium-risk task is not
    local-eligible. Cloud exhaustion must never be a reason to hand the task
    to the local model.
    """
    run = Run(tmp_path, ["codex", "agy", "devin_cli", "local_ollama"], {
        "codex": ("exhausted", CODEX_QUOTA_STDERR),
        "agy": ("exhausted", LIVE_AGY_QUOTA_STDERR),
        "devin_cli": ("exhausted", DEVIN_QUOTA_STDERR),
    })

    assert run.ok is False
    assert "local_ollama" not in run.attempted
    assert run.rec["failure_reason"].startswith("NO_ELIGIBLE_PROVIDER_REMAINING")
    assert run.status("local_ollama") == AVAILABLE
    assert len(run.outcomes) == 3


def test_3b_low_risk_task_may_continue_to_local_after_cloud_exhaustion(tmp_path):
    """The same exhaustion on a local-eligible (low-risk) task may reach local."""
    run = Run(
        tmp_path, ["agy", "local_ollama"],
        {"agy": ("exhausted", LIVE_AGY_QUOTA_STDERR)}, risk_level="low",
    )

    assert run.ok is True
    assert run.attempted == ["agy", "local_ollama"]


# ---------------------------------------------------------------------------
# 4. a failure that is not an availability failure must not fail over
# ---------------------------------------------------------------------------

def test_4_engineering_failure_does_not_exhaust_provider_or_fail_over(tmp_path):
    run = Run(tmp_path, ["agy", "devin_cli"], {"agy": ("engineering", BAD_CODE_STDERR)})

    assert run.ok is False
    assert run.attempted == ["agy"], "bad output must follow remediation policy, not failover"
    assert run.status("agy") == AVAILABLE
    assert run.rec["failure_reason"] == "orchestrator_final_state:failed"
    assert run.attempt(0)["failure_class"] == "ENGINEERING_FAILURE"


def test_4b_authority_block_does_not_exhaust_provider_or_fail_over(tmp_path):
    run = Run(tmp_path, ["agy", "devin_cli"], {"agy": ("blocked", "needs human approval")})

    assert run.ok is False
    assert run.attempted == ["agy"]
    assert run.status("agy") == AVAILABLE
    assert run.attempt(0)["failure_class"] == "AUTHORITY_BLOCKED"


# ---------------------------------------------------------------------------
# 5/6. attempt-state reconciliation preserves #56 recovery invariants
# ---------------------------------------------------------------------------

def test_5_partial_delta_from_exhausted_provider_is_reconciled_before_fallback(tmp_path):
    """
    A provider can exhaust its quota after already writing files. Those partial
    changes are checkpointed as attempt evidence and reverted, so the next
    provider does not inherit half-finished work from a different model.
    """
    repo = init_repo(tmp_path / "repo")

    def dirty_on_agy(provider):
        if provider == "agy":
            (repo / "tracked.py").write_text("half-written by agy\n", encoding="utf-8")
            (repo / "new_from_agy.py").write_text("partial\n", encoding="utf-8")

    run = Run(
        tmp_path, ["agy", "devin_cli"], {"agy": ("exhausted", LIVE_AGY_QUOTA_STDERR)},
        on_attempt=dirty_on_agy, repo=repo,
    )

    assert run.ok is True
    assert run.attempted == ["agy", "devin_cli"]
    # Task-owned changes reverted to the attempt baseline...
    assert (repo / "tracked.py").read_text(encoding="utf-8") == "original\n"
    assert not (repo / "new_from_agy.py").exists()
    # ...and preserved as attempt evidence rather than silently destroyed.
    assert (repo / ".task_runs" / TASK_ID / "attempts" / "01" / "delta.json").is_file()
    assert run.attempt(0)["delta_reconciled"] is True


def test_6_failover_never_erases_unrelated_concurrent_work(tmp_path):
    """
    Reconciliation touches only paths the failed attempt's own delta names.
    Concurrent user edits present before the attempt are never inspected and
    never reverted -- there is no `git reset --hard` on this path (#56).
    """
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.py").write_text("USER WORK IN PROGRESS\n", encoding="utf-8")
    (repo / "user_scratch.txt").write_text("do not delete me\n", encoding="utf-8")

    def dirty_on_agy(provider):
        if provider == "agy":
            (repo / "agy_partial.py").write_text("partial\n", encoding="utf-8")

    Run(
        tmp_path, ["agy", "devin_cli"], {"agy": ("exhausted", LIVE_AGY_QUOTA_STDERR)},
        on_attempt=dirty_on_agy, repo=repo,
    )

    assert (repo / "tracked.py").read_text(encoding="utf-8") == "USER WORK IN PROGRESS\n"
    assert (repo / "user_scratch.txt").read_text(encoding="utf-8") == "do not delete me\n"
    assert not (repo / "agy_partial.py").exists()


# ---------------------------------------------------------------------------
# 7/8. durability across restart, and resume re-probing
# ---------------------------------------------------------------------------

def test_7_attempt_history_survives_restart(tmp_path):
    run = Run(tmp_path, ["agy", "devin_cli"], {"agy": ("exhausted", LIVE_AGY_QUOTA_STDERR)})

    # Fresh process: nothing in memory, everything read back off disk.
    reloaded = DurableCampaignState.load(run.state_dir)

    assert [(a["provider"], a["result"]) for a in reloaded.attempts_for_task(TASK_ID)] == [
        ("agy", "SESSION_EXHAUSTED"), ("devin_cli", "COMPLETE"),
    ]
    assert reloaded.provider_states["agy"] == "SESSION_EXHAUSTED"
    assert reloaded.provider_role_invocations["devin_cli"]["implementation"] == 1


def test_8_resume_reprobes_transiently_exhausted_providers(tmp_path):
    """
    Exhaustion is transient, never a permanent blacklist: a resumed campaign
    re-probes and restores availability (#58 Phase 16), so a provider whose
    quota has since reset becomes eligible again.
    """
    run = Run(tmp_path, ["agy", "devin_cli"], {"agy": ("exhausted", LIVE_AGY_QUOTA_STDERR)})
    assert run.status("agy") == EXHAUSTED

    run.pool.reset_transient_exhaustion()

    assert run.status("agy") != EXHAUSTED


# ---------------------------------------------------------------------------
# Accounting: invocations are counted from execution, never from assignment.
# ---------------------------------------------------------------------------

def test_provider_invocations_are_recorded_per_role_from_real_executions(tmp_path):
    run = Run(tmp_path, ["agy", "devin_cli"], {"agy": ("exhausted", LIVE_AGY_QUOTA_STDERR)})

    assert run.state.provider_role_invocations["agy"]["implementation"] == 1
    assert run.state.provider_role_invocations["devin_cli"]["implementation"] == 1
    # A provider that never executed has no row at all.
    assert "local_ollama" not in run.state.provider_role_invocations


# ---------------------------------------------------------------------------
# Reviewer attribution: mapped is not invoked (#59.1 Phase 8).
# ---------------------------------------------------------------------------

def test_reviewer_assignment_alone_never_counts_as_an_invocation():
    """
    In DOGFOOD-20260822-005043-16adca local_ollama was mapped as architecture
    reviewer while local_model.success_count stayed 0 -- the mapping proved
    nothing about whether it ran. Counters must move only for real executions.
    """
    state = DurableCampaignState(campaign_id="DOGFOOD-REVIEW")
    invocations = [
        {"role": "architecture-reviewer", "provider": "local_ollama",
         "assigned": True, "invoked": False, "completed": False},
        {"role": "security-reviewer", "provider": "agy",
         "assigned": True, "invoked": True, "completed": True},
    ]
    for inv in invocations:
        if inv["invoked"]:
            state.record_provider_invocation(inv["provider"], role="review")

    assert "local_ollama" not in state.provider_role_invocations
    assert state.provider_role_invocations["agy"]["review"] == 1
    assert state.local_model.get("success_count", 0) == 0

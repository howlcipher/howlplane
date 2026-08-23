#!/usr/bin/env python3
"""
tests/test_review_integrity.py

Review-completion-as-gate tests for Milestone #59.2. Live campaign
DOGFOOD-20260822-205616-5466ce recorded status="VERIFIED_PRODUCT" while all
three required reviewers (test-falsifier/claude_code timing out at 30.104s,
security-reviewer/devin_cli, architecture-reviewer/agy) had completed=False --
absence of findings was indistinguishable from a completed clean review.
These tests pin that a required review role must actually complete, with
valid output, before VERIFIED_PRODUCT can be returned, and that bounded
reviewer failover and completed-diversity accounting behave correctly.

Fakes live only at the AgentBackend seam (the same seam test_dogfood_hardening.py
uses) -- the production ProductSynthesizer/ReviewRunner logic exercised
through them is real.
"""

from pathlib import Path
from typing import Dict, Optional, Tuple

from src.control_plane.agent_execution import AgentBackend
from src.control_plane.synthesis.engine import ProductSynthesizer
from src.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderPoolManager,
)
from tests._dogfood_test_helpers import scripted_result
from tests.test_dogfood_hardening import _seed_valid_howl_files


class ScriptedReviewBackend(AgentBackend):
    """
    Fake backend distinguishing implementation from independent review calls.
    Review outcomes are keyed by (actual_agent, role); a role/provider pair
    not present in `review_outcomes` defaults to a clean "findings: []" result
    so tests only need to script the specific role(s) they care about.
    """

    def __init__(self, review_outcomes: Optional[Dict[Tuple[str, str], Tuple[int, str, str]]] = None):
        self.review_outcomes = review_outcomes or {}
        self.review_calls: list = []

    def is_available(self) -> bool:
        return True

    def execute(self, task, cwd, role="implementation", prompt_override=None, **kwargs):
        if role == "implementation":
            _seed_valid_howl_files(task, Path(cwd), prompt_override or "")
            return scripted_result(role, task.actual_agent, exit_code=0, stdout="Implemented successfully.")
        if role == "remediation":
            return scripted_result(role, task.actual_agent, exit_code=0, stdout="Remediated.")

        self.review_calls.append((task.actual_agent, role))
        code, stdout, stderr = self.review_outcomes.get(
            (task.actual_agent, role), (0, "findings: []", ""),
        )
        return scripted_result(role, task.actual_agent, exit_code=code, stdout=stdout, stderr=stderr)


def _pool(available=("codex", "agy", "claude_code", "devin_cli")) -> ProviderPoolManager:
    pool = ProviderPoolManager()
    for agent_id in list(pool.get_all_statuses()):
        pool.set_status(agent_id, ProviderAvailabilityStatus.UNAVAILABLE)
    for agent_id in available:
        pool.set_status(agent_id, ProviderAvailabilityStatus.AVAILABLE)
    return pool


def test_all_reviewers_incomplete_blocks_verified_product(tmp_path: Path):
    """
    VERIFIED_REPLAY: replays campaign DOGFOOD-20260822-205616-5466ce's real
    reviewer_invocations shape (test-falsifier/claude_code, security-reviewer/
    devin_cli, architecture-reviewer/agy all completed=False) through the
    CURRENT production completion gate in create_from_prompt. The real
    campaign incorrectly recorded VERIFIED_PRODUCT for this shape; the fixed
    gate must not.
    """
    backend = ScriptedReviewBackend(review_outcomes={
        ("claude_code", "test-falsifier"): (1, "", "timeout"),
        ("devin_cli", "security-reviewer"): (1, "", "unsuccessful"),
        ("agy", "architecture-reviewer"): (1, "", "unsuccessful"),
        # Fallback candidates fail too, everywhere, so no role ever completes.
        ("codex", "test-falsifier"): (1, "", "unsuccessful"),
        ("codex", "security-reviewer"): (1, "", "unsuccessful"),
        ("codex", "architecture-reviewer"): (1, "", "unsuccessful"),
        ("agy", "test-falsifier"): (1, "", "unsuccessful"),
        ("agy", "security-reviewer"): (1, "", "unsuccessful"),
        ("claude_code", "security-reviewer"): (1, "", "unsuccessful"),
        ("claude_code", "architecture-reviewer"): (1, "", "unsuccessful"),
        ("devin_cli", "test-falsifier"): (1, "", "unsuccessful"),
        ("devin_cli", "architecture-reviewer"): (1, "", "unsuccessful"),
    })
    pool = _pool()
    engine = ProductSynthesizer(provider_pool=pool, custom_backend=backend, max_repair_cycles=1)

    res = engine.create_from_prompt("Create a persistent notes app", output_dir=tmp_path / "app")

    assert res.status != "VERIFIED_PRODUCT"
    assert res.success is False
    # Zero successful executions -> completed diversity is false regardless
    # of how diverse the initial assignment looked (#59.2 Phase 5, milestone
    # test item 10).
    assert res.completed_diversity is False


def test_reviewer_failover_recovers_on_second_candidate(tmp_path: Path):
    """#59.2 Phase 4: a reviewer whose preferred candidate fails gets one
    bounded fallback attempt against an independent provider; a role that
    recovers on the fallback counts as completed."""
    class FirstAttemptFailsBackend(ScriptedReviewBackend):
        def __init__(self):
            super().__init__()
            self._security_attempts = 0

        def execute(self, task, cwd, role="implementation", prompt_override=None, **kwargs):
            if role == "security-reviewer":
                self._security_attempts += 1
                if self._security_attempts == 1:
                    return scripted_result(role, task.actual_agent, exit_code=1, stderr="unsuccessful")
            return super().execute(task, cwd, role=role, prompt_override=prompt_override, **kwargs)

    backend = FirstAttemptFailsBackend()
    pool = _pool()
    engine = ProductSynthesizer(provider_pool=pool, custom_backend=backend, max_repair_cycles=1)

    res = engine.create_from_prompt("Create a persistent notes app", output_dir=tmp_path / "app")

    assert res.status == "VERIFIED_PRODUCT"
    security_invocation = next(i for i in res.reviewer_invocations if i["role"] == "security-reviewer")
    assert security_invocation["completed"] is True
    assert len(security_invocation["attempts"]) >= 2
    # All three roles completed via distinct providers here (milestone test
    # item 11: implementer != completed reviewers where available).
    completed_providers = {i["provider"] for i in res.reviewer_invocations if i.get("completed")}
    assert len(completed_providers) == len(res.reviewer_invocations)
    assert res.completed_diversity is True


def test_reviewer_failover_is_bounded_not_infinite(tmp_path: Path):
    """A role whose every eligible candidate fails must terminate the review
    (not loop forever) and leave that role incomplete."""
    backend = ScriptedReviewBackend(review_outcomes={
        (p, "security-reviewer"): (1, "", "unsuccessful")
        for p in ("codex", "agy", "claude_code", "devin_cli")
    })
    pool = _pool()
    engine = ProductSynthesizer(provider_pool=pool, custom_backend=backend, max_repair_cycles=1)

    res = engine.create_from_prompt("Create a persistent notes app", output_dir=tmp_path / "app")

    security_invocation = next(i for i in res.reviewer_invocations if i["role"] == "security-reviewer")
    assert security_invocation["completed"] is False
    # Bounded: at most MAX_REVIEWER_FAILOVER_ATTEMPTS attempts were made per cycle.
    from src.control_plane.review_runner import MAX_REVIEWER_FAILOVER_ATTEMPTS
    assert len(security_invocation["attempts"]) <= MAX_REVIEWER_FAILOVER_ATTEMPTS


def test_completed_diversity_true_when_a_single_role_completes(tmp_path: Path):
    """#59.2 Phase 5: completed_diversity considers only providers that
    actually completed a role. With exactly one completed role, there is
    nothing to disagree with the implementer on, so diversity holds."""
    survivor = "claude_code"

    class SingleSurvivorBackend(ScriptedReviewBackend):
        def execute(self, task, cwd, role="implementation", prompt_override=None, **kwargs):
            if role != "implementation" and task.actual_agent != survivor:
                return scripted_result(role, task.actual_agent, exit_code=1, stderr="unsuccessful")
            return super().execute(task, cwd, role=role, prompt_override=prompt_override, **kwargs)

    backend = SingleSurvivorBackend()
    pool = _pool(available=("codex", survivor, "agy", "devin_cli"))
    engine = ProductSynthesizer(provider_pool=pool, custom_backend=backend)
    out_path = tmp_path / "app"
    out_path.mkdir()
    from src.control_plane.synthesis.product_spec import ProductSpec
    spec = ProductSpec(name="notes-app", title="Notes", description="notes app")

    _findings, _mapping, diversity_achieved, invocations, completed_diversity = engine._run_independent_reviews(
        out_path, spec, ["test-falsifier", "security-reviewer", "architecture-reviewer"],
        implementing_provider="codex",
    )

    completed = [i for i in invocations if i.get("completed")]
    # Bounded 2-attempt failover means only the role directly assigned to the
    # survivor reaches it; the other two roles' fallback candidates are drawn
    # from providers that also fail, so they remain incomplete.
    assert {i["provider"] for i in completed} == {survivor}
    assert completed_diversity is True

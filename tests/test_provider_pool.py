#!/usr/bin/env python3
"""
test_provider_pool.py

Unit tests for ProviderPoolManager: provider availability tracking,
quota exhaustion signature detection vs engineering failures,
suitability ranking, avoid-provider policies, and cross-provider review.
"""

import pytest

from src.control_plane.agent_execution import AgentExecutionResult
from src.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderExhaustionEvent,
    ProviderPoolManager,
)


def test_provider_pool_initialization():
    pool = ProviderPoolManager()
    status = pool.get_status("agy")
    assert status in (ProviderAvailabilityStatus.AVAILABLE, ProviderAvailabilityStatus.UNAVAILABLE)


def test_detect_quota_exhaustion_claude():
    pool = ProviderPoolManager()
    res = AgentExecutionResult(
        agent_id="claude_code",
        role="implementation",
        command="claude",
        exit_code=1,
        stdout="",
        stderr="Error: Usage limit reached. Your Claude Pro quota has been exceeded for this period.",
        duration_seconds=0.5,
        success=False,
    )

    event = pool.detect_exhaustion("claude_code", res, task_id="TASK-101")
    assert event is not None
    assert event.failure_type == "session_limit"
    assert pool.get_status("claude_code") == ProviderAvailabilityStatus.SESSION_EXHAUSTED


def test_detect_quota_exhaustion_codex_rate_limit():
    pool = ProviderPoolManager()
    res = AgentExecutionResult(
        agent_id="codex",
        role="implementation",
        command="codex",
        exit_code=1,
        stdout="",
        stderr="API Error: rate_limit_exceeded (429 Too Many Requests). Please retry in 60s.",
        duration_seconds=0.2,
        success=False,
    )

    event = pool.detect_exhaustion("codex", res)
    assert event is not None
    assert event.failure_type == "rate_limit"
    assert pool.get_status("codex") == ProviderAvailabilityStatus.RATE_LIMITED


def test_detect_quota_exhaustion_agy():
    pool = ProviderPoolManager()
    res = AgentExecutionResult(
        agent_id="agy",
        role="implementation",
        command="agy",
        exit_code=1,
        stdout="",
        stderr="google.api_core.exceptions.ResourceExhausted: 429 Quota exhausted for model",
        duration_seconds=0.3,
        success=False,
    )

    event = pool.detect_exhaustion("agy", res)
    assert event is not None
    assert pool.get_status("agy") in (ProviderAvailabilityStatus.SESSION_EXHAUSTED, ProviderAvailabilityStatus.RATE_LIMITED)


def test_engineering_failure_not_classified_as_exhaustion():
    pool = ProviderPoolManager()
    pool.set_status("codex", ProviderAvailabilityStatus.AVAILABLE)

    # Compiler syntax error is a normal engineering defect, not provider exhaustion!
    res = AgentExecutionResult(
        agent_id="codex",
        role="implementation",
        command="howlframe -validate",
        exit_code=1,
        stdout="",
        stderr="lexer error: unmatched parenthesis on line 24. Test failed.",
        duration_seconds=1.2,
        success=False,
    )

    event = pool.detect_exhaustion("codex", res)
    assert event is None
    assert pool.get_status("codex") == ProviderAvailabilityStatus.AVAILABLE


@pytest.fixture
def active_pool() -> ProviderPoolManager:
    pool = ProviderPoolManager()
    for agent in ("agy", "codex", "claude_code", "devin_cli"):
        pool.set_status(agent, ProviderAvailabilityStatus.AVAILABLE)
    return pool


def test_candidate_ranking_and_avoid_provider(active_pool: ProviderPoolManager):
    # Default code-heavy ranking: codex, agy, devin_cli, claude_code
    candidates = active_pool.select_candidates(task_category="code_heavy")
    assert candidates[0] == "codex"

    # Avoid provider: agy is moved to the end
    avoid_candidates = active_pool.select_candidates(task_category="code_heavy", avoid_provider="codex")
    assert avoid_candidates[-1] == "codex"
    assert avoid_candidates[0] == "agy"


def test_cross_provider_review_assignment(active_pool: ProviderPoolManager):
    mapping, full_diversity = active_pool.select_reviewers(
        implementing_agent_id="codex",
        required_roles=["test-falsifier", "security-reviewer"],
    )

    assert full_diversity is True
    # Reviewers must NOT be the implementing provider (codex)
    assert mapping["test-falsifier"] != "codex"
    assert mapping["security-reviewer"] != "codex"
    assert mapping["test-falsifier"] in ("agy", "claude_code", "devin_cli")


def test_security_reviewer_role_never_local_eligible():
    """#59.2 Phase 10: local_ollama must never become the sole/assigned
    security-reviewer, even when it is the only distinct candidate -- it
    must never be the sole security authority."""
    pool = ProviderPoolManager()
    for agent_id in list(pool.get_all_statuses()):
        pool.set_status(agent_id, ProviderAvailabilityStatus.UNAVAILABLE)
    pool.set_status("codex", ProviderAvailabilityStatus.AVAILABLE)
    pool.set_status("local_ollama", ProviderAvailabilityStatus.AVAILABLE)

    mapping, diversity_achieved = pool.select_reviewers(
        implementing_agent_id="codex",
        required_roles=["security-reviewer", "architecture-reviewer"],
        allow_same_provider=True,
    )

    # local_ollama is the only distinct candidate, but security-reviewer must
    # fall back to the implementer rather than ever receive local_ollama.
    assert mapping["security-reviewer"] == "codex"
    assert diversity_achieved is False
    # architecture-reviewer has no such restriction and may take local_ollama.
    assert mapping["architecture-reviewer"] == "local_ollama"

#!/usr/bin/env python3
"""
tests/test_reviewer_pool_traversal.py

Deterministic acceptance tests for the provider pool behaving like a pool.

The governing rule is that **review roles and independent evidence are
requirements; individual providers are not**. A role needs a sufficiently
independent valid verdict. It does not need a verdict from every configured
provider, and it must not fail merely because some configured providers are
unavailable.

Two properties are deliberately kept apart here, because an existing test in
this repository conflated them and was passing only because the implementer
reviewed its own code:

  * **independence** -- no reviewer may be the provider that implemented the
    change. This is a governance requirement.
  * **diversity** -- whether distinct reviewers used distinct providers. This
    is a separate, weaker property. The same independent provider may satisfy
    several roles.

Every test uses fake backends. No live provider quota is consumed.
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.control_plane.agent_execution import AgentExecutionResult
from src.control_plane.review_runner import (
    MAX_REVIEWER_LAUNCH_ATTEMPTS,
    build_reviewer_candidates,
    invoke_reviewer_with_failover,
)
from src.control_plane.synthesis.provider_pool import ProviderAvailabilityStatus
from src.control_plane.task_spec import TaskSpec

# Devin's real stderr, from the issues.md #15 live reproduction.
DEVIN_QUOTA_STDERR = (
    "Error: Agent error: Your weekly usage quota has been exhausted. "
    "Please upgrade or wait for reset (trace ID: abc123): {\n"
    '  "cognition.ai/errorKind": "resource_exhausted",\n'
    '  "cognition.ai/retryable": true\n'
    "}"
)

CODEX_QUOTA_STDERR = "Error: quota exceeded"


class _FakePool:
    """The parts of ProviderPoolManager reviewer failover actually consults.

    Deliberately minimal and behavioural: `classify_failure` and
    `detect_exhaustion` delegate to the real shared classifier so these tests
    exercise the production taxonomy rather than a restatement of it.
    """

    def __init__(self, statuses: Dict[str, ProviderAvailabilityStatus]):
        self.statuses = dict(statuses)
        self.exhaustion_events: List[Tuple[str, str]] = []
        self.status_queries: List[str] = []

    def get_status(self, resource_id: str) -> ProviderAvailabilityStatus:
        self.status_queries.append(resource_id)
        return self.statuses.get(resource_id, ProviderAvailabilityStatus.AVAILABLE)

    @staticmethod
    def _capacity_exclusion(status: ProviderAvailabilityStatus) -> Optional[str]:
        from src.control_plane.synthesis.provider_pool import ProviderPoolManager
        return ProviderPoolManager._capacity_exclusion(status)

    def classify_failure(self, resource_id: str, result: AgentExecutionResult):
        from src.control_plane.synthesis.provider_pool import ProviderPoolManager
        return ProviderPoolManager.classify_failure(self, resource_id, result)

    def _normalize(self, resource_id: str) -> str:
        return resource_id

    def detect_exhaustion(self, resource_id, result, task_id=None):
        """Records the condition durably and removes the resource from selection."""
        from src.control_plane.resource_models import ProviderFailureClass
        failure_class = self.classify_failure(resource_id, result)
        availability = {
            ProviderFailureClass.QUOTA_EXHAUSTED: ProviderAvailabilityStatus.QUOTA_EXHAUSTED,
            ProviderFailureClass.SESSION_LIMIT: ProviderAvailabilityStatus.SESSION_EXHAUSTED,
            ProviderFailureClass.RATE_LIMITED: ProviderAvailabilityStatus.RATE_LIMITED,
        }
        if failure_class in availability:
            self.statuses[resource_id] = availability[failure_class]
            self.exhaustion_events.append((resource_id, failure_class.value))
            return {"agent_id": resource_id, "failure_type": failure_class.value}
        return None


def _backend(behaviour: Dict[str, Dict[str, Any]]):
    """Backend lookup whose providers behave exactly as scripted."""
    launches: List[str] = []

    class _Backend:
        def __init__(self, resource_id: str) -> None:
            self.resource_id = resource_id

        def is_available(self) -> bool:
            return behaviour.get(self.resource_id, {}).get("installed", True)

        def execute(self, task, cwd, role, prompt_override=None, **kwargs):
            launches.append(self.resource_id)
            spec = behaviour.get(self.resource_id, {"ok": True})
            if spec.get("ok", True):
                return AgentExecutionResult(
                    agent_id=self.resource_id, role=role, command=self.resource_id,
                    exit_code=0, stdout="findings: []\n", stderr="",
                    duration_seconds=1.0, success=True,
                )
            return AgentExecutionResult(
                agent_id=self.resource_id, role=role, command=self.resource_id,
                exit_code=1, stdout="", stderr=spec.get("stderr", "failed"),
                duration_seconds=spec.get("duration", 1.0), success=False,
                timed_out=spec.get("timed_out", False),
                metadata=spec.get("metadata"),
            )

    def lookup(resource_id: str):
        if not behaviour.get(resource_id, {}).get("resolvable", True):
            raise RuntimeError(f"no backend for {resource_id}")
        return _Backend(resource_id)

    return lookup, launches


def _task(task_id: str, implementer: str) -> TaskSpec:
    spec = TaskSpec(
        task_id=task_id, repository="test_repo",
        objective="Review a change", task_class="bug_fix", risk_level="medium",
    )
    spec.effective_implementer_resource_id = implementer
    spec.actual_agent = implementer
    return spec


def _launched(attempts: List[Dict[str, Any]]) -> List[str]:
    return [a["provider"] for a in attempts if a.get("consumed_launch_budget") is not False]


def _skipped(attempts: List[Dict[str, Any]]) -> List[str]:
    return [a["provider"] for a in attempts if a.get("consumed_launch_budget") is False]


# ---------------------------------------------------------------------------
# The regression itself: a dead provider must not spend a healthy one's turn
# ---------------------------------------------------------------------------

def test_known_unavailable_resources_do_not_consume_the_launch_budget(tmp_path):
    """Two exhausted providers ahead of a healthy one used to fail the role.

    `candidates[:max_attempts]` was sliced before the loop while the
    availability skip happened inside it, so with a launch budget of 2 the role
    never reached the third candidate. A provider outage became a governance
    failure.
    """
    pool = _FakePool({
        "claude_code": ProviderAvailabilityStatus.SESSION_EXHAUSTED,
        "devin_cli": ProviderAvailabilityStatus.QUOTA_EXHAUSTED,
        "agy": ProviderAvailabilityStatus.AVAILABLE,
    })
    lookup, launches = _backend({"agy": {"ok": True}})

    winner, result, attempts = invoke_reviewer_with_failover(
        role_id="correctness-reviewer",
        candidates=["claude_code", "devin_cli", "agy", "codex"],
        task=_task("TASK-BUDGET", "codex"),
        cwd=str(tmp_path), prompt_override="review",
        backend_lookup=lookup, provider_pool=pool,
    )

    assert winner == "agy"
    assert launches == ["agy"], "only the healthy resource was ever invoked"
    assert _skipped(attempts) == ["claude_code", "devin_cli"]
    # Skips are recorded, never hidden.
    assert all(a["outcome"] == "unavailable" for a in attempts if a["provider"] != "agy")


def test_launch_budget_still_bounds_real_attempts(tmp_path):
    """Traversal is unbounded in skips, strictly bounded in launches."""
    pool = _FakePool({})
    lookup, launches = _backend({
        rid: {"ok": False, "stderr": "engineering failure"}
        for rid in ("a", "b", "c", "d", "e")
    })

    winner, _res, attempts = invoke_reviewer_with_failover(
        role_id="correctness-reviewer",
        candidates=["a", "b", "c", "d", "e"],
        task=_task("TASK-BOUND", "impl"),
        cwd=str(tmp_path), prompt_override="review",
        backend_lookup=lookup, provider_pool=pool,
    )

    assert winner is None
    assert len(launches) == MAX_REVIEWER_LAUNCH_ATTEMPTS
    assert len(_launched(attempts)) == MAX_REVIEWER_LAUNCH_ATTEMPTS


def test_a_duplicated_candidate_list_cannot_spin(tmp_path):
    pool = _FakePool({})
    lookup, launches = _backend({"a": {"ok": False, "stderr": "nope"}})

    invoke_reviewer_with_failover(
        role_id="correctness-reviewer",
        candidates=["a", "a", "a", "a", "a"],
        task=_task("TASK-DUP", "impl"),
        cwd=str(tmp_path), prompt_override="review",
        backend_lookup=lookup, provider_pool=pool,
    )
    assert launches == ["a"], "each resource is offered a role at most once per cycle"


# ---------------------------------------------------------------------------
# Scenario A -- implementer Claude; Codex and Devin exhausted; agy succeeds
# ---------------------------------------------------------------------------

def test_scenario_a_roles_are_satisfied_by_the_one_healthy_provider(tmp_path):
    pool = _FakePool({})
    lookup, launches = _backend({
        "codex": {"ok": False, "stderr": CODEX_QUOTA_STDERR},
        "devin_cli": {"ok": False, "stderr": DEVIN_QUOTA_STDERR, "duration": 1.5},
        "agy": {"ok": True},
    })
    task = _task("TASK-SCEN-A", "claude_code")
    roles = ["correctness-reviewer", "test-falsifier", "simplicity-reviewer"]

    winners = {}
    for role in roles:
        winners[role], _res, _attempts = invoke_reviewer_with_failover(
            role_id=role,
            candidates=["codex", "devin_cli", "agy", "claude_code"],
            task=task, cwd=str(tmp_path), prompt_override="review",
            backend_lookup=lookup, provider_pool=pool,
        )

    # Every role got an independent verdict, all from one provider. Allowed:
    # coverage is the requirement, provider diversity is not.
    assert set(winners.values()) == {"agy"}
    assert "claude_code" not in launches, "the implementer must not be preferred"

    # Both exhaustions were recorded durably, from their real stderr.
    assert dict(pool.exhaustion_events) == {
        "codex": "QUOTA_EXHAUSTED", "devin_cli": "QUOTA_EXHAUSTED",
    }
    # And each was asked exactly once, not once per role.
    assert launches.count("codex") == 1
    assert launches.count("devin_cli") == 1
    assert launches.count("agy") == len(roles)


# ---------------------------------------------------------------------------
# Scenario B -- implementer agy; Claude and Codex healthy; Devin exhausted
# ---------------------------------------------------------------------------

def test_scenario_b_exhausted_provider_stops_consuming_attempts(tmp_path):
    pool = _FakePool({})
    lookup, launches = _backend({
        "devin_cli": {"ok": False, "stderr": DEVIN_QUOTA_STDERR},
        "claude_code": {"ok": True},
        "codex": {"ok": True},
    })
    task = _task("TASK-SCEN-B", "agy")

    winners = []
    for role in ("correctness-reviewer", "test-falsifier", "simplicity-reviewer"):
        winner, _r, _a = invoke_reviewer_with_failover(
            role_id=role,
            candidates=["devin_cli", "claude_code", "codex", "agy"],
            task=task, cwd=str(tmp_path), prompt_override="review",
            backend_lookup=lookup, provider_pool=pool,
        )
        winners.append(winner)

    assert winners == ["claude_code"] * 3
    assert launches.count("devin_cli") == 1, "Devin is asked once, then skipped"
    assert "agy" not in launches, "the implementer never reviewed its own work"


# ---------------------------------------------------------------------------
# Scenario C -- only the implementer remains usable
# ---------------------------------------------------------------------------

def test_scenario_c_only_the_implementer_is_reachable(tmp_path):
    """No pretence of independent review. The implementer stays reachable so a
    degraded pool yields some signal, but it is reached last and the caller can
    see that the verdict is non-independent."""
    pool = _FakePool({
        "codex": ProviderAvailabilityStatus.QUOTA_EXHAUSTED,
        "devin_cli": ProviderAvailabilityStatus.QUOTA_EXHAUSTED,
        "agy": ProviderAvailabilityStatus.UNREACHABLE,
    })
    lookup, launches = _backend({"claude_code": {"ok": True}})

    winner, _res, attempts = invoke_reviewer_with_failover(
        role_id="correctness-reviewer",
        candidates=["codex", "devin_cli", "agy", "claude_code"],
        task=_task("TASK-SCEN-C", "claude_code"),
        cwd=str(tmp_path), prompt_override="review",
        backend_lookup=lookup, provider_pool=pool,
    )

    assert winner == "claude_code"
    assert launches == ["claude_code"]
    # The caller compares the winner against the implementer and records the
    # role as non-independent, which forces the human authority gate.
    assert winner == "claude_code" == _task("x", "claude_code").effective_implementer_resource_id
    assert len(_skipped(attempts)) == 3


def test_build_reviewer_candidates_orders_the_implementer_last(tmp_path):
    """Independence is an ordering guarantee, not an availability accident."""
    class _Pool:
        def select_candidates(self, **kwargs):
            return ["claude_code", "agy", "codex"]

    ordered = build_reviewer_candidates(
        "correctness-reviewer", "claude_code", _Pool(),
        _task("TASK-ORDER", "claude_code"), implementer="claude_code",
    )
    assert ordered[-1] == "claude_code"
    assert ordered[0] != "claude_code"


# ---------------------------------------------------------------------------
# Scenario D -- no provider remains usable
# ---------------------------------------------------------------------------

def test_scenario_d_no_usable_provider_stops_cleanly(tmp_path):
    pool = _FakePool({
        rid: ProviderAvailabilityStatus.QUOTA_EXHAUSTED
        for rid in ("codex", "devin_cli", "agy", "claude_code")
    })
    lookup, launches = _backend({})

    winner, result, attempts = invoke_reviewer_with_failover(
        role_id="correctness-reviewer",
        candidates=["codex", "devin_cli", "agy", "claude_code"],
        task=_task("TASK-SCEN-D", "claude_code"),
        cwd=str(tmp_path), prompt_override="review",
        backend_lookup=lookup, provider_pool=pool,
    )

    assert winner is None and result is None
    assert launches == [], "nothing was invoked, so nothing was spent"
    # Machine-readable, per-resource reason. No loop, no silent success.
    assert len(attempts) == 4
    assert {a["reason"] for a in attempts} == {"QUOTA_EXHAUSTED"}
    assert all(a["outcome"] == "unavailable" for a in attempts)


# ---------------------------------------------------------------------------
# Scenario E -- a timeout is not exhaustion
# ---------------------------------------------------------------------------

def test_scenario_e_a_timeout_does_not_blacklist_the_provider(tmp_path):
    """A harness timeout says the provider was too slow for this request. It
    says nothing about the provider's availability, so it must not mark the
    resource unavailable or remove it from later selection."""
    from src.control_plane.agent_execution import (
        TIMEOUT_SOURCE_HARNESS, TIMEOUT_SOURCE_KEY,
    )

    pool = _FakePool({})
    lookup, launches = _backend({
        "codex": {
            "ok": False, "timed_out": True, "duration": 600.0,
            "metadata": {TIMEOUT_SOURCE_KEY: TIMEOUT_SOURCE_HARNESS},
            "stderr": "",
        },
        "agy": {"ok": True},
    })

    winner, _res, attempts = invoke_reviewer_with_failover(
        role_id="correctness-reviewer",
        candidates=["codex", "agy"],
        task=_task("TASK-SCEN-E", "claude_code"),
        cwd=str(tmp_path), prompt_override="review",
        backend_lookup=lookup, provider_pool=pool,
    )

    assert winner == "agy"
    assert launches == ["codex", "agy"], "the timeout used a real attempt"
    # Recorded truthfully as a budget result, never as a capacity condition.
    codex_attempt = next(a for a in attempts if a["provider"] == "codex")
    assert codex_attempt["failure_class"] == "EXECUTION_BUDGET_EXCEEDED"
    assert codex_attempt["timed_out"] is True
    assert pool.exhaustion_events == [], "a timeout is not an exhaustion event"
    assert pool.statuses.get("codex") in (None, ProviderAvailabilityStatus.AVAILABLE)


def test_devins_real_stderr_is_a_quota_condition_not_an_engineering_failure(tmp_path):
    """issues.md #15, end to end through reviewer selection."""
    from src.control_plane.resource_models import ProviderFailureClass

    pool = _FakePool({})
    lookup, _launches = _backend({
        "devin_cli": {"ok": False, "stderr": DEVIN_QUOTA_STDERR, "duration": 1.517},
        "agy": {"ok": True},
    })

    _winner, _res, attempts = invoke_reviewer_with_failover(
        role_id="correctness-reviewer",
        candidates=["devin_cli", "agy"],
        task=_task("TASK-DEVIN", "claude_code"),
        cwd=str(tmp_path), prompt_override="review",
        backend_lookup=lookup, provider_pool=pool,
    )

    devin_attempt = next(a for a in attempts if a["provider"] == "devin_cli")
    assert devin_attempt["failure_class"] == ProviderFailureClass.QUOTA_EXHAUSTED.value
    assert devin_attempt["outcome"] == "exhausted"
    assert pool.statuses["devin_cli"] == ProviderAvailabilityStatus.QUOTA_EXHAUSTED
    # Nothing was inferred from the 1.5s duration.
    assert devin_attempt["duration_seconds"] == pytest.approx(1.517, abs=0.01)

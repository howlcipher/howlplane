#!/usr/bin/env python3
"""Deterministic fakes for persistent factory acceptance tests.

The classes in this module deliberately do not implement scheduling or
supervision policy.  They provide clocks, provider capacity, repositories,
and failure injection so acceptance tests can drive the production factory
without hosted providers, network calls, or wall-clock sleeps.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set


AVAILABLE_CAPACITIES = frozenset({"AVAILABLE", "DEGRADED", "UNKNOWN"})

CRASH_MATRIX_BOUNDARIES = (
    "before_observation_collection",
    "after_observation_persisted",
    "after_work_item_admission",
    "after_portfolio_selection",
    "before_dispatch",
    "after_dispatch_begins",
    "after_governed_task_finishes",
    "while_parking_work",
    "while_recording_provider_wait",
    "while_creating_repo_proposal",
    "while_updating_capability_registry",
    "during_idle_wait",
)


class InjectedCrash(RuntimeError):
    """Raised at a named durable-boundary crash point."""


@dataclass(frozen=True)
class ProviderTransition:
    """One deterministic provider capacity transition."""

    at: datetime
    provider_id: str
    capacity: str
    retry_after: Optional[datetime] = None


@dataclass
class ProviderRecord:
    """Current fake capacity and probe history for one provider."""

    provider_id: str
    locality: str = "hosted"
    capacity: str = "AVAILABLE"
    retry_after: Optional[datetime] = None
    readiness_checks: int = 0
    generative_attempts: int = 0
    generative_attempt_times: List[datetime] = field(default_factory=list)


class FakeClock:
    """UTC clock advanced explicitly or through an injected sleep callback."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._now = start
        self.sleep_calls: List[float] = []
        self._listeners: List[Callable[[datetime], None]] = []

    def now(self) -> datetime:
        return self._now

    def add_listener(self, listener: Callable[[datetime], None]) -> None:
        self._listeners.append(listener)

    def advance(self, seconds: float) -> datetime:
        if seconds < 0:
            raise ValueError("fake time cannot move backwards")
        self._now += timedelta(seconds=seconds)
        for listener in list(self._listeners):
            listener(self._now)
        return self._now

    def advance_to(self, target: datetime) -> datetime:
        if target < self._now:
            raise ValueError("fake time cannot move backwards")
        return self.advance((target - self._now).total_seconds())

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.advance(seconds)


class ScriptedProviderPool:
    """Provider inventory with clock-driven capacity transitions.

    ``has_available_providers`` is a read-only capacity query.  ``reprobe_due``
    is the explicit non-generative readiness operation acceptance tests use at
    retry boundaries.  Neither method consumes hosted inference quota.
    """

    def __init__(
        self,
        clock: FakeClock,
        records: Iterable[ProviderRecord],
        transitions: Iterable[ProviderTransition] = (),
    ):
        self.clock = clock
        self.records = {record.provider_id: record for record in records}
        self.transitions = sorted(
            list(transitions), key=lambda transition: transition.at
        )
        self.transition_history: List[ProviderTransition] = []
        clock.add_listener(self._apply_due)
        self._apply_due(clock.now())

    def _apply_due(self, now: datetime) -> None:
        while self.transitions and self.transitions[0].at <= now:
            transition = self.transitions.pop(0)
            record = self.records[transition.provider_id]
            record.capacity = transition.capacity
            record.retry_after = transition.retry_after
            self.transition_history.append(transition)

    def has_available_providers(self) -> bool:
        self._apply_due(self.clock.now())
        return any(
            record.capacity in AVAILABLE_CAPACITIES
            for record in self.records.values()
        )

    def inventory(self) -> List[Dict[str, Any]]:
        self._apply_due(self.clock.now())
        return [
            {
                "identity": {"resource_id": record.provider_id},
                "locality": record.locality,
                "capacity": record.capacity,
                "retry_after": (
                    record.retry_after.isoformat()
                    if record.retry_after is not None
                    else None
                ),
            }
            for record in sorted(
                self.records.values(), key=lambda value: value.provider_id
            )
        ]

    def reprobe_due(self) -> List[str]:
        """Recheck only providers whose cooldown has expired."""
        checked: List[str] = []
        now = self.clock.now()
        for record in self.records.values():
            if record.retry_after is None or record.retry_after > now:
                continue
            record.readiness_checks += 1
            checked.append(record.provider_id)
        return sorted(checked)

    def record_generative_attempt(self, provider_id: str) -> None:
        record = self.records[provider_id]
        if record.capacity not in AVAILABLE_CAPACITIES:
            raise AssertionError(
                f"generative attempt sent to unavailable provider: {provider_id}"
            )
        record.generative_attempts += 1
        record.generative_attempt_times.append(self.clock.now())

    def choose_available_provider(self) -> str:
        """Choose a deterministic available provider for a fake governed task."""
        self._apply_due(self.clock.now())
        available = [
            record
            for record in self.records.values()
            if record.capacity in AVAILABLE_CAPACITIES
        ]
        if not available:
            raise AssertionError("governed task dispatched with no eligible provider")
        available.sort(
            key=lambda record: (record.locality == "local", record.provider_id)
        )
        return available[0].provider_id


class CrashInjector:
    """One-shot named crash injector used at durable boundaries."""

    def __init__(self, *points: str):
        self._armed: Set[str] = set(points)
        self.seen: List[str] = []

    def reach(self, point: str) -> None:
        self.seen.append(point)
        if point in self._armed:
            self._armed.remove(point)
            raise InjectedCrash(point)


class FakeAuthority:
    """Deterministic repository and action authority boundary."""

    def __init__(
        self,
        authorized_repositories: Iterable[str] = (),
        allowed_actions: Iterable[str] = (),
        never_delegatable: Iterable[str] = (),
    ):
        self.authorized_repositories = set(authorized_repositories)
        self.allowed_actions = set(allowed_actions)
        self.never_delegatable = set(never_delegatable)
        self.decisions: List[Dict[str, str]] = []

    def evaluate(self, repository: str, action: str) -> str:
        if action in self.never_delegatable:
            decision = "PARK_HUMAN"
        elif repository not in self.authorized_repositories:
            decision = "PARK_HUMAN"
        elif action not in self.allowed_actions:
            decision = "PARK_HUMAN"
        else:
            decision = "ALLOW"
        self.decisions.append(
            {"repository": repository, "action": action, "decision": decision}
        )
        return decision


@dataclass
class PullRequest:
    number: int
    branch: str
    commit_sha: str
    state: str = "OPEN"
    checks: Dict[str, str] = field(default_factory=dict)


class FakeRepository:
    """Observable Git and pull-request truth with idempotency by stable key."""

    def __init__(self, name: str, injector: Optional[CrashInjector] = None):
        self.name = name
        self.injector = injector or CrashInjector()
        self.branches: Dict[str, str] = {}
        self.pull_requests: Dict[int, PullRequest] = {}
        self.pr_by_branch: Dict[str, int] = {}
        self.merged_shas: Set[str] = set()
        self.events: List[Dict[str, Any]] = []

    def create_branch(self, branch: str, commit_sha: str) -> str:
        existing = self.branches.get(branch)
        if existing is not None and existing != commit_sha:
            raise ValueError("branch idempotency key reused for another commit")
        self.branches[branch] = commit_sha
        self.events.append({"action": "branch", "branch": branch})
        self.injector.reach("after_branch_created")
        return branch

    def create_pull_request(self, branch: str) -> PullRequest:
        existing_number = self.pr_by_branch.get(branch)
        if existing_number is not None:
            return self.pull_requests[existing_number]
        if branch not in self.branches:
            raise ValueError("pull request branch does not exist")
        number = len(self.pull_requests) + 1
        pull_request = PullRequest(number, branch, self.branches[branch])
        self.pull_requests[number] = pull_request
        self.pr_by_branch[branch] = number
        self.events.append({"action": "pull_request", "number": number})
        self.injector.reach("after_pull_request_accepted")
        return pull_request

    def observe_pull_request(self, branch: str) -> Optional[PullRequest]:
        number = self.pr_by_branch.get(branch)
        return self.pull_requests.get(number) if number is not None else None

    def set_checks(self, number: int, checks: Dict[str, str]) -> None:
        self.pull_requests[number].checks = dict(checks)

    def merge(self, number: int, required_checks: Iterable[str]) -> str:
        pull_request = self.pull_requests[number]
        required = list(required_checks)
        if not required or any(
            pull_request.checks.get(check) != "SUCCESS" for check in required
        ):
            raise PermissionError("required checks are not terminal green")
        pull_request.state = "MERGED"
        merge_sha = f"merge-{pull_request.commit_sha}"
        self.merged_shas.add(merge_sha)
        self.events.append({"action": "merge", "number": number})
        self.injector.reach("after_merge_accepted")
        return merge_sha


@dataclass
class DispatchScript:
    """One scripted governed-task outcome, not a scheduling decision."""

    success: bool
    next_state: str
    reason: str
    provider_id: Optional[str] = None
    git_record: Optional[Dict[str, Any]] = None
    next_work_item_state: Optional[str] = None
    requires_authority: bool = False
    provider_unavailable: bool = False
    blocker: Optional[str] = None


class ScriptedDispatcher:
    """Records dispatches and returns per-work-item governed outcomes."""

    def __init__(
        self,
        scripts: Dict[str, List[DispatchScript]],
        provider_pool: Optional[ScriptedProviderPool] = None,
        injector: Optional[CrashInjector] = None,
    ):
        self.scripts = {key: list(value) for key, value in scripts.items()}
        self.provider_pool = provider_pool
        self.injector = injector or CrashInjector()
        self.calls: List[str] = []

    def dispatch(self, item: Any, **identifiers: Any) -> Any:
        self.calls.append(item.work_item_id)
        self.injector.reach("after_dispatch_begins")
        try:
            script = self.scripts[item.work_item_id].pop(0)
        except (KeyError, IndexError) as exc:
            raise AssertionError(
                f"unexpected or duplicate dispatch: {item.work_item_id}"
            ) from exc
        provider_id = script.provider_id
        if provider_id == "auto" and self.provider_pool is not None:
            provider_id = self.provider_pool.choose_available_provider()
        if provider_id and self.provider_pool is not None:
            self.provider_pool.record_generative_attempt(provider_id)

        class Outcome:
            pass

        outcome = Outcome()
        outcome.success = script.success
        outcome.work_item_id = item.work_item_id
        outcome.next_state = script.next_state
        outcome.next_work_item_state = script.next_work_item_state or (
            "shipped" if script.success else "failed"
        )
        outcome.reason = script.reason
        outcome.git_record = script.git_record
        outcome.provider_id = provider_id
        outcome.requires_authority = script.requires_authority
        outcome.provider_unavailable = script.provider_unavailable
        outcome.blocker = script.blocker
        outcome.task_id = identifiers.get("task_id")
        outcome.dispatch_id = identifiers.get("dispatch_id")
        return outcome


def assert_bounded_waits(clock: FakeClock, minimum_seconds: float = 1.0) -> None:
    """Fail if a run loop advanced through zero or subsecond hot-loop waits."""
    assert clock.sleep_calls, "factory never yielded to its injected wait"
    assert min(clock.sleep_calls) >= minimum_seconds


def assert_no_duplicate_dispatch(dispatcher: ScriptedDispatcher) -> None:
    """Fail when the same durable work item was dispatched more than once."""
    assert len(dispatcher.calls) == len(set(dispatcher.calls))

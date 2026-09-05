#!/usr/bin/env python3
"""Shared helpers for factory supervisor tests to avoid duplication across files."""

from datetime import datetime, timedelta, timezone

from src.control_plane.factory.dispatcher import MarathonDispatcherAdapter
from src.control_plane.factory.portfolio import FactoryPolicy
from src.control_plane.factory.repo_proposal import CapabilityStore, RepoProposalStore
from src.control_plane.factory.supervisor import FactorySupervisor
from src.control_plane.factory.supervisor_state import SupervisorStateStore
from src.control_plane.factory.work_item import WorkItem, WorkItemOrigin, WorkItemState, WorkItemStore


START = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)


class FakeProviderPool:
    def __init__(self, has_capacity=True, retry_after=None):
        self.has_capacity = has_capacity
        self.retry_after = retry_after
        self.reprobed = False

    def has_available_providers(self):
        return self.has_capacity

    def inventory(self):
        if self.retry_after:
            return [{"identity": {"resource_id": "codex"}, "retry_after": self.retry_after}]
        return []

    def reset_transient_exhaustion(self):
        self.reprobed = True


class SuccessEngine:
    def execute_factory_work_item(self, item, files_changed=None, dispatch_id=None):
        return True, {"provider": "fake"}


class RecordingDispatcher:
    """Return configured outcomes in order and record dispatched work items."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def dispatch(self, item, dispatch_id, task_id):
        outcome = self.outcomes.pop(0)
        outcome.dispatch_id = dispatch_id
        outcome.task_id = task_id
        self.calls.append(item.work_item_id)
        return outcome


def make_supervisor(
    tmp_path,
    *,
    discovery=None,
    dispatcher=None,
    pool=None,
    clock_start=START,
    sleep=None,
    state_dir=None,
):
    state_dir = state_dir or tmp_path
    state_store = SupervisorStateStore(state_dir / "supervisor")
    work_store = WorkItemStore(state_dir / "work_items")
    repo_store = RepoProposalStore(state_dir / "repo_proposals")
    cap_store = CapabilityStore(state_dir / "capabilities")
    now = {"t": clock_start}
    sleeps = []

    def clock():
        return now["t"]

    def sleep_fn(seconds):
        sleeps.append(seconds)
        now["t"] += timedelta(seconds=seconds)

    supervisor = FactorySupervisor(
        state_store=state_store,
        work_item_store=work_store,
        repo_proposal_store=repo_store,
        capability_store=cap_store,
        dispatcher=dispatcher or MarathonDispatcherAdapter(lambda: SuccessEngine()),
        discovery=discovery or (lambda: []),
        provider_pool=pool or FakeProviderPool(),
        policy=FactoryPolicy(),
        clock=clock,
        sleep=sleep or sleep_fn,
        tick_interval_seconds=5.0,
        provider_retry_interval_seconds=30.0,
        backoff_base_seconds=1.0,
        max_backoff_seconds=30.0,
        state_dir=state_dir,
    )
    return supervisor, now, sleeps


def ready_pair(store, *, first_title):
    first = ready_work_item(
        store, title=first_title, identity_keys=[first_title], source_rank=1
    )
    second = ready_work_item(
        store, title="ready", identity_keys=["ready"], source_rank=2
    )
    return first, second


def unavailable_supervisor(tmp_path, *, wait_seconds):
    retry_after = (START + timedelta(seconds=wait_seconds)).isoformat()
    supervisor, now, sleeps = make_supervisor(
        tmp_path,
        pool=FakeProviderPool(has_capacity=False, retry_after=retry_after),
    )
    return supervisor, now, sleeps, retry_after


def ready_work_item(
    store,
    *,
    origin=WorkItemOrigin.EXISTING_BACKLOG,
    repository="howlcipher/howlplane",
    title="work",
    identity_keys=None,
    key=None,
    source_rank=0,
):
    item = WorkItem.create(
        origin=origin,
        repository=repository,
        title=title,
        identity_keys=identity_keys or [key or "key"],
        source_rank=source_rank,
    )
    item.transition_to(WorkItemState.ADMITTED)
    item.transition_to(WorkItemState.READY)
    store.save_object(item)
    return item

#!/usr/bin/env python3
"""
tests/test_factory_portfolio.py

Deterministic tests for portfolio selection and the anti-starvation policy.

`select` is a pure function, so every one of these runs without a clock, a
provider, a repository, or a filesystem. That is the point of keeping the
policy pure: the alternative is asserting on portfolio balance by observing a
long unattended run, which is neither fast nor repeatable.

The most important test in this file is the negative one -- that a portfolio
containing only self-improvement work, with the cap already met, selects
nothing at all. A factory that always finds something to do is the failure
mode, not the goal.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.control_plane.factory.portfolio import (
    FactoryPolicy,
    INTROSPECTIVE_ORIGINS,
    ORIGIN_PRIORITY,
    select,
)
from src.control_plane.factory.work_item import (
    WorkItem,
    WorkItemOrigin,
    WorkItemState,
)

NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=timezone.utc)
PRODUCT = "howlcipher/howlframe"
SELF = "howlcipher/howlplane"


def ready(origin, repository=PRODUCT, *, rank=0, file_rank=0, age_hours=0.0, keys=None):
    """A dispatchable work item, aged relative to the fixed NOW."""
    item = WorkItem.create(
        origin=origin,
        repository=repository,
        title=f"{origin} #{rank}",
        identity_keys=keys or [repository, str(origin), str(file_rank), str(rank)],
        source_rank=rank,
        source_file_rank=file_rank,
    )
    item.created_at = (NOW - timedelta(hours=age_hours)).isoformat()
    item.transition_to(WorkItemState.ADMITTED)
    item.transition_to(WorkItemState.READY)
    return item


def window(*entries):
    """Recent dispatch history, most recent last."""
    return [{"repository": repo, "origin": origin} for repo, origin in entries]


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def test_owner_direction_preempts_a_higher_ranked_backlog_row():
    backlog = ready(WorkItemOrigin.EXISTING_BACKLOG, rank=0)
    owner = ready(WorkItemOrigin.OWNER_DIRECTION, rank=99)

    chosen = select([backlog, owner], [], FactoryPolicy(), now=NOW).item
    assert chosen.origin == WorkItemOrigin.OWNER_DIRECTION


def test_origin_priority_is_total_and_has_no_ties():
    assert len(set(ORIGIN_PRIORITY.values())) == len(ORIGIN_PRIORITY)
    assert set(ORIGIN_PRIORITY) == set(WorkItemOrigin)


def test_committed_backlog_order_is_preserved_and_not_resorted_by_score():
    """BacklogSource treats file order as human judgement; so must this."""
    first = ready(WorkItemOrigin.EXISTING_BACKLOG, rank=0)
    first.score = 0.6
    second = ready(WorkItemOrigin.EXISTING_BACKLOG, rank=1)
    second.score = 8.0

    chosen = select([second, first], [], FactoryPolicy(), now=NOW).item
    assert chosen.work_item_id == first.work_item_id


def test_bugs_file_outranks_improvements_file():
    improvement = ready(WorkItemOrigin.EXISTING_BACKLOG, file_rank=1, rank=0)
    bug = ready(WorkItemOrigin.EXISTING_BACKLOG, file_rank=0, rank=5)

    chosen = select([improvement, bug], [], FactoryPolicy(), now=NOW).item
    assert chosen.work_item_id == bug.work_item_id


def test_a_starving_item_jumps_ahead_of_a_fresher_one_of_the_same_origin():
    fresh = ready(WorkItemOrigin.EXISTING_BACKLOG, rank=0, age_hours=1)
    starving = ready(WorkItemOrigin.EXISTING_BACKLOG, rank=9, age_hours=100)

    chosen = select([fresh, starving], [], FactoryPolicy(), now=NOW).item
    assert chosen.work_item_id == starving.work_item_id


def test_starvation_does_not_override_owner_direction():
    starving = ready(WorkItemOrigin.SELF_IMPROVEMENT, repository=SELF, age_hours=500)
    owner = ready(WorkItemOrigin.OWNER_DIRECTION, age_hours=0)

    chosen = select([starving, owner], [], FactoryPolicy(), now=NOW).item
    assert chosen.origin == WorkItemOrigin.OWNER_DIRECTION


# ---------------------------------------------------------------------------
# Caps and anti-starvation
# ---------------------------------------------------------------------------

def test_self_improvement_is_capped_within_the_window():
    policy = FactoryPolicy()
    recent = window(*[(SELF, "self_improvement")] * policy.max_introspective_in_window)
    item = ready(WorkItemOrigin.SELF_IMPROVEMENT, repository=SELF)

    outcome = select([item], recent, policy, now=NOW)
    assert outcome.item is None
    assert outcome.withheld[0]["reason"] == "self_improvement_cap"


def test_the_factory_idles_rather_than_exceeding_its_self_improvement_cap():
    """The headline negative: only self-work left, cap met, so nothing runs."""
    policy = FactoryPolicy()
    recent = window(*[(SELF, "maintenance")] * policy.max_introspective_in_window)
    only_self_work = [
        ready(WorkItemOrigin.SELF_IMPROVEMENT, repository=SELF, rank=i)
        for i in range(5)
    ]

    outcome = select(only_self_work, recent, policy, now=NOW)

    assert outcome.item is None
    assert outcome.reason == "all_candidates_capped"
    assert len(outcome.withheld) == 5


def test_maintenance_counts_against_the_same_budget_as_self_improvement():
    assert WorkItemOrigin.MAINTENANCE in INTROSPECTIVE_ORIGINS
    assert WorkItemOrigin.SELF_IMPROVEMENT in INTROSPECTIVE_ORIGINS

    policy = FactoryPolicy()
    recent = window(*[(SELF, "self_improvement")] * policy.max_introspective_in_window)
    outcome = select(
        [ready(WorkItemOrigin.MAINTENANCE, repository=SELF)], recent, policy, now=NOW
    )
    assert outcome.item is None


def test_non_product_repository_is_capped_so_howlframe_keeps_its_share():
    policy = FactoryPolicy()
    recent = window(*[(SELF, "existing_backlog")] * policy.max_non_product_in_window)
    item = ready(WorkItemOrigin.EXISTING_BACKLOG, repository=SELF)

    outcome = select([item], recent, policy, now=NOW)
    assert outcome.item is None
    assert outcome.withheld[0]["reason"] == "non_product_repository_cap"


def test_product_repository_work_is_never_capped_by_the_repository_rule():
    policy = FactoryPolicy()
    recent = window(*[(SELF, "existing_backlog")] * policy.max_non_product_in_window)
    item = ready(WorkItemOrigin.EXISTING_BACKLOG, repository=PRODUCT)

    assert select([item], recent, policy, now=NOW).item is not None


def test_owner_direction_ignores_every_cap():
    policy = FactoryPolicy()
    recent = window(
        *([(SELF, "self_improvement")] * policy.max_introspective_in_window),
        *([(SELF, "existing_backlog")] * policy.max_non_product_in_window),
    )
    owner = ready(WorkItemOrigin.OWNER_DIRECTION, repository=SELF)

    assert select([owner], recent, policy, now=NOW).item is owner


def test_a_capped_item_yields_to_an_uncapped_one_rather_than_blocking_it():
    policy = FactoryPolicy()
    recent = window(*[(SELF, "self_improvement")] * policy.max_introspective_in_window)
    capped = ready(WorkItemOrigin.SELF_IMPROVEMENT, repository=SELF)
    open_work = ready(WorkItemOrigin.EXISTING_BACKLOG, repository=PRODUCT)

    outcome = select([capped, open_work], recent, policy, now=NOW)

    assert outcome.item.work_item_id == open_work.work_item_id
    assert [w["reason"] for w in outcome.withheld] == ["self_improvement_cap"]


def test_creative_experiments_have_their_own_cap():
    policy = FactoryPolicy()
    recent = window(*[(PRODUCT, "creative_experiment")] * policy.max_creative_in_window)
    item = ready(WorkItemOrigin.CREATIVE_EXPERIMENT)

    outcome = select([item], recent, policy, now=NOW)
    assert outcome.withheld[0]["reason"] == "creative_experiment_cap"


def test_only_the_most_recent_window_entries_count():
    """An old burst of self-work must not cap the factory forever."""
    policy = FactoryPolicy(portfolio_window=10, max_introspective_in_window=3)
    recent = window(
        *([(SELF, "self_improvement")] * 5),
        *([(PRODUCT, "existing_backlog")] * 10),
    )
    item = ready(WorkItemOrigin.SELF_IMPROVEMENT, repository=SELF)

    assert select([item], recent, policy, now=NOW).item is not None


# ---------------------------------------------------------------------------
# Dispatchability
# ---------------------------------------------------------------------------

def test_nothing_dispatchable_reports_the_reason_rather_than_returning_silently():
    outcome = select([], [], FactoryPolicy(), now=NOW)
    assert outcome.item is None
    assert outcome.reason == "no_dispatchable_work"


@pytest.mark.parametrize(
    "state",
    [
        WorkItemState.PROPOSED,
        WorkItemState.ADMITTED,
        WorkItemState.AWAITING_OWNER,
        WorkItemState.BLOCKED,
    ],
)
def test_only_ready_items_are_ever_selected(state):
    item = WorkItem.create(
        origin=WorkItemOrigin.EXISTING_BACKLOG,
        repository=PRODUCT,
        title="t",
        identity_keys=["k"],
    )
    if state != WorkItemState.PROPOSED:
        item.transition_to(WorkItemState.ADMITTED)
    if state in (WorkItemState.AWAITING_OWNER, WorkItemState.BLOCKED):
        item.transition_to(state)

    assert select([item], [], FactoryPolicy(), now=NOW).item is None


def test_an_item_awaiting_a_blocker_is_not_selected():
    item = ready(WorkItemOrigin.EXISTING_BACKLOG)
    item.blocked_by = ["WI-howlframe-0000000000000000"]

    assert select([item], [], FactoryPolicy(), now=NOW).item is None

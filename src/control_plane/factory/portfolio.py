#!/usr/bin/env python3
"""
portfolio.py

Chooses the next piece of work, and refuses to choose one when every remaining
candidate would over-serve a category.

This is a pure function over a list of `WorkItem`s and a dispatch window. No
I/O, no clock of its own, no provider calls -- which is what makes the
anti-starvation policy exhaustively testable rather than something that has to
be observed over a long run.

The policy is deliberately legible rather than optimal. A weighted round-robin
over the last N dispatches can be explained to the owner in one sentence and
audited from `factory status`; a learned ranker cannot, and section 18 of the
north star explicitly warns against pretending a score is more precise than the
evidence behind it.

The one rule that matters most is negative: when the only remaining work is
self-improvement and the cap is already met, this returns `None`. The factory
then idles. That is the behaviour that stops it spending a hundred consecutive
cycles refactoring itself while real product work waits.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from src.control_plane.factory.work_item import WorkItem, WorkItemOrigin

# Lower sorts first. Owner direction always wins; a bug outranks a feature of
# similar standing; the factory's opinions about itself come last.
ORIGIN_PRIORITY: Dict[str, int] = {
    WorkItemOrigin.OWNER_DIRECTION: 0,
    WorkItemOrigin.EXISTING_BACKLOG: 1,
    WorkItemOrigin.DISCOVERED_PROBLEM: 2,
    WorkItemOrigin.INFERRED_NEED: 3,
    WorkItemOrigin.MAINTENANCE: 4,
    WorkItemOrigin.SELF_IMPROVEMENT: 5,
    WorkItemOrigin.CREATIVE_EXPERIMENT: 6,
}

# Origins that count against the "do not grind on yourself" budget.
INTROSPECTIVE_ORIGINS = frozenset(
    {WorkItemOrigin.SELF_IMPROVEMENT, WorkItemOrigin.MAINTENANCE}
)


@dataclass(frozen=True)
class FactoryPolicy:
    """Portfolio shares. Owner-tunable, defaulted to HowlFrame-weighted."""

    product_repository: str = "howlcipher/howlframe"
    portfolio_window: int = 10
    # At most this many of the last `portfolio_window` dispatches may be
    # against a repository other than the product one.
    max_non_product_in_window: int = 4
    # At most this many may be self-improvement or maintenance.
    max_introspective_in_window: int = 3
    # Creative experiments never auto-admit, so this is a second belt.
    max_creative_in_window: int = 1
    # A ready item older than this jumps its repository's queue.
    starvation_age_hours: float = 72.0

    def counts(self, window: Sequence[Dict[str, str]]) -> Dict[str, int]:
        recent = list(window)[-self.portfolio_window:]
        return {
            "non_product": sum(
                1 for d in recent if d.get("repository") != self.product_repository
            ),
            "introspective": sum(
                1 for d in recent if d.get("origin") in INTROSPECTIVE_ORIGINS
            ),
            "creative": sum(
                1
                for d in recent
                if d.get("origin") == WorkItemOrigin.CREATIVE_EXPERIMENT
            ),
        }


@dataclass
class SelectionOutcome:
    """What was chosen, and -- just as importantly -- what was held back."""

    item: Optional[WorkItem] = None
    reason: str = "no_dispatchable_work"
    withheld: List[Dict[str, str]] = field(default_factory=list)


def _age_hours(item: WorkItem, now: datetime) -> float:
    try:
        created = datetime.fromisoformat(item.created_at)
    except (TypeError, ValueError):
        return 0.0
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (now - created).total_seconds() / 3600.0)


def _sort_key(item: WorkItem, now: datetime, policy: FactoryPolicy) -> Tuple:
    """Origin first, then starvation, then the backlog's committed order.

    Score is deliberately absent. `BacklogSource` records that a backlog's own
    ordering encodes judgement the code cannot reconstruct, and re-sorting by
    the parsed score here would silently overrule the human who ranked it.
    """
    starving = _age_hours(item, now) >= policy.starvation_age_hours
    return (
        ORIGIN_PRIORITY.get(item.origin, 99),
        0 if starving else 1,
        item.source_file_rank,
        item.source_rank,
        item.work_item_id,
    )


def _cap_blocking(
    item: WorkItem, counts: Dict[str, int], policy: FactoryPolicy
) -> Optional[str]:
    """The cap this item would breach, or None if it may be dispatched."""
    if item.origin == WorkItemOrigin.OWNER_DIRECTION:
        # Owner direction preempts the window. The owner setting a priority is
        # not the factory over-serving a category.
        return None
    if (
        item.origin == WorkItemOrigin.CREATIVE_EXPERIMENT
        and counts["creative"] >= policy.max_creative_in_window
    ):
        return "creative_experiment_cap"
    if (
        item.origin in INTROSPECTIVE_ORIGINS
        and counts["introspective"] >= policy.max_introspective_in_window
    ):
        return "self_improvement_cap"
    if (
        item.repository != policy.product_repository
        and counts["non_product"] >= policy.max_non_product_in_window
    ):
        return "non_product_repository_cap"
    return None


def select(
    work_items: Sequence[WorkItem],
    window: Sequence[Dict[str, str]],
    policy: FactoryPolicy,
    now: Optional[datetime] = None,
) -> SelectionOutcome:
    """Pick the next dispatchable item, or explain why nothing was picked.

    `window` is the recent dispatch history, most recent last, each entry
    carrying at least `repository` and `origin`.
    """
    now = now or datetime.now(timezone.utc)
    counts = policy.counts(window)

    candidates = sorted(
        (item for item in work_items if item.is_dispatchable),
        key=lambda i: _sort_key(i, now, policy),
    )
    if not candidates:
        return SelectionOutcome(reason="no_dispatchable_work")

    # Every candidate is evaluated, not just those ahead of the winner. The
    # owner asking "why is my self-improvement work not running" needs the
    # answer even when something else was dispatched this tick, so `withheld`
    # is the complete set of capped candidates rather than a prefix of it.
    chosen: Optional[WorkItem] = None
    withheld: List[Dict[str, str]] = []
    for item in candidates:
        breach = _cap_blocking(item, counts, policy)
        if breach is None:
            if chosen is None:
                chosen = item
            continue
        withheld.append(
            {
                "work_item_id": item.work_item_id,
                "origin": item.origin,
                "repository": item.repository,
                "reason": breach,
            }
        )

    if chosen is not None:
        return SelectionOutcome(item=chosen, reason="selected", withheld=withheld)

    # Everything ready would over-serve a category. Idling here is the point:
    # the alternative is grinding on whatever is left regardless of balance.
    return SelectionOutcome(reason="all_candidates_capped", withheld=withheld)

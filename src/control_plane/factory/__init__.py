#!/usr/bin/env python3
"""
factory

The persistent supervisory layer above individual governed tasks.

Authorized by the named carve-out in `documentation/CONTROL_PLANE.md` section
1.1.1 and specified in `documentation/adr/0006_persistent_factory_supervisor.md`.

This package decides *what is worth attempting*. It never decides *what is
permitted*: `AuthorityProfile`, `AuthorityEnvelope` and `HumanBoundaryGate`
remain the only authority paths, and admitting an item to the portfolio grants
it nothing.
"""

from src.control_plane.factory.portfolio import (
    FactoryPolicy,
    ORIGIN_PRIORITY,
    SelectionOutcome,
    select,
)
from src.control_plane.factory.work_item import (
    InvalidWorkItemTransitionError,
    WORK_ITEM_SCHEMA_VERSION,
    WORK_ITEM_TRANSITIONS,
    WorkItem,
    WorkItemOrigin,
    WorkItemState,
    WorkItemStore,
    work_item_fingerprint,
)

__all__ = [
    "FactoryPolicy",
    "InvalidWorkItemTransitionError",
    "ORIGIN_PRIORITY",
    "SelectionOutcome",
    "WORK_ITEM_SCHEMA_VERSION",
    "WORK_ITEM_TRANSITIONS",
    "WorkItem",
    "WorkItemOrigin",
    "WorkItemState",
    "WorkItemStore",
    "select",
    "work_item_fingerprint",
]

#!/usr/bin/env python3
"""
work_item.py

The factory's portfolio entity.

A `WorkItem` is not a `TaskSpec` and the two are deliberately not merged. A
`TaskSpec` describes one governed execution -- one branch, one implementation,
one review cycle -- and its states answer "where is this run". A `WorkItem`
describes a piece of work the factory believes is worth doing, and its states
answer "where does this stand in the portfolio". Conflating them would make
"this item is waiting on another item" indistinguishable from "this task run is
blocked", which is exactly the distinction the blocker handling depends on.

One `WorkItem` spawns zero or more `TaskSpec`s over its life.

The field that does not exist anywhere else in the control plane is `origin`.
Without it, owner direction, an item read out of a ranked backlog, something the
factory discovered on its own, and the factory proposing work on itself are all
the same shape -- and no portfolio policy can tell them apart or stop the system
spending every cycle on itself.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from src.control_plane.durable_store import DurableObjectStore
from src.control_plane.reasoning.artifact_safety import SafeArtifactSerializationMixin

WORK_ITEM_SCHEMA_VERSION = "howlplane.work_item/v1"

_REPO_TAG = re.compile(r"[^A-Za-z0-9]+")


class WorkItemOrigin(str, Enum):
    """Where the work came from. Recorded, never inferred after the fact."""

    OWNER_DIRECTION = "owner_direction"
    EXISTING_BACKLOG = "existing_backlog"
    DISCOVERED_PROBLEM = "discovered_problem"
    INFERRED_NEED = "inferred_need"
    SELF_IMPROVEMENT = "self_improvement"
    MAINTENANCE = "maintenance"
    CREATIVE_EXPERIMENT = "creative_experiment"


class WorkItemState(str, Enum):
    """Portfolio-level lifecycle. Distinct from `TaskSpec`'s run-level states."""

    PROPOSED = "proposed"
    ADMITTED = "admitted"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    AWAITING_OWNER = "awaiting_owner"
    DEFERRED = "deferred"
    SHIPPED = "shipped"
    FAILED = "failed"
    REJECTED = "rejected"
    ABANDONED = "abandoned"


TERMINAL_STATES: Set[str] = {
    WorkItemState.SHIPPED,
    WorkItemState.REJECTED,
    WorkItemState.ABANDONED,
}

# Explicit adjacency, enforced on every transition, mirroring the pattern
# `task_spec.STATE_TRANSITIONS` established. An illegal transition is a bug in
# the supervisor, not a state to tolerate.
WORK_ITEM_TRANSITIONS: Dict[str, Set[str]] = {
    WorkItemState.PROPOSED: {
        WorkItemState.ADMITTED, WorkItemState.AWAITING_OWNER, WorkItemState.REJECTED,
    },
    WorkItemState.ADMITTED: {
        WorkItemState.READY, WorkItemState.BLOCKED, WorkItemState.AWAITING_OWNER,
        WorkItemState.DEFERRED, WorkItemState.REJECTED,
    },
    WorkItemState.READY: {
        WorkItemState.IN_PROGRESS, WorkItemState.BLOCKED,
        WorkItemState.AWAITING_OWNER, WorkItemState.DEFERRED, WorkItemState.ABANDONED,
    },
    WorkItemState.IN_PROGRESS: {
        WorkItemState.VERIFYING, WorkItemState.BLOCKED,
        WorkItemState.AWAITING_OWNER, WorkItemState.FAILED, WorkItemState.SHIPPED,
        WorkItemState.DEFERRED,
    },
    WorkItemState.VERIFYING: {
        WorkItemState.SHIPPED, WorkItemState.FAILED, WorkItemState.AWAITING_OWNER,
    },
    WorkItemState.BLOCKED: {
        WorkItemState.READY, WorkItemState.AWAITING_OWNER, WorkItemState.ABANDONED,
    },
    WorkItemState.AWAITING_OWNER: {
        WorkItemState.READY, WorkItemState.REJECTED, WorkItemState.ABANDONED,
    },
    WorkItemState.DEFERRED: {WorkItemState.READY, WorkItemState.ABANDONED},
    WorkItemState.FAILED: {
        WorkItemState.READY, WorkItemState.ABANDONED, WorkItemState.AWAITING_OWNER,
    },
    WorkItemState.SHIPPED: set(),
    WorkItemState.REJECTED: set(),
    WorkItemState.ABANDONED: set(),
}


class InvalidWorkItemTransitionError(ValueError):
    """Raised when an illegal portfolio state transition is attempted."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plain(value: Any) -> str:
    """The plain string behind a `(str, Enum)` member, or the value itself."""
    return str(getattr(value, "value", value))


def repository_tag(repository: str) -> str:
    """Short filename-safe tag for a repository slug (`owner/name` -> `name`)."""
    tail = (repository or "unknown").rsplit("/", 1)[-1]
    tag = _REPO_TAG.sub("-", tail).strip("-")
    return tag or "unknown"


def work_item_fingerprint(origin: str, repository: str, keys: List[str]) -> str:
    """Stable identity for a piece of work, independent of when it was seen.

    The repository is part of the identity because backlog item ids are only
    unique within a file. `BacklogItem.task_id` hardcodes a `HOWLFRAM-` prefix
    regardless of which repository the row came from, so two repositories can
    and do produce the same task id -- the fingerprint must not inherit that
    collision.
    """
    canonical = json.dumps(
        {"origin": str(origin), "repository": repository, "keys": sorted(keys)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass
class WorkItem(SafeArtifactSerializationMixin):
    """One piece of work the factory is tracking, with its provenance."""

    work_item_id: str
    fingerprint: str
    origin: str
    repository: str
    title: str
    description: str = ""
    kind: str = "improvement"
    state: str = WorkItemState.PROPOSED

    score: Optional[float] = None
    # Committed ordering from the source backlog. The backlog's own order
    # encodes human judgement this code cannot reconstruct, so it is preserved
    # rather than recomputed from score.
    source_file_rank: int = 0
    source_rank: int = 0
    source_ref: Dict[str, Any] = field(default_factory=dict)

    evidence_refs: List[str] = field(default_factory=list)
    evidence_fingerprints: List[str] = field(default_factory=list)
    occurrence_count: int = 1
    admission_blocked_reason: Optional[str] = None

    blocked_by: List[str] = field(default_factory=list)
    blocker_class: Optional[str] = None
    resolution_depth: int = 0
    parent_work_item_id: Optional[str] = None

    touches_self_modification_paths: bool = False
    task_ids: List[str] = field(default_factory=list)
    campaign_id: Optional[str] = None
    attempts: int = 0
    retry_after: Optional[str] = None

    reopening_history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    schema_version: str = WORK_ITEM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Store origin and state as plain strings.

        `WorkItemOrigin`/`WorkItemState` are `(str, Enum)`, so a member compares
        equal to its value but renders as `WorkItemOrigin.MAINTENANCE`. A freshly
        constructed item would then format differently from the same item loaded
        back off disk, where it is a plain string. Normalizing once here keeps
        callers free to pass either and keeps every downstream rendering
        identical.
        """
        self.origin = _plain(self.origin)
        self.state = _plain(self.state)
        if self.state not in WORK_ITEM_TRANSITIONS:
            raise ValueError(f"Unknown work item state: {self.state!r}")
        if self.origin not in {o.value for o in WorkItemOrigin}:
            raise ValueError(f"Unknown work item origin: {self.origin!r}")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkItem":
        d = dict(data)
        d.pop("schema_version", None)
        valid = {f for f in cls.__dataclass_fields__ if f != "schema_version"}
        return cls(**{k: v for k, v in d.items() if k in valid})

    @classmethod
    def create(
        cls,
        origin: str,
        repository: str,
        title: str,
        identity_keys: List[str],
        **kwargs: Any,
    ) -> "WorkItem":
        """Build an item with a fingerprint-derived, collision-safe id."""
        fingerprint = work_item_fingerprint(origin, repository, identity_keys)
        return cls(
            work_item_id=f"WI-{repository_tag(repository)}-{fingerprint}",
            fingerprint=fingerprint,
            origin=origin,
            repository=repository,
            title=title,
            **kwargs,
        )

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_dispatchable(self) -> bool:
        return self.state == WorkItemState.READY and not self.blocked_by

    def transition_to(self, new_state: str, reason: str = "") -> None:
        """Move to `new_state`, refusing any transition not on the map."""
        target = _plain(new_state)
        allowed = WORK_ITEM_TRANSITIONS.get(self.state, set())
        if target not in allowed:
            # Rendered with plain values, not enum reprs: the supervisor logs
            # this from inside a loop where the message is the only context.
            legal = sorted(_plain(state) for state in allowed)
            raise InvalidWorkItemTransitionError(
                f"{self.work_item_id}: cannot move from '{self.state}' to "
                f"'{target}'. Allowed: {legal or 'none (terminal state)'}."
            )
        self.state = target
        self.updated_at = _now()
        self.reopening_history.append(
            {"at": self.updated_at, "to_state": _plain(new_state), "reason": reason}
        )

    def reopen(
        self,
        new_evidence_refs: List[str],
        new_evidence_fingerprints: List[str],
        reason: str,
    ) -> None:
        """Reopen a disposed item, but only on materially new evidence.

        Same contract as `TrajectoryObservation.reopen`: new *fingerprints*,
        not merely new references. Re-seeing the same evidence under a new id
        is not a reason to re-propose what was already rejected.
        """
        self.evidence_refs = sorted(set(self.evidence_refs) | set(new_evidence_refs))
        self.evidence_fingerprints = sorted(
            set(self.evidence_fingerprints) | set(new_evidence_fingerprints)
        )
        self.state = _plain(WorkItemState.PROPOSED)
        self.updated_at = _now()
        self.reopening_history.append({
            "at": self.updated_at,
            "reason": reason,
            "evidence_refs": list(new_evidence_refs),
            "evidence_fingerprints": list(new_evidence_fingerprints),
        })


class WorkItemStore(DurableObjectStore):
    """Durable store for work items, keyed by work_item_id."""

    def __init__(self, base_dir: Union[str, Path]):
        super().__init__(
            base_dir,
            factory=WorkItem.from_dict,
            dedup_field=None,
            id_attr="work_item_id",
        )

    def find_by_fingerprint(self, fingerprint: str) -> Optional[WorkItem]:
        return self.find_by_field("fingerprint", fingerprint)

    def _admission_state_for_origin(
        self,
        origin: str,
        is_ambiguous: bool,
        trusted_provenance: bool = True,
    ) -> str:
        """Deterministic admission policy: backlog/discovered rows auto-admit to ready; inferred/creative await owner.

        Owner direction is only auto-ready when it carries trusted provenance;
        otherwise it is reviewed like any other speculative origin.
        """
        # Auto-admit origins are trusted sources that the factory may act on
        # without owner confirmation. Ambiguity only matters for speculative
        # origins (inferred/creative), which always await owner review.
        owner_review_origins = {
            WorkItemOrigin.INFERRED_NEED,
            WorkItemOrigin.CREATIVE_EXPERIMENT,
        }
        if origin in owner_review_origins:
            return WorkItemState.AWAITING_OWNER
        if origin == WorkItemOrigin.OWNER_DIRECTION and not trusted_provenance:
            return WorkItemState.AWAITING_OWNER
        auto_ready_origins = {
            WorkItemOrigin.OWNER_DIRECTION,
            WorkItemOrigin.EXISTING_BACKLOG,
            WorkItemOrigin.DISCOVERED_PROBLEM,
            WorkItemOrigin.SELF_IMPROVEMENT,
            WorkItemOrigin.MAINTENANCE,
        }
        if origin in auto_ready_origins:
            return WorkItemState.READY
        return WorkItemState.AWAITING_OWNER

    def admit_evidence(
        self,
        origin: str,
        repository: str,
        title: str,
        identity_keys: List[str],
        evidence_refs: List[str],
        evidence_fingerprints: List[str],
        is_ambiguous: bool = False,
        trusted_provenance: bool = True,
        **kwargs: Any,
    ) -> WorkItem:
        fingerprint = work_item_fingerprint(origin, repository, identity_keys)
        existing = self.find_by_fingerprint(fingerprint)
        if existing is not None:
            new_fps = set(evidence_fingerprints).difference(existing.evidence_fingerprints)
            new_refs = set(evidence_refs).difference(existing.evidence_refs)
            if not new_fps and not new_refs:
                return existing
            existing.reopen(
                sorted(new_refs), sorted(new_fps), reason="new evidence admitted"
            )
            target = self._admission_state_for_origin(
                origin, is_ambiguous, trusted_provenance=trusted_provenance
            )
            existing.transition_to(
                WorkItemState.ADMITTED,
                reason="reopened evidence readmitted",
            )
            existing.transition_to(
                target,
                reason=(
                    f"origin={origin} ambiguous={is_ambiguous} "
                    f"trusted={trusted_provenance}"
                ),
            )
            self.save_object(existing)
            return existing
        item = WorkItem.create(
            origin=origin,
            repository=repository,
            title=title,
            identity_keys=identity_keys,
            evidence_refs=sorted(set(evidence_refs)),
            evidence_fingerprints=sorted(set(evidence_fingerprints)),
            **kwargs,
        )
        item.transition_to(WorkItemState.ADMITTED, reason="evidence admitted")
        target = self._admission_state_for_origin(origin, is_ambiguous, trusted_provenance=trusted_provenance)
        if target != WorkItemState.ADMITTED:
            item.transition_to(target, reason=f"origin={origin} ambiguous={is_ambiguous} trusted={trusted_provenance}")
        self.save_object(item)
        return item

#!/usr/bin/env python3
"""Durable, auditable owner decisions for Factory WorkItems.

This module deliberately does not reuse the generic task approval lifecycle in
``human_boundary.py``: Factory WorkItems are portfolio-level entities with
different semantics (repository, origin, governed dispatch) than a single
orchestrated task.  Keeping the records separate keeps each boundary auditable
and prevents one approval surface from accidentally authorizing the other.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from howlplane.control_plane.durable_store import DurableObjectStore

OWNER_DECISION_SCHEMA_VERSION = "howlplane.factory.owner_decision/v1"


@dataclass
class OwnerDecisionRecord:
    """One explicit owner approval/rejection of a Factory WorkItem."""

    decision_id: str
    work_item_id: str
    repository: str
    decision: str  # "approved" | "rejected" | "retry"
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    reason: Optional[str] = None
    evidence_fingerprint: Optional[str] = None
    evidence_fingerprints: List[str] = field(default_factory=list)
    mission_digest: Optional[str] = None
    mission_path: Optional[str] = None
    orchestration_task_id: Optional[str] = None
    prior_state: Optional[str] = None
    prior_blocked_reason: Optional[str] = None
    operator_source: str = "cli"
    schema_version: str = OWNER_DECISION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "work_item_id": self.work_item_id,
            "repository": self.repository,
            "decision": self.decision,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "evidence_fingerprint": self.evidence_fingerprint,
            "evidence_fingerprints": list(self.evidence_fingerprints),
            "mission_digest": self.mission_digest,
            "mission_path": self.mission_path,
            "orchestration_task_id": self.orchestration_task_id,
            "prior_state": self.prior_state,
            "prior_blocked_reason": self.prior_blocked_reason,
            "operator_source": self.operator_source,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OwnerDecisionRecord":
        return cls(**{k: v for k, v in data.items() if k in {f.name for f in cls.__dataclass_fields__.values()}})


class OwnerDecisionStore(DurableObjectStore):
    """Atomic store for owner decisions, keyed by decision_id."""

    def __init__(self, base_dir: Union[str, Path]):
        super().__init__(
            base_dir,
            factory=OwnerDecisionRecord.from_dict,
            dedup_field=None,
            id_attr="decision_id",
        )

    def decisions_for(self, work_item_id: str) -> List[OwnerDecisionRecord]:
        return [d for d in self.list_all() if d.work_item_id == work_item_id]

    def latest_for(self, work_item_id: str) -> Optional[OwnerDecisionRecord]:
        decisions = sorted(self.decisions_for(work_item_id), key=lambda d: d.timestamp, reverse=True)
        return decisions[0] if decisions else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace(":", "_").replace("+", "Z")


def record_owner_decision(
    store: OwnerDecisionStore,
    work_item_id: str,
    repository: str,
    decision: str,
    *,
    reason: Optional[str] = None,
    evidence_fingerprint: Optional[str] = None,
    evidence_fingerprints: Optional[List[str]] = None,
    mission_digest: Optional[str] = None,
    mission_path: Optional[str] = None,
    orchestration_task_id: Optional[str] = None,
    prior_state: Optional[str] = None,
    prior_blocked_reason: Optional[str] = None,
    operator_source: str = "cli",
) -> OwnerDecisionRecord:
    """Persist an auditable owner decision and return it."""
    decision_id = f"OWNER-{work_item_id}-{_safe_now()}"
    record = OwnerDecisionRecord(
        decision_id=decision_id,
        work_item_id=work_item_id,
        repository=repository,
        decision=decision,
        timestamp=_now(),
        reason=reason,
        evidence_fingerprint=evidence_fingerprint,
        evidence_fingerprints=list(evidence_fingerprints or []),
        mission_digest=mission_digest,
        mission_path=mission_path,
        orchestration_task_id=orchestration_task_id,
        prior_state=prior_state,
        prior_blocked_reason=prior_blocked_reason,
        operator_source=operator_source,
    )
    store.save_object(record)
    return record

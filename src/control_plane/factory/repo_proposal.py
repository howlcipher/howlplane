#!/usr/bin/env python3
"""Proposal-first repository need disposition and durable capability registry."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


def _plain(value: Any) -> str:
    return str(getattr(value, "value", value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

from src.control_plane.durable_store import DurableObjectStore
from src.control_plane.task_spec import DataClassSerializationMixin

REPO_PROPOSAL_SCHEMA_VERSION = "howlplane.factory.repo_proposal/v1"
CAPABILITY_STORE_SCHEMA_VERSION = "howlplane.factory.capability_registry/v2"


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    DEPRECATED = "deprecated"


class NeedDisposition(str, Enum):
    USE_EXISTING_CAPABILITY = "use_existing_capability"
    IMPROVE_EXISTING_REPOSITORY = "improve_existing_repository"
    BUILD_REUSABLE_CAPABILITY = "build_reusable_capability"
    PROPOSE_NEW_REPOSITORY = "propose_new_repository"
    LOCAL_PROJECT_FIX = "local_project_fix"
    NEEDS_HUMAN_DECISION = "needs_human_decision"


class ProposalState(str, Enum):
    PROPOSED = "proposed"
    AWAITING_AUTHORITY = "awaiting_authority"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass
class CapabilityRecord(DataClassSerializationMixin):
    """Durable record of a capability, where it lives, and whether it is suitable."""

    capability_id: str
    name: str = ""
    description: str = ""
    provided_by: List[str] = field(default_factory=list)
    required_by: List[str] = field(default_factory=list)
    interfaces: List[str] = field(default_factory=list)
    verification_status: str = "unverified"
    verified_by: str = ""
    last_verified_at: Optional[str] = None
    active: bool = True
    evidence_fingerprints: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    schema_version: str = CAPABILITY_STORE_SCHEMA_VERSION

    @property
    def is_verified(self) -> bool:
        return self.verification_status == VerificationStatus.VERIFIED

    def is_suitable_for(self, required_interface: Optional[str] = None) -> bool:
        if not self.active or not self.is_verified:
            return False
        if not required_interface:
            return True
        return required_interface in self.interfaces


class CapabilityStore(DurableObjectStore):
    """Durable store for capability records keyed by capability_id."""

    def __init__(self, base_dir: Union[str, Path]):
        super().__init__(
            base_dir,
            factory=CapabilityRecord.from_dict,
            dedup_field=None,
            id_attr="capability_id",
        )

    def load_all(self) -> Dict[str, CapabilityRecord]:
        return {record.capability_id: record for record in self.list_all()}


class CapabilityRegistry:
    """In-memory registry backed by a durable CapabilityStore."""

    def __init__(self, store: Optional[CapabilityStore] = None):
        self._store = store
        self._records: Dict[str, CapabilityRecord] = {}
        if store is not None:
            self._records = store.load_all()

    def register(self, record: CapabilityRecord) -> None:
        record.updated_at = _now()
        self._records[record.capability_id] = record
        if self._store is not None:
            self._store.save_object(record)

    def find(self, capability_id: str) -> Optional[CapabilityRecord]:
        return self._records.get(capability_id)

    def covers(
        self,
        capability_id: str,
        required_interface: Optional[str] = None,
    ) -> bool:
        record = self.find(capability_id)
        if record is None:
            return False
        return record.is_suitable_for(required_interface)


def _new_repo_eligible(need: Dict[str, Any]) -> bool:
    """Strict evidence rule for a brand-new repository proposal."""
    if need.get("has_natural_home"):
        return False
    if not need.get("clear_purpose"):
        return False
    if not need.get("bounded_maintenance"):
        return False
    if not need.get("deterministic_verification"):
        return False
    return (
        need.get("multiple_consumers")
        or need.get("strong_isolation_lifecycle_reason")
    )


def _local_project_eligible(need: Dict[str, Any]) -> bool:
    """A local project fix is appropriate when a natural home exists and is active."""
    return bool(
        need.get("has_natural_home")
        and need.get("single_consumer")
        and not need.get("requires_cross_project_contract")
    )


def _can_improve_existing(need: Dict[str, Any]) -> bool:
    return bool(
        need.get("existing_repository")
        and need.get("capability_id")
        and not need.get("requires_new_lifecycle")
    )


def _can_build_reusable(need: Dict[str, Any]) -> bool:
    return bool(
        need.get("buildable_capability")
        and need.get("bounded_maintenance")
        and need.get("deterministic_verification")
    )


def resolve_need_disposition(
    registry: CapabilityRegistry,
    need: Dict[str, Any],
    evidence_fingerprints: List[str],
) -> NeedDisposition:
    """Deterministic rules for where a capability need should live."""
    capability_id = need.get("capability_id")
    required_interface = need.get("required_interface")

    if capability_id and registry.covers(capability_id, required_interface):
        if _can_improve_existing(need):
            return NeedDisposition.IMPROVE_EXISTING_REPOSITORY
        return NeedDisposition.USE_EXISTING_CAPABILITY

    if _local_project_eligible(need):
        return NeedDisposition.LOCAL_PROJECT_FIX

    if _can_build_reusable(need):
        return NeedDisposition.BUILD_REUSABLE_CAPABILITY

    if _new_repo_eligible(need) and evidence_fingerprints:
        return NeedDisposition.PROPOSE_NEW_REPOSITORY

    return NeedDisposition.NEEDS_HUMAN_DECISION


@dataclass
class RepoProposal(DataClassSerializationMixin):
    """Proposal-first record for a future repository bootstrap."""

    proposal_id: str
    repository_name: str
    disposition: str
    rationale: str
    evidence_fingerprints: List[str] = field(default_factory=list)
    bootstrap_plan: Dict[str, Any] = field(default_factory=dict)
    state: str = _plain(ProposalState.AWAITING_AUTHORITY)
    schema_version: str = REPO_PROPOSAL_SCHEMA_VERSION

    def __post_init__(self):
        self.disposition = str(self.disposition)
        self.state = str(self.state)

    def as_bootstrap_contract(self) -> Dict[str, Any]:
        return {
            "schema": "howlplane.factory.bootstrap_contract/v1",
            "proposal_id": self.proposal_id,
            "repository_name": self.repository_name,
            "disposition": self.disposition,
            "evidence_fingerprints": self.evidence_fingerprints,
            "bootstrap_plan": self.bootstrap_plan,
            "state": self.state,
        }


class RepoProposalStore(DurableObjectStore):
    """Durable store for repository proposals keyed by proposal_id."""

    def __init__(self, base_dir: Union[str, Path]):
        super().__init__(
            base_dir,
            factory=RepoProposal.from_dict,
            dedup_field=None,
            id_attr="proposal_id",
        )

    def propose(
        self,
        proposal_id: str,
        repository_name: str,
        disposition: str,
        rationale: str,
        evidence_fingerprints: List[str],
        bootstrap_plan: Dict[str, Any],
    ) -> Optional[RepoProposal]:
        """Persist a new repository proposal, refusing to overwrite or propose emptiness."""
        if not proposal_id:
            return None
        if not repository_name:
            return None
        capability_id = (bootstrap_plan or {}).get("capability_id") or proposal_id
        if not capability_id:
            return None
        if self.exists(proposal_id):
            return self.load(proposal_id)

        proposal = RepoProposal(
            proposal_id=proposal_id,
            repository_name=repository_name,
            disposition=disposition,
            rationale=rationale,
            evidence_fingerprints=sorted(set(evidence_fingerprints)),
            bootstrap_plan=bootstrap_plan,
        )
        self.save_object(proposal)
        return proposal

    def list_awaiting_authority(self) -> List[RepoProposal]:
        return [p for p in self.list_all() if p.state == ProposalState.AWAITING_AUTHORITY]

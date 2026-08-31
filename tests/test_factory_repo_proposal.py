#!/usr/bin/env python3
"""Tests for deterministic need disposition and proposal-first repository records."""

import pytest

from src.control_plane.factory.repo_proposal import (
    CapabilityRecord,
    CapabilityRegistry,
    CapabilityStore,
    NeedDisposition,
    ProposalState,
    RepoProposal,
    RepoProposalStore,
    resolve_need_disposition,
)


def _registry(tmp_path=None):
    store = CapabilityStore(tmp_path / "capabilities") if tmp_path else None
    return CapabilityRegistry(store)


VERIFIED_AUTHZ = CapabilityRecord(
    capability_id="authz",
    provided_by=["howlplane"],
    interfaces=["http"],
    active=True,
    verification_status="verified",
)


@pytest.mark.parametrize(
    "need, evidence, existing_capability, expected",
    [
        (
            {"capability_id": "authz", "required_interface": "http"},
            ["fp-1"],
            VERIFIED_AUTHZ,
            NeedDisposition.USE_EXISTING_CAPABILITY,
        ),
        (
            {
                "capability_id": "authz",
                "required_interface": "http",
                "existing_repository": True,
            },
            ["fp-1"],
            VERIFIED_AUTHZ,
            NeedDisposition.IMPROVE_EXISTING_REPOSITORY,
        ),
        (
            {
                "capability_id": "telemetry",
                "buildable_capability": True,
                "bounded_maintenance": True,
                "deterministic_verification": True,
            },
            ["fp-2"],
            None,
            NeedDisposition.BUILD_REUSABLE_CAPABILITY,
        ),
        (
            {
                "capability_id": "shared_schema",
                "has_natural_home": False,
                "clear_purpose": True,
                "bounded_maintenance": True,
                "deterministic_verification": True,
                "multiple_consumers": True,
                "proposed_repository": "howl-schema",
            },
            ["fp-3"],
            None,
            NeedDisposition.PROPOSE_NEW_REPOSITORY,
        ),
        (
            {"capability_id": "lint", "has_natural_home": True, "single_consumer": True},
            ["fp-4"],
            None,
            NeedDisposition.LOCAL_PROJECT_FIX,
        ),
        (
            {"capability_id": "vague"},
            ["fp-5"],
            None,
            NeedDisposition.NEEDS_HUMAN_DECISION,
        ),
        (
            {
                "capability_id": "x",
                "has_natural_home": False,
                "clear_purpose": True,
                "bounded_maintenance": True,
                "deterministic_verification": True,
                "multiple_consumers": True,
            },
            [],
            None,
            NeedDisposition.NEEDS_HUMAN_DECISION,
        ),
    ],
)
def test_need_disposition_rules(need, evidence, existing_capability, expected):
    registry = _registry()
    if existing_capability is not None:
        registry.register(existing_capability)

    assert resolve_need_disposition(registry, need, evidence) == expected


def test_capability_store_is_durable(tmp_path):
    store = CapabilityStore(tmp_path / "capabilities")
    record = CapabilityRecord(capability_id="test", provided_by=["howlplane"])
    store.save_object(record)
    loaded = store.load("test")
    assert loaded.capability_id == "test"


def test_repo_proposal_store_round_trip(tmp_path):
    store = RepoProposalStore(tmp_path / "proposals")
    proposal = store.propose(
        proposal_id="PROP-1",
        repository_name="howl-new-thing",
        disposition=str(NeedDisposition.PROPOSE_NEW_REPOSITORY),
        rationale="strict evidence",
        evidence_fingerprints=["fp-6"],
        bootstrap_plan={"steps": ["init"]},
    )
    loaded = store.load(proposal.proposal_id)
    assert loaded.disposition == str(NeedDisposition.PROPOSE_NEW_REPOSITORY)
    assert loaded.state == ProposalState.AWAITING_AUTHORITY


def test_repo_proposal_bootstrap_contract():
    proposal = RepoProposal(
        proposal_id="PROP-2",
        repository_name="howl-metrics",
        disposition=str(NeedDisposition.NEEDS_HUMAN_DECISION),
        rationale="unclear ownership",
        evidence_fingerprints=["fp-7"],
        bootstrap_plan={"tests": ["lint", "test"]},
    )
    contract = proposal.as_bootstrap_contract()
    assert contract["schema"] == "howlplane.factory.bootstrap_contract/v1"
    assert contract["proposal_id"] == "PROP-2"


@pytest.mark.parametrize(
    "proposal_id, repository_name, bootstrap_capability_id",
    [
        ("PROP-EMPTY", "", "x"),
        ("", "howl-new", ""),
    ],
)
def test_propose_requires_non_empty_repository_and_capability(
    tmp_path, proposal_id, repository_name, bootstrap_capability_id
):
    store = RepoProposalStore(tmp_path / "proposals")
    assert store.propose(
        proposal_id=proposal_id,
        repository_name=repository_name,
        disposition=str(NeedDisposition.PROPOSE_NEW_REPOSITORY),
        rationale="empty field",
        evidence_fingerprints=["fp-1"],
        bootstrap_plan={"capability_id": bootstrap_capability_id},
    ) is None


def test_propose_does_not_overwrite_existing_proposal(tmp_path):
    store = RepoProposalStore(tmp_path / "proposals")
    first = store.propose(
        proposal_id="PROP-EXIST",
        repository_name="howl-first",
        disposition=str(NeedDisposition.PROPOSE_NEW_REPOSITORY),
        rationale="first",
        evidence_fingerprints=["fp-1"],
        bootstrap_plan={"capability_id": "x"},
    )
    second = store.propose(
        proposal_id="PROP-EXIST",
        repository_name="howl-second",
        disposition=str(NeedDisposition.PROPOSE_NEW_REPOSITORY),
        rationale="second",
        evidence_fingerprints=["fp-2"],
        bootstrap_plan={"capability_id": "x"},
    )
    assert second.repository_name == first.repository_name
    assert second.rationale == first.rationale


def test_existing_capability_record_merges_reuse_evidence(tmp_path):
    store = CapabilityStore(tmp_path / "capabilities")
    record = CapabilityRecord(
        capability_id="authz",
        provided_by=["howlplane"],
        interfaces=["http"],
        active=True,
        verification_status="verified",
        evidence_fingerprints=["fp-1"],
    )
    store.save_object(record)
    registry = CapabilityRegistry(store)
    found = registry.find("authz")
    assert found is not None
    found.evidence_fingerprints = sorted(set(found.evidence_fingerprints) | {"fp-2"})
    registry.register(found)
    reloaded = store.load("authz")
    assert sorted(reloaded.evidence_fingerprints) == ["fp-1", "fp-2"]

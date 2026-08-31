#!/usr/bin/env python3
"""Mutation-style checks proving key acceptance assertions are real detectors."""

from datetime import datetime, timezone

import pytest


pytest.importorskip(
    "src.control_plane.factory.portfolio",
    reason="factory portfolio is being integrated by PR #68",
)

from src.control_plane.factory import portfolio
from src.control_plane.factory import repo_proposal
from src.control_plane.factory.portfolio import FactoryPolicy
from src.control_plane.factory.repo_proposal import (
    CapabilityRecord,
    CapabilityRegistry,
    NeedDisposition,
    VerificationStatus,
)
from src.control_plane.factory.work_item import (
    WorkItem,
    WorkItemOrigin,
    WorkItemState,
)


NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def _ready(origin):
    item = WorkItem.create(
        origin=origin,
        repository="howlcipher/howlplane",
        title=str(origin),
        identity_keys=[str(origin)],
    )
    item.transition_to(WorkItemState.ADMITTED)
    item.transition_to(WorkItemState.READY)
    return item


def test_portfolio_cap_assertion_fails_when_cap_protection_is_injected_away(
    monkeypatch,
):
    candidate = _ready(WorkItemOrigin.SELF_IMPROVEMENT)
    policy = FactoryPolicy()
    history = [
        {
            "origin": WorkItemOrigin.SELF_IMPROVEMENT,
            "repository": "howlcipher/howlplane",
        }
        for _ in range(policy.max_introspective_in_window)
    ]

    def assert_capped():
        outcome = portfolio.select([candidate], history, policy, now=NOW)
        assert outcome.item is None
        assert outcome.reason == "all_candidates_capped"

    assert_capped()
    monkeypatch.setattr(portfolio, "_cap_blocking", lambda item, counts, policy: None)
    with pytest.raises(AssertionError):
        assert_capped()


def test_capability_reuse_assertion_fails_when_registry_cover_is_disabled(
    monkeypatch,
):
    registry = CapabilityRegistry()
    registry.register(
        CapabilityRecord(
            capability_id="shared/v1",
            interfaces=["shared/v1"],
            verification_status=VerificationStatus.VERIFIED,
            active=True,
        )
    )
    need = {
        "capability_id": "shared/v1",
        "required_interface": "shared/v1",
    }

    def assert_reused():
        assert repo_proposal.resolve_need_disposition(
            registry, need, ["verified-fingerprint"]
        ) == NeedDisposition.USE_EXISTING_CAPABILITY

    assert_reused()
    monkeypatch.setattr(registry, "covers", lambda *args, **kwargs: False)
    with pytest.raises(AssertionError):
        assert_reused()


def test_new_repository_threshold_assertion_fails_when_gate_is_forced_open(
    monkeypatch,
):
    need = {
        "capability_id": "one-off/v1",
        "has_natural_home": True,
        "single_consumer": True,
        "requires_cross_project_contract": False,
        "clear_purpose": True,
        "bounded_maintenance": True,
        "deterministic_verification": True,
    }

    def assert_no_new_repository():
        disposition = repo_proposal.resolve_need_disposition(
            CapabilityRegistry(), need, ["one-event"]
        )
        assert disposition != NeedDisposition.PROPOSE_NEW_REPOSITORY

    assert_no_new_repository()
    monkeypatch.setattr(repo_proposal, "_local_project_eligible", lambda value: False)
    monkeypatch.setattr(repo_proposal, "_new_repo_eligible", lambda value: True)
    with pytest.raises(AssertionError):
        assert_no_new_repository()

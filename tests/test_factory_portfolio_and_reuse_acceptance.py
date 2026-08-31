#!/usr/bin/env python3
"""Portfolio, capability reuse, and repository proposal acceptance contracts."""

from dataclasses import fields
from datetime import datetime, timedelta, timezone
import importlib

import pytest


pytest.importorskip(
    "src.control_plane.factory.work_item",
    reason="factory work portfolio is being integrated by PR #68",
)

from src.control_plane.factory.portfolio import FactoryPolicy, select
from src.control_plane.factory.repo_proposal import (
    CapabilityRecord,
    CapabilityRegistry,
    NeedDisposition,
    RepoProposal,
    VerificationStatus,
    resolve_need_disposition,
)
from src.control_plane.factory.work_item import (
    WorkItem,
    WorkItemOrigin,
    WorkItemState,
    WorkItemStore,
)


NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def _ready(key, origin, repository="howlcipher/howlframe", created_at=None):
    item = WorkItem.create(
        origin=origin,
        repository=repository,
        title=key,
        identity_keys=[key],
        created_at=(created_at or NOW).isoformat(),
    )
    item.transition_to(WorkItemState.ADMITTED)
    item.transition_to(WorkItemState.READY)
    return item


def test_owner_direction_preempts_caps_and_lower_priority_work():
    policy = FactoryPolicy()
    owner = _ready(
        "owner",
        WorkItemOrigin.OWNER_DIRECTION,
        repository="howlcipher/howlplane",
    )
    product = _ready("product", WorkItemOrigin.EXISTING_BACKLOG)
    history = [
        {
            "origin": WorkItemOrigin.SELF_IMPROVEMENT,
            "repository": "howlcipher/howlplane",
        }
        for _ in range(policy.portfolio_window)
    ]
    assert select([product, owner], history, policy, now=NOW).item is owner


def test_parked_and_blocked_work_never_win_selection():
    parked = WorkItem.create(
        origin=WorkItemOrigin.OWNER_DIRECTION,
        repository="howlcipher/howlframe",
        title="parked",
        identity_keys=["parked"],
    )
    parked.transition_to(WorkItemState.ADMITTED)
    parked.transition_to(WorkItemState.AWAITING_OWNER)
    blocked = WorkItem.create(
        origin=WorkItemOrigin.OWNER_DIRECTION,
        repository="howlcipher/howlframe",
        title="blocked",
        identity_keys=["blocked"],
        blocked_by=["WI-dependency"],
    )
    blocked.transition_to(WorkItemState.ADMITTED)
    blocked.transition_to(WorkItemState.BLOCKED)
    ordinary = _ready("ordinary", WorkItemOrigin.EXISTING_BACKLOG)
    assert select([parked, blocked, ordinary], [], FactoryPolicy(), now=NOW).item is ordinary


def test_starvation_override_changes_order_within_repository_not_owner_priority():
    starving = _ready(
        "starving",
        WorkItemOrigin.EXISTING_BACKLOG,
        created_at=NOW - timedelta(hours=73),
    )
    recent = _ready("recent", WorkItemOrigin.EXISTING_BACKLOG)
    recent.source_rank = 0
    starving.source_rank = 20
    owner = _ready("owner", WorkItemOrigin.OWNER_DIRECTION)
    outcome = select([recent, starving], [], FactoryPolicy(), now=NOW)
    assert outcome.item is starving
    assert select([starving, owner], [], FactoryPolicy(), now=NOW).item is owner


def test_factory_returns_no_eligible_work_instead_of_inventing_work():
    outcome = select([], [], FactoryPolicy(), now=NOW)
    assert outcome.item is None
    assert outcome.reason == "no_dispatchable_work"


@pytest.mark.parametrize(
    "origin,history_origin,expected_reason",
    [
        (
            WorkItemOrigin.SELF_IMPROVEMENT,
            WorkItemOrigin.SELF_IMPROVEMENT,
            "self_improvement_cap",
        ),
        (
            WorkItemOrigin.MAINTENANCE,
            WorkItemOrigin.MAINTENANCE,
            "self_improvement_cap",
        ),
        (
            WorkItemOrigin.CREATIVE_EXPERIMENT,
            WorkItemOrigin.CREATIVE_EXPERIMENT,
            "creative_experiment_cap",
        ),
    ],
)
def test_introspective_and_creative_work_cannot_dominate_portfolio(
    origin, history_origin, expected_reason
):
    policy = FactoryPolicy()
    candidate = _ready("capped", origin)
    limit = (
        policy.max_creative_in_window
        if origin == WorkItemOrigin.CREATIVE_EXPERIMENT
        else policy.max_introspective_in_window
    )
    history = [
        {"origin": history_origin, "repository": "howlcipher/howlplane"}
        for _ in range(limit)
    ]

    outcome = select([candidate], history, policy, now=NOW)

    assert outcome.item is None
    assert outcome.reason == "all_candidates_capped"
    assert outcome.withheld[0]["reason"] == expected_reason


def test_non_product_repository_cap_prevents_monopoly():
    policy = FactoryPolicy()
    non_product = _ready(
        "non-product", WorkItemOrigin.EXISTING_BACKLOG, "howlcipher/howlplane"
    )
    product = _ready("product", WorkItemOrigin.EXISTING_BACKLOG)
    history = [
        {
            "origin": WorkItemOrigin.EXISTING_BACKLOG,
            "repository": "howlcipher/howlplane",
        }
        for _ in range(policy.max_non_product_in_window)
    ]

    outcome = select([non_product, product], history, policy, now=NOW)

    assert outcome.item is product
    assert outcome.withheld[0]["reason"] == "non_product_repository_cap"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_24_7: portfolio fairness caps only the aggregate non-product "
        "share, so the designated product repository can monopolize dispatch"
    ),
)
def test_designated_product_repository_cannot_monopolize_factory():
    policy = FactoryPolicy()
    product = _ready("product", WorkItemOrigin.EXISTING_BACKLOG)
    product.source_rank = 0
    other = _ready(
        "other", WorkItemOrigin.EXISTING_BACKLOG, "howlcipher/howlplane"
    )
    other.source_rank = 1
    history = [
        {
            "origin": WorkItemOrigin.EXISTING_BACKLOG,
            "repository": policy.product_repository,
        }
        for _ in range(policy.portfolio_window)
    ]

    assert select([product, other], history, policy, now=NOW).item is other


def test_duplicate_observations_deduplicate_to_one_work_item(tmp_path):
    store = WorkItemStore(tmp_path / "work")
    values = {
        "origin": WorkItemOrigin.DISCOVERED_PROBLEM,
        "repository": "howlcipher/howlframe",
        "title": "same observation",
        "identity_keys": ["observation", "same"],
        "evidence_refs": ["evidence/same.json"],
        "evidence_fingerprints": ["sha256:same"],
    }

    first = store.admit_evidence(**values)
    second = store.admit_evidence(**values)

    assert first.work_item_id == second.work_item_id
    assert len(store.list_all()) == 1


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_24_7: WorkItemStore reopens disposed work for a new evidence "
        "reference even when no materially new evidence fingerprint exists"
    ),
)
def test_reopen_requires_new_evidence_fingerprint_not_a_renamed_reference(tmp_path):
    store = WorkItemStore(tmp_path / "work")
    item = store.admit_evidence(
        origin=WorkItemOrigin.INFERRED_NEED,
        repository="howlcipher/howlframe",
        title="same need",
        identity_keys=["need", "x"],
        evidence_refs=["evidence/first.json"],
        evidence_fingerprints=["sha256:same"],
    )
    item.transition_to(WorkItemState.REJECTED)
    store.save_object(item)

    observed = store.admit_evidence(
        origin=WorkItemOrigin.INFERRED_NEED,
        repository="howlcipher/howlframe",
        title="same need",
        identity_keys=["need", "x"],
        evidence_refs=["evidence/renamed.json"],
        evidence_fingerprints=["sha256:same"],
    )
    assert observed.state == WorkItemState.REJECTED


def test_exact_verified_capability_match_is_reused():
    registry = CapabilityRegistry()
    registry.register(
        CapabilityRecord(
            capability_id="deployment-dll-inspector/v1",
            provided_by=["howlcipher/tooling"],
            required_by=["howlcipher/repo-a"],
            interfaces=["deployment-dll-inspector/v1"],
            verification_status=VerificationStatus.VERIFIED,
            evidence_fingerprints=["verified:contract-v1"],
        )
    )
    disposition = resolve_need_disposition(
        registry,
        {
            "capability_id": "deployment-dll-inspector/v1",
            "required_interface": "deployment-dll-inspector/v1",
        },
        ["verified:contract-v1"],
    )
    assert disposition == NeedDisposition.USE_EXISTING_CAPABILITY


@pytest.mark.parametrize(
    "status,active",
    [
        (VerificationStatus.UNVERIFIED, True),
        (VerificationStatus.DEPRECATED, True),
        (VerificationStatus.VERIFIED, False),
    ],
)
def test_unverified_deprecated_or_inactive_capability_is_not_reused(
    status, active
):
    registry = CapabilityRegistry()
    registry.register(
        CapabilityRecord(
            capability_id="shared/v1",
            interfaces=["shared/v1"],
            verification_status=status,
            active=active,
        )
    )

    assert registry.covers("shared/v1", "shared/v1") is False


def test_no_suitable_capability_does_not_claim_reuse():
    disposition = resolve_need_disposition(
        CapabilityRegistry(),
        {
            "capability_id": "missing/v1",
            "required_interface": "missing/v1",
        },
        ["need:verified"],
    )
    assert disposition != NeedDisposition.USE_EXISTING_CAPABILITY


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_AUTONOMOUS_REPO_CREATION: reuse only looks up capability_id "
        "and cannot discover a compatible verified interface under another ID"
    ),
)
def test_compatible_interface_is_reused_without_exact_capability_id():
    registry = CapabilityRegistry()
    registry.register(
        CapabilityRecord(
            capability_id="shared-implementation/v2",
            interfaces=["deployment-inspection/v1"],
            verification_status=VerificationStatus.VERIFIED,
        )
    )
    disposition = resolve_need_disposition(
        registry,
        {
            "capability_id": "requested-name/v1",
            "required_interface": "deployment-inspection/v1",
        },
        ["need:verified"],
    )
    assert disposition == NeedDisposition.USE_EXISTING_CAPABILITY


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_AUTONOMOUS_REPO_CREATION: capability records have no risk "
        "or authority domain and freshness is recorded but not enforced"
    ),
)
def test_capability_registry_can_reject_stale_unverified_or_wrong_domain_records():
    names = {field.name for field in fields(CapabilityRecord)}
    assert {
        "risk_domain",
        "authority_domain",
    }.issubset(names)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_AUTONOMOUS_REPO_CREATION: multiple compatible capability "
        "candidates have no deterministic ranking or ambiguity disposition"
    ),
)
def test_multiple_capability_candidates_are_resolved_deterministically():
    registry = CapabilityRegistry()
    assert hasattr(registry, "find_suitable")
    assert registry.find_suitable("shared/v1") in {None, "NEEDS_HUMAN_DECISION"}


def test_one_tiny_helper_for_one_repo_does_not_create_repository():
    disposition = resolve_need_disposition(
        CapabilityRegistry(),
        {
            "capability_id": "one-off-helper/v1",
            "has_natural_home": True,
            "single_consumer": True,
            "requires_cross_project_contract": False,
        },
        ["repo-a:one-observation"],
    )
    assert disposition != NeedDisposition.PROPOSE_NEW_REPOSITORY


def test_three_independent_consumers_can_justify_repository_proposal():
    disposition = resolve_need_disposition(
        CapabilityRegistry(),
        {
            "capability_id": "shared-tool/v1",
            "no_natural_home": True,
            "has_natural_home": False,
            "multiple_consumers": True,
            "consumer_repositories": ["repo-a", "repo-b", "repo-c"],
            "clear_purpose": True,
            "bounded_maintenance": True,
            "deterministic_verification": True,
        },
        ["repo-a:fp", "repo-b:fp", "repo-c:fp"],
    )
    assert disposition == NeedDisposition.PROPOSE_NEW_REPOSITORY


def test_creative_idea_without_evidence_cannot_create_repository():
    disposition = resolve_need_disposition(
        CapabilityRegistry(),
        {
            "capability_id": "creative-product/v1",
            "no_natural_home": True,
            "multiple_consumers": True,
            "requires_isolation_verification": True,
            "origin": "creative_experiment",
        },
        [],
    )
    assert disposition == NeedDisposition.NEEDS_HUMAN_DECISION


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_AUTONOMOUS_REPO_CREATION: bootstrap contract accepts an "
        "empty plan and does not require the repository safety baseline"
    ),
)
def test_repository_bootstrap_contract_requires_safety_baseline():
    proposal = RepoProposal(
        proposal_id="PROP-safety",
        repository_name="howl-tool",
        disposition=NeedDisposition.PROPOSE_NEW_REPOSITORY,
        rationale="three independent consumers",
        evidence_fingerprints=["a", "b", "c"],
        bootstrap_plan={},
    )
    contract = proposal.as_bootstrap_contract()
    assert {
        "readme",
        "agents",
        "project_manifest",
        "tests",
        "lint",
        "ci",
        "security_scan",
        "hygiene",
        "branch_protection",
        "versioning",
        "owner_purpose",
        "discoverability",
        "capability_registration",
    }.issubset(contract["bootstrap_plan"])


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_24_7: owner-direction and inferred-need discovery evidence "
        "thresholds have no production module yet"
    ),
)
def test_need_discovery_has_deterministic_owner_and_repetition_thresholds():
    discovery = importlib.import_module("src.control_plane.factory.discovery")
    assert hasattr(discovery, "classify_owner_direction")
    assert hasattr(discovery, "infer_repeated_owner_need")
    repeated = discovery.infer_repeated_owner_need(
        [{"fingerprint": "dll-conflict", "repository": f"repo-{index}"} for index in range(3)]
    )
    isolated = discovery.infer_repeated_owner_need(
        [{"fingerprint": "ran-script-once", "repository": "repo-a"}]
    )
    assert repeated is not None
    assert isolated is None

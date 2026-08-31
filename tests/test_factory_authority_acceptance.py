#!/usr/bin/env python3
"""Factory self-improvement authority invariants."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.control_plane.authority_envelope import create_envelope
from src.control_plane.authority_profile import OVERNIGHT_SAFE_PROFILE
from src.control_plane.human_boundary import HumanBoundaryGate
from src.control_plane import human_boundary
from src.control_plane.task_spec import TaskSpec


REPOSITORY = "howlcipher/howlplane"


def _envelope():
    return create_envelope(
        OVERNIGHT_SAFE_PROFILE,
        "FACTORY-AUTHORITY-ACCEPTANCE",
        "cli:operator@test-host",
        now=datetime.now(timezone.utc),
    )


def _task(objective: str = "Improve factory capability") -> TaskSpec:
    return TaskSpec(
        task_id="FACTORY-AUTHORITY-1",
        repository="howlplane",
        objective=objective,
        task_class="feature",
        risk_level="low",
    )


@pytest.mark.parametrize(
    "changed_path",
    [
        "src/control_plane/authority_profile.py",
        "src/control_plane/authority_envelope.py",
        "src/control_plane/human_boundary.py",
        "src/control_plane/executor.py",
    ],
)
def test_factory_cannot_edit_policy_that_grants_factory_authority(changed_path):
    result = HumanBoundaryGate.evaluate_with_delegated_authority(
        _task(),
        [],
        _envelope(),
        "howlplane",
        repo_slug=REPOSITORY,
        files_changed=[changed_path],
    )
    assert result.requires_human_approval is True
    assert "authority_enforcement_modification" in result.triggered_boundaries


@pytest.mark.parametrize(
    "objective,planned_action,expected_boundary",
    [
        ("Enable force push", "git push --force origin main", "force_push"),
        (
            "Bypass required checks",
            "merge with --admin and bypass required check",
            "bypass_required_checks",
        ),
        (
            "Deploy the factory to production",
            "production deploy",
            "production_deployment",
        ),
        (
            "Remove branch protection",
            "weaken branch protection",
            "branch_protection_weakening",
        ),
    ],
)
def test_factory_never_delegates_explicit_self_serving_actions(
    objective, planned_action, expected_boundary
):
    result = HumanBoundaryGate.evaluate_with_delegated_authority(
        _task(objective),
        [planned_action],
        _envelope(),
        "howlplane",
        repo_slug=REPOSITORY,
    )
    assert result.requires_human_approval is True
    assert expected_boundary in result.triggered_boundaries


def test_force_push_detector_fails_if_never_delegatable_protection_is_removed(
    monkeypatch,
):
    weakened_profile = replace(
        OVERNIGHT_SAFE_PROFILE,
        allowed_action_classes=(
            list(OVERNIGHT_SAFE_PROFILE.allowed_action_classes) + ["force_push"]
        ),
        denied_action_classes=[
            action
            for action in OVERNIGHT_SAFE_PROFILE.denied_action_classes
            if action != "force_push"
        ],
    )
    weakened_envelope = create_envelope(
        weakened_profile,
        "FACTORY-INJECTED-AUTHORITY-DEFECT",
        "cli:operator@test-host",
        now=datetime.now(timezone.utc),
    )
    monkeypatch.setattr(
        human_boundary,
        "NEVER_DELEGATABLE_BOUNDARIES",
        set(human_boundary.NEVER_DELEGATABLE_BOUNDARIES) - {"force_push"},
    )

    def assert_force_push_is_parked():
        result = HumanBoundaryGate.evaluate_with_delegated_authority(
            _task("Enable force push"),
            ["git push --force origin main"],
            weakened_envelope,
            "howlplane",
            repo_slug=REPOSITORY,
        )
        assert result.requires_human_approval is True

    with pytest.raises(AssertionError):
        assert_force_push_is_parked()


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_24_7: review and verification policy modules are not yet "
        "classified as authority-enforcement self-modification"
    ),
)
@pytest.mark.parametrize(
    "changed_path",
    [
        "src/control_plane/reviewers.py",
        "src/control_plane/review_runner.py",
        "src/control_plane/reconciliation.py",
        "src/control_plane/verification.py",
        "src/control_plane/hygiene_policy.py",
    ],
)
def test_factory_cannot_remove_independent_review_or_deterministic_verification(
    changed_path,
):
    result = HumanBoundaryGate.evaluate_with_delegated_authority(
        _task("Weaken the checks that constrain future factory changes"),
        [],
        _envelope(),
        "howlplane",
        repo_slug=REPOSITORY,
        files_changed=[changed_path],
    )
    assert result.requires_human_approval is True
    assert "authority_enforcement_modification" in result.triggered_boundaries


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BLOCKING_24_7: the factory subtree is protected only by PR #68 and "
        "is not covered on origin/main"
    ),
)
def test_factory_cannot_rewrite_its_own_dispatch_controller():
    result = HumanBoundaryGate.evaluate_with_delegated_authority(
        _task("Raise the factory's own dispatch limits"),
        [],
        _envelope(),
        "howlplane",
        repo_slug=REPOSITORY,
        files_changed=["src/control_plane/factory/supervisor.py"],
    )
    assert result.requires_human_approval is True
    assert "authority_enforcement_modification" in result.triggered_boundaries

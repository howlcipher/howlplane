#!/usr/bin/env python3
"""
tests/test_factory_self_modification.py

The factory must never be able to rewrite the controller it is running under.

`SELF_MODIFICATION_PATHS` is matched with `endswith`, which can name a file but
not a directory. The factory is a package that will grow modules, so an exact
path list would silently stop covering it the first time someone adds a file.
`SELF_MODIFICATION_PATH_PREFIXES` covers the subtree instead, and the load
bearing test here walks the package on disk rather than restating a list -- a
module added next year is covered without anyone remembering to add it.

The prefix rule deliberately over-covers: `portfolio.py` is a pure function and
`work_item.py` is a dataclass, neither of which enforces authority. Flagging
them costs one park, which is cheap. Missing the supervisor would not be.
"""

from pathlib import Path

import pytest

from src.control_plane.proposed_action import (
    SELF_MODIFICATION_PATH_PREFIXES,
    SELF_MODIFICATION_PATHS,
    infer_proposed_actions_from_diff,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FACTORY_DIR = REPO_ROOT / "src" / "control_plane" / "factory"


def _factory_modules():
    return sorted(p for p in FACTORY_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_factory_package_actually_exists_to_be_guarded():
    """Guards the guard: an empty glob would make every test below vacuous."""
    assert _factory_modules(), f"no python modules found under {FACTORY_DIR}"


@pytest.mark.parametrize("module", _factory_modules(), ids=lambda p: p.name)
def test_every_factory_module_is_a_self_modification_boundary(module):
    rel = module.relative_to(REPO_ROOT).as_posix()
    actions = infer_proposed_actions_from_diff([rel], repo_name="howlcipher/howlplane")

    assert actions, f"{rel} is not flagged as self-modification"
    action = actions[0]
    assert action.action_type == "authority_enforcement_modification"
    assert action.risk_level == "critical"
    # No bounded executor may ever run it, even with an approval in hand.
    assert action.executor_id is None
    assert action.requires_bounded_execution is True


def test_a_module_that_does_not_exist_yet_is_still_covered():
    """The whole reason for a prefix rule rather than a path list."""
    future = "src/control_plane/factory/future_guard.py"
    assert not (REPO_ROOT / future).exists()

    actions = infer_proposed_actions_from_diff([future])
    assert actions and actions[0].risk_level == "critical"


def test_nested_future_subpackages_are_covered():
    actions = infer_proposed_actions_from_diff(
        ["src/control_plane/factory/miners/ci_failures.py"]
    )
    assert actions and actions[0].action_type == "authority_enforcement_modification"


def test_windows_style_separators_are_normalized():
    actions = infer_proposed_actions_from_diff(
        ["src\\control_plane\\factory\\portfolio.py"]
    )
    assert actions, "backslash-separated paths must normalize before matching"


def test_the_original_authority_files_are_still_covered():
    """The prefix rule is additive; it must not displace the exact-path list."""
    for path in SELF_MODIFICATION_PATHS:
        actions = infer_proposed_actions_from_diff([path])
        assert actions, f"{path} lost its self-modification protection"


def test_ordinary_control_plane_files_are_not_swept_up():
    """Over-broad matching would park routine work and stall the factory."""
    for benign in (
        "src/control_plane/router.py",
        "src/control_plane/backlog_source.py",
        "src/control_plane/metrics.py",
        "tests/test_factory_portfolio.py",
        "documentation/CONTROL_PLANE.md",
    ):
        assert infer_proposed_actions_from_diff([benign]) == [], benign


def test_a_path_merely_mentioning_factory_is_not_matched():
    """`factory/` must mean the package, not any path with the word in it."""
    assert infer_proposed_actions_from_diff(["src/infrastructure/factory_helpers.py"]) == []
    assert infer_proposed_actions_from_diff(["src/infrastructure/vector_store_factory.py"]) == []


def test_the_prefix_list_is_anchored_to_a_directory():
    for prefix in SELF_MODIFICATION_PATH_PREFIXES:
        assert prefix.endswith("/"), f"{prefix!r} must end in / to mean a directory"


def test_factory_work_item_with_self_modifying_path_is_parked_by_boundary(tmp_path):
    from unittest.mock import MagicMock

    from src.control_plane.authority_envelope import create_envelope
    from src.control_plane.authority_profile import OVERNIGHT_SAFE_PROFILE
    from src.control_plane.factory.work_item import WorkItem, WorkItemOrigin
    from src.control_plane.synthesis import MarathonDogfoodEngine

    # Overnight-safe profile allows routine git/GitHub actions but explicitly
    # denies authority_enforcement_modification via NEVER_DELEGATABLE_BOUNDARIES,
    # so a factory work item touching the supervisor must park.
    envelope = create_envelope(
        OVERNIGHT_SAFE_PROFILE,
        campaign_id="TEST-CAMPAIGN",
        operator_origin="test",
    )
    pool = MagicMock()
    pool.select_candidates.return_value = ["codex"]
    engine = MarathonDogfoodEngine(
        provider_pool=pool,
        target_repo=REPO_ROOT,
        campaign_dir=tmp_path / "campaigns",
    )
    engine.authority_envelope = envelope
    engine.git_executor = engine._git_executor_factory(envelope, 0)

    item = WorkItem.create(
        origin=WorkItemOrigin.EXISTING_BACKLOG,
        repository="howlcipher/howlplane",
        title="fix supervisor",
        identity_keys=["supervisor"],
        evidence_refs=["src/control_plane/factory/supervisor.py#1"],
    )
    success, git_record = engine.execute_factory_work_item(
        item,
        files_changed=["src/control_plane/factory/supervisor.py"],
    )
    assert success is False
    assert git_record is not None
    assert git_record.get("integration_mode") == "parked"
    pool.select_candidates.assert_called_once()

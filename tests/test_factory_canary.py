#!/usr/bin/env python3
"""Tests for the Factory routing canary engine and dispatcher."""

from pathlib import Path

import pytest

from howlplane.control_plane.factory.canary import (
    CanaryDispatcherAdapter,
    CanaryEngine,
    canary_evidence_for_workspace,
)
from howlplane.control_plane.factory.target import Workspace
from howlplane.control_plane.factory.work_item import WorkItem


def _work_item(repository: str) -> WorkItem:
    return WorkItem(
        work_item_id=f"WI-{repository.replace('/', '-')}-0000000000000000",
        fingerprint="0000000000000000",
        origin="owner_direction",
        repository=repository,
        title=f"Canary for {repository}",
        description="x",
        kind="factory_routing_canary",
        state="ready",
        evidence_fingerprints=[f"canary:{repository}"],
    )


def _engine(tmp_path: Path, target_rel: str, slug: str, allowed_rel: str) -> CanaryEngine:
    target = tmp_path / target_rel
    target.mkdir(parents=True)
    return CanaryEngine(
        target_repo=target,
        repo_slug=slug,
        allowed_marker_parents=[tmp_path / allowed_rel],
    )


@pytest.mark.parametrize(
    "target_rel, allowed_rel, item_repo, expected_success, expected",
    [
        ("worktree/target", "worktree", "owner/repo", True, "canary"),
        ("outside/target", "worktree", "owner/repo", False, "CANARY_TARGET_NOT_MANAGED"),
        ("worktree/target", "worktree", "owner/other", False, "CANARY_REPOSITORY_MISMATCH"),
    ],
)
def test_canary_engine_behavior(tmp_path, target_rel, allowed_rel, item_repo, expected_success, expected):
    engine = _engine(tmp_path, target_rel, "owner/repo", allowed_rel)
    item = _work_item(item_repo)

    success, record = engine.execute_factory_work_item(item, dispatch_id="d1")

    assert success is expected_success
    if expected_success:
        assert record["integration_mode"] == expected
    else:
        assert record["failure_reason"] == expected


def test_canary_dispatcher_routes_to_matching_engine(tmp_path):
    target_a = tmp_path / "a" / "target"
    target_b = tmp_path / "b" / "target"
    target_a.mkdir(parents=True)
    target_b.mkdir(parents=True)
    dispatcher = CanaryDispatcherAdapter(
        {
            "owner/a": CanaryEngine(target_repo=target_a, repo_slug="owner/a", allowed_marker_parents=[tmp_path]),
            "owner/b": CanaryEngine(target_repo=target_b, repo_slug="owner/b", allowed_marker_parents=[tmp_path]),
        }
    )

    outcome = dispatcher.dispatch(_work_item("owner/b"), dispatch_id="d1", task_id="t1")

    assert outcome.success is True
    assert outcome.reason == "canary_routing_verified"
    assert not (target_b / ".factory_canary").exists()


def test_canary_dispatcher_reports_missing_engine():
    dispatcher = CanaryDispatcherAdapter({})
    outcome = dispatcher.dispatch(_work_item("owner/missing"), dispatch_id="d1", task_id="t1")
    assert outcome.success is False
    assert outcome.failure_code == "canary_engine_not_found"


def test_canary_evidence_for_workspace_returns_one_per_repository(tmp_path):
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    ws = Workspace.from_dict(
        {
            "repositories": [
                {"path": str(repo_a), "repository": "owner/a"},
                {"path": str(repo_b), "repository": "owner/b"},
            ]
        }
    )
    evidence = canary_evidence_for_workspace(ws)

    assert len(evidence) == 2
    assert {e["repository"] for e in evidence} == {"owner/a", "owner/b"}
    assert all(e["origin"] == "owner_direction" for e in evidence)

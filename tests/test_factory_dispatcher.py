#!/usr/bin/env python3
"""Tests for the dispatcher adapter that hands factory work to the governed lifecycle."""

import pytest

from src.control_plane.factory.dispatcher import DispatchOutcome, MarathonDispatcherAdapter
from src.control_plane.factory.work_item import WorkItem, WorkItemState, WorkItemOrigin


class FakeEngine:
    def __init__(self, result):
        self.result = result
        self.last_work_item = None
        self.last_files_changed = None
        self.dispatch_ids = []

    def execute_factory_work_item(self, work_item, files_changed=None, dispatch_id=None):
        self.last_work_item = work_item
        self.last_files_changed = files_changed
        self.dispatch_ids.append(dispatch_id)
        return self.result


def _work_item():
    return WorkItem.create(
        origin=WorkItemOrigin.EXISTING_BACKLOG,
        repository="howlcipher/howlplane",
        title="test item",
        identity_keys=["bugs.md", "1"],
    )


def test_adapter_returns_shipped_on_success():
    engine = FakeEngine((True, {"provider": "codex", "merged": True}))
    adapter = MarathonDispatcherAdapter(engine_factory=lambda: engine)
    item = _work_item()
    outcome = adapter.dispatch(item, dispatch_id="D-1", task_id="T-1")
    assert isinstance(outcome, DispatchOutcome)
    assert outcome.success is True
    assert outcome.next_work_item_state == WorkItemState.SHIPPED
    assert outcome.work_item_id == item.work_item_id
    assert outcome.task_id == "T-1"
    assert outcome.dispatch_id == "D-1"
    assert engine.last_work_item is item


@pytest.mark.parametrize(
    "evidence, expected_state, expected_flags",
    [
        (
            {"integration_mode": "parked"},
            WorkItemState.AWAITING_OWNER,
            {"requires_authority": True, "blocker": "authority_boundary"},
        ),
        (
            {
                "failure_reason": "NO_ELIGIBLE_PROVIDER_REMAINING: tried codex",
                "failure_class": "PROVIDER_EXHAUSTED",
            },
            WorkItemState.DEFERRED,
            {"provider_unavailable": True, "blocker": "provider_unavailable"},
        ),
        (
            {
                "failure_reason": "BLOCKED: missing dependency",
                "failure_class": "DEPENDENCY_BLOCKED",
            },
            WorkItemState.BLOCKED,
            {"blocker": "dependency"},
        ),
        (
            {"failure_reason": "orchestrator_final_state:failed"},
            WorkItemState.FAILED,
            {},
        ),
    ],
)
def test_adapter_classifies_unsuccessful_dispatch(evidence, expected_state, expected_flags):
    engine = FakeEngine((False, evidence))
    outcome = MarathonDispatcherAdapter(engine_factory=lambda: engine).dispatch(
        _work_item(), dispatch_id="D-failure", task_id="T-failure"
    )

    assert outcome.success is False
    assert outcome.next_work_item_state == expected_state
    for attribute, expected in expected_flags.items():
        assert getattr(outcome, attribute) == expected


def test_adapter_extracts_files_changed_from_evidence_refs():
    engine = FakeEngine((True, {"provider": "codex"}))
    adapter = MarathonDispatcherAdapter(engine_factory=lambda: engine)
    item = _work_item()
    item.evidence_refs = ["src/foo.py#1", "src/bar.py#2"]
    adapter.dispatch(item, dispatch_id="D-6", task_id="T-6")
    assert sorted(engine.last_files_changed or []) == ["src/bar.py", "src/foo.py"]


def test_adapter_extracts_files_changed_from_source_ref():
    engine = FakeEngine((True, {"provider": "codex"}))
    adapter = MarathonDispatcherAdapter(engine_factory=lambda: engine)
    item = _work_item()
    item.source_ref = {"source_file": "src/control_plane/factory/supervisor.py"}
    adapter.dispatch(item, dispatch_id="D-7", task_id="T-7")
    assert "src/control_plane/factory/supervisor.py" in (engine.last_files_changed or [])


def test_dispatch_id_reaches_the_engine_so_retries_get_distinct_trajectories():
    """Two dispatches of one work item must be distinguishable downstream.

    Found by a live canary. Trajectory identity falls back to the run
    directory, and the factory reuses one directory per work item
    (`.task_runs/<work_item_id>`), so a second dispatch derived the id the
    first had already written. The trajectory store is deliberately immutable
    -- identical content is an idempotent no-op, differing content raises -- so
    the retry died with "Artifact 'traj-...' already exists with different
    content_digest" before reaching a provider, turning a routine retryable
    provider failure into an orchestrator exception and a backoff.

    The dispatcher already had a unique dispatch_id per dispatch and simply
    never passed it on.
    """
    engine = FakeEngine((True, {"provider": "codex", "merged": True}))
    adapter = MarathonDispatcherAdapter(engine_factory=lambda: engine)
    item = _work_item()

    adapter.dispatch(item, dispatch_id="D-1", task_id="T-1")
    adapter.dispatch(item, dispatch_id="D-2", task_id="T-1")

    assert engine.dispatch_ids == ["D-1", "D-2"], (
        "the engine cannot tell two dispatches of the same work item apart"
    )

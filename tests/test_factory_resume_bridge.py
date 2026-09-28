#!/usr/bin/env python3
"""
tests/test_factory_resume_bridge.py

Contract tests for the marathon side of the orchestration human-boundary
bridge: an approved park is resumed in its own run and never failed over into
a fresh duplicate run; delegation never covers review gates or
NEVER_DELEGATABLE triggers; an implementation that changed nothing reaches the
Factory as a FAILED item, not an owner decision. No provider is invoked.
"""

from pathlib import Path
from typing import Any, List

import pytest

from howlplane.control_plane.authority_envelope import create_envelope
from howlplane.control_plane.authority_profile import get_profile
from howlplane.control_plane.factory.dispatcher import MarathonDispatcherAdapter
from howlplane.control_plane.factory.work_item import WorkItem, WorkItemOrigin, WorkItemState
from howlplane.control_plane.human_boundary import HumanLifecycleManager
from howlplane.control_plane.orchestrator import OrchestrationResult
from howlplane.control_plane.synthesis import MarathonDogfoodEngine
from howlplane.control_plane.synthesis.provider_pool import (
    ProviderAvailabilityStatus,
    ProviderPoolManager,
)
from howlplane.control_plane.task_spec import TaskSpec
from tests._dogfood_test_helpers import init_minimal_python_repo


class RecordingOrchestrator:
    """Fails the test's intent if a fresh run is ever started."""

    def __init__(self):
        self.runs: List[Any] = []

    def run(self, task, planned_actions=None, **kwargs):
        self.runs.append(task.task_id)
        return OrchestrationResult(task_id=task.task_id, task_spec=task, final_state="failed", exit_code=1)


def _engine(tmp_path: Path, orchestrator: RecordingOrchestrator) -> MarathonDogfoodEngine:
    repo = tmp_path / "repo"
    init_minimal_python_repo(repo)
    pool = ProviderPoolManager()
    for agent_id in list(pool.get_all_statuses()):
        pool.set_status(agent_id, ProviderAvailabilityStatus.AVAILABLE)
    engine = MarathonDogfoodEngine(
        provider_pool=pool,
        base_output_dir=tmp_path / "out",
        campaign_dir=tmp_path / "campaigns",
        target_repo=repo,
        repo_slug="howlcipher/howlplane",
        orchestrator_factory=lambda config: orchestrator,
    )
    engine.authority_envelope = create_envelope(get_profile("overnight-safe"), "C-1", "cli:test@host")
    engine.git_executor = object()  # never reached: the resume does not complete
    return engine


def _item(resume: str = None) -> WorkItem:
    item = WorkItem.create(
        origin=WorkItemOrigin.EXISTING_BACKLOG, repository="howlcipher/howlplane",
        title="backlog 61", identity_keys=["61"],
    )
    item.resume_orchestration_task_id = resume
    return item


def test_non_complete_resume_is_terminal_and_never_starts_a_fresh_run(tmp_path, monkeypatch):
    orchestrator = RecordingOrchestrator()
    engine = _engine(tmp_path, orchestrator)
    item = _item(resume="WI-howlplane-parked")
    resumed = []

    def fake_resume(cls, target_repo, task_id, orchestrator=None, ledger=None, control_plane_root=None):
        resumed.append(task_id)
        spec = TaskSpec(task_id=task_id, repository="howlcipher/howlplane", objective="x")
        return OrchestrationResult(
            task_id=task_id, task_spec=spec, final_state="failed", exit_code=1,
            failure_class="PROVIDER_EXHAUSTED",
        )

    monkeypatch.setattr(HumanLifecycleManager, "resume", classmethod(fake_resume))
    ok, record = engine.execute_factory_work_item(item, dispatch_id="D-1")

    assert ok is False
    assert resumed == ["WI-howlplane-parked"]
    assert orchestrator.runs == []
    assert record["failure_reason"] == "orchestrator_final_state:failed"


def test_refused_resume_fails_closed_with_its_reason(tmp_path, monkeypatch):
    orchestrator = RecordingOrchestrator()
    engine = _engine(tmp_path, orchestrator)

    def stale(cls, **kwargs):
        raise RuntimeError("Repository state has drifted since approval was granted")

    monkeypatch.setattr(HumanLifecycleManager, "resume", classmethod(lambda cls, **kw: stale(cls, **kw)))
    ok, record = engine.execute_factory_work_item(_item(resume="WI-parked"), dispatch_id="D-2")

    assert ok is False
    assert record["failure_code"] == "orchestration_resume_failed"
    assert "drifted" in record["failure_reason"]
    assert record["resumed_orchestration_task_id"] == "WI-parked"
    assert orchestrator.runs == []


@pytest.mark.parametrize("triggers, no_changes, expected", [
    (["independent_review_unavailable"], False, False),
    (["non_independent_review"], False, False),
    (["force_push"], False, False),
    (["merge_pull_request"], True, False),
    (["merge_pull_request"], False, True),
    ([], False, False),
])
def test_delegation_never_covers_review_gates_or_non_delegatable_triggers(
    tmp_path, triggers, no_changes, expected,
):
    engine = _engine(tmp_path, RecordingOrchestrator())
    boundary = {"triggers": triggers, "implementation_no_changes": no_changes}
    assert engine._boundary_fully_delegated(boundary) is expected
    engine.authority_envelope = None
    assert engine._boundary_fully_delegated(boundary) is False


def test_no_changes_summary_requires_every_attempt_to_be_empty():
    empty = {"resource_id": "codex", "failure_class": "PROVIDER_STALLED", "delta": {"files_modified": []}}
    changed = {"resource_id": "claude_code", "delta": {"files_modified": ["x.py"]}}

    def result(attempts):
        return type("R", (), {"implementation_attempts": attempts})()

    assert MarathonDogfoodEngine._implementation_no_changes_summary(result([empty])) == "codex: PROVIDER_STALLED"
    assert MarathonDogfoodEngine._implementation_no_changes_summary(result([empty, changed])) is None
    assert MarathonDogfoodEngine._implementation_no_changes_summary(result([])) is None


def test_dispatcher_maps_no_changes_to_failed_not_owner_review():
    class Engine:
        def execute_factory_work_item(self, item, files_changed=None, dispatch_id=None, run_mode="continuous"):
            return False, {
                "failure_reason": "implementation_no_changes: codex: PROVIDER_STALLED",
                "failure_class": "PROVIDER_EXHAUSTED",
                "failure_code": "implementation_no_changes",
            }

    outcome = MarathonDispatcherAdapter(lambda: Engine()).dispatch(_item(), dispatch_id="D-3", task_id="T-3")
    assert outcome.next_work_item_state == WorkItemState.FAILED
    assert outcome.requires_authority is False and outcome.provider_unavailable is False
    assert outcome.reason.startswith("implementation_no_changes")

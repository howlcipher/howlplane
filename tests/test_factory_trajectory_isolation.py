#!/usr/bin/env python3
"""Deterministic regression tests for trajectory identity and evidence persistence (#B10)."""

from pathlib import Path

import pytest

from src.control_plane.agent_execution import FakeAgentBackend
from src.control_plane.atomic_io import safe_load_json
from src.control_plane.authority_envelope import create_envelope
from src.control_plane.authority_profile import get_profile
from src.control_plane.reasoning.artifact_safety import canonical_digest
from src.control_plane.factory.dispatcher import MarathonDispatcherAdapter
from src.control_plane.factory.supervisor import FactorySupervisor
from src.control_plane.factory.supervisor_state import SupervisorState, SupervisorStateStore
from src.control_plane.factory.work_item import WorkItemState, WorkItemStore
from src.control_plane.git_integration import GitIntegrationExecutor
from src.control_plane.orchestrator import GovernedTaskOrchestrator, OrchestrationConfig
from src.control_plane.reasoning.execution_trajectory import ExecutionTrajectory, TrajectoryStore
from src.control_plane.synthesis.marathon import MarathonDogfoodEngine
from src.control_plane.synthesis.provider_pool import ProviderPoolManager
from tests.test_local_ollama_provider import _AlwaysSucceedGhRunner, _AlwaysSucceedGitRunner
from tests._dogfood_test_helpers import init_minimal_python_repo
from tests._factory_test_helpers import make_supervisor, ready_work_item


def _setup_engine(
    repo: Path, shared_traj_dir: Path, campaign_name: str, campaign_dir: Path
) -> MarathonDogfoodEngine:
    pool = ProviderPoolManager.from_config(probe_on_start=False)
    backend = FakeAgentBackend(
        agent_id="claude_code",
        side_effect=lambda task, cwd, prompt: (cwd / "src" / "feature.py").write_text(
            f"def run():\n    # {task.task_id} {campaign_name}\n    return True\n"
        ),
    )

    def orch_factory(config: OrchestrationConfig) -> GovernedTaskOrchestrator:
        config.trajectory_store_dir = shared_traj_dir
        config.custom_backend = backend
        config.custom_reviewer_fn = lambda role, diff, task: "findings: []\n"
        config.acquire_locks = False
        config.enable_howlframe_audit = False
        return GovernedTaskOrchestrator(repo, config=config)

    engine = MarathonDogfoodEngine(
        provider_pool=pool,
        target_repo=repo,
        repo_slug="howlcipher/howlplane",
        campaign_dir=campaign_dir,
        orchestrator_factory=orch_factory,
    )
    env = create_envelope(get_profile("overnight-safe"), campaign_name, "cli:test@host")
    engine.authority_envelope = env
    engine.git_executor = GitIntegrationExecutor(
        repo, "howlcipher/howlplane", env, git_runner=_AlwaysSucceedGitRunner(), gh_runner=_AlwaysSucceedGhRunner()
    )
    return engine


def test_distinct_state_dirs_produce_distinct_trajectory_identities_and_idempotent_rerun(tmp_path: Path):
    """Regression (A): distinct state dirs produce distinct trajectory identities;

    the same dispatch attempt re-run produces the same identity (idempotent no-op preserved).
    """
    repo = init_minimal_python_repo(tmp_path / "target_repo")
    shared_traj_dir = tmp_path / "shared_trajectories"

    # State dir 1
    state_dir_1 = tmp_path / "state_1"
    engine1 = _setup_engine(repo, shared_traj_dir, "CAMPAIGN-1", tmp_path / "campaign_1")
    sup1, _, _ = make_supervisor(
        state_dir_1,
        dispatcher=MarathonDispatcherAdapter(lambda: engine1),
        pool=engine1.provider_pool,
        state_dir=state_dir_1,
    )
    item1 = ready_work_item(sup1.work_item_store, title="task_a", identity_keys=["task_a"])
    item_id = item1.work_item_id

    res1 = sup1.tick()
    assert res1.state == SupervisorState.IDLE or res1.selected_work_item_id == item_id
    dispatch_id_1 = sup1.state_record.last_work_item_id
    assert sup1.instance_id is not None
    assert sup1.instance_id in sup1.state_record.dispatch_history[0]["dispatch_id"]

    trajectories_after_1 = sorted(p.name for p in shared_traj_dir.glob("*.json"))
    assert len(trajectories_after_1) == 1
    traj_1_name = trajectories_after_1[0]

    # State dir 2 (fresh state dir, same work item)
    state_dir_2 = tmp_path / "state_2"
    engine2 = _setup_engine(repo, shared_traj_dir, "CAMPAIGN-2", tmp_path / "campaign_2")
    sup2, _, _ = make_supervisor(
        state_dir_2,
        dispatcher=MarathonDispatcherAdapter(lambda: engine2),
        pool=engine2.provider_pool,
        state_dir=state_dir_2,
    )
    assert sup2.instance_id != sup1.instance_id, (
        f"Distinct state dirs must have distinct instance_ids: {sup1.instance_id} vs {sup2.instance_id}"
    )

    item1_saved = sup1.work_item_store.load(item_id)
    item1_saved.state = WorkItemState.READY
    item1_saved.attempts = 0
    sup2.work_item_store.save_object(item1_saved)

    res2 = sup2.tick()
    assert res2.state != SupervisorState.BACKOFF_AFTER_FAILURE, (
        f"State dir 2 dispatch failed: {sup2.state_record.last_error}"
    )
    assert sup2.state_record.failure_count == 0

    dispatch_id_2 = sup2.state_record.dispatch_history[0]["dispatch_id"]
    assert dispatch_id_1 != dispatch_id_2
    assert sup2.instance_id in dispatch_id_2

    trajectories_after_2 = sorted(p.name for p in shared_traj_dir.glob("*.json"))
    assert len(trajectories_after_2) == 2, (
        f"Expected 2 distinct trajectory files in shared store, found: {trajectories_after_2}"
    )
    assert traj_1_name in trajectories_after_2

    # Verify idempotence within a single dispatch attempt: re-loading supervisor preserves instance_id
    reloaded_sup1 = FactorySupervisor(
        state_store=SupervisorStateStore(state_dir_1 / "supervisor"),
        work_item_store=WorkItemStore(state_dir_1 / "work_items"),
        repo_proposal_store=sup1.repo_proposal_store,
        capability_store=sup1.capability_registry._store,
        dispatcher=sup1.dispatcher,
        discovery=sup1.discovery,
        provider_pool=sup1.provider_pool,
        state_dir=state_dir_1,
    )
    assert reloaded_sup1.instance_id == sup1.instance_id

    # Re-saving identical trajectory into store is an idempotent no-op
    store = TrajectoryStore(shared_traj_dir)
    t1_id = traj_1_name.replace(".json", "")
    existing_t1 = store.load(t1_id)
    saved_path = store.save(existing_t1)
    assert saved_path == shared_traj_dir / traj_1_name
    assert len(list(shared_traj_dir.glob("*.json"))) == 2


def test_evidence_store_write_conflict_does_not_fail_dispatch(tmp_path: Path):
    """Regression (B): an evidence-store write conflict does not produce

    ENGINEERING_FAILURE and does not stop the dispatch.
    """
    repo = init_minimal_python_repo(tmp_path / "target_repo")
    shared_traj_dir = tmp_path / "shared_trajectories"
    state_dir = tmp_path / "state"

    engine = _setup_engine(repo, shared_traj_dir, "CAMPAIGN-B", tmp_path / "campaign_b")
    sup, _, _ = make_supervisor(
        state_dir,
        dispatcher=MarathonDispatcherAdapter(lambda: engine),
        pool=engine.provider_pool,
        state_dir=state_dir,
    )
    item = ready_work_item(sup.work_item_store, title="conflict_task", identity_keys=["conflict_task"])

    # Predict the trajectory identity for this dispatch attempt:
    # dispatch_id = f"D-{item.work_item_id}-{sup.instance_id}-0"
    # trajectory_event_id = f"{dispatch_id}:1"
    dispatch_id = f"D-{item.work_item_id}-{sup.instance_id}-0"
    event_key = f"{dispatch_id}:1"
    trajectory_id = f"traj-{canonical_digest({'event': event_key})[:24]}"

    # Pre-seed the shared trajectory store with a conflicting artifact
    shared_traj_dir.mkdir(parents=True, exist_ok=True)
    conflicting_file = shared_traj_dir / f"{trajectory_id}.json"
    conflicting_file.write_text('{"trajectory_id": "' + trajectory_id + '", "content_digest": "conflict-12345"}', encoding="utf-8")

    # Run the dispatch. The provider succeeds, but persisting the trajectory encounters
    # a FileExistsError write conflict.
    res = sup.tick()

    # The dispatch must not surface as orchestrator_exception or ENGINEERING_FAILURE
    assert res.state != SupervisorState.BACKOFF_AFTER_FAILURE, (
        f"Evidence persistence conflict caused supervisor failure: {sup.state_record.last_error}"
    )
    assert sup.state_record.failure_count == 0
    assert "already exists with different" not in str(sup.state_record.last_error)
    assert "orchestrator_exception" not in str(sup.state_record.last_error)

    # Work item must have completed successfully
    loaded_item = sup.work_item_store.load(item.work_item_id)
    assert loaded_item.state == WorkItemState.SHIPPED

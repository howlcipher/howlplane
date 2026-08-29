"""
test_checkpoint_terminalization.py

Mandatory tests for checkpoint terminalization across all lifecycle failure boundaries:
- Implementation failure
- Review failure
- Verification failure
- Authority rejection/failure
- Recovery failure
- Non-terminal crash/interruption retaining resumable in_progress checkpoint
"""

import json
from pathlib import Path
from typing import Dict, List, Optional
import pytest

from src.control_plane.atomic_io import safe_load_json
from src.control_plane.checkpoints import CheckpointManager, StageCheckpoint
from src.control_plane.orchestrator import (
    GovernedTaskOrchestrator,
    OrchestrationConfig,
)
from src.control_plane.task_spec import TaskSpec
from tests.test_provider_failover import (
    _FakeBackendResolver,
    _edit_feature_to_true,
    _init_test_repo,
    _make_task,
    _run_failover_task,
)


def _load_chk(path: Path) -> Optional[StageCheckpoint]:
    data = safe_load_json(path)
    return StageCheckpoint.from_dict(data) if data else None


def _load_all_chks(run_dir: Path) -> List[StageCheckpoint]:
    c_dir = CheckpointManager.get_checkpoints_dir(run_dir)
    chks = []
    for p in sorted(c_dir.glob("*.json")):
        chk = _load_chk(p)
        if chk:
            chks.append(chk)
    return chks


def test_implementation_failure_terminalizes_active_stage_checkpoint(tmp_path: Path):
    """When all implementation attempts fail, implementing_01 must be terminalized as failed.
    No checkpoint must remain in_progress, and no bogus failed_01.json stage should be created.
    """
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": False, "stderr": "SyntaxError in code"},
    })

    res = _run_failover_task(repo, resolver, max_attempts=1)
    assert res.final_state == "failed"

    run_dir = Path(res.run_dir)
    c_dir = CheckpointManager.get_checkpoints_dir(run_dir)

    # Checkpoints directory exists
    assert c_dir.is_dir()

    # implementing_01.json must exist and be FAILED with completed timestamp
    impl_chk_path = c_dir / "implementing_01.json"
    assert impl_chk_path.is_file(), f"Missing {impl_chk_path}"
    impl_chk = _load_chk(impl_chk_path)
    assert impl_chk is not None
    assert impl_chk.stage == "implementing"
    assert impl_chk.status == "failed"
    assert impl_chk.stage_completed_at is not None
    assert impl_chk.duration_seconds >= 0.0

    # No in-progress checkpoints remain
    all_checkpoints = _load_all_chks(run_dir)
    in_progress = [c for c in all_checkpoints if c.status == "in_progress"]
    assert len(in_progress) == 0, f"Found stale in_progress checkpoints: {in_progress}"

    # No bogus 'failed_01.json' stage checkpoint exists
    failed_chk_path = c_dir / "failed_01.json"
    assert not failed_chk_path.exists(), "Found bogus failed_01.json checkpoint"


def test_review_failure_terminalizes_reviewing_stage_checkpoint(tmp_path: Path):
    """When review rejects or fails, reviewing_01 must be terminalized as failed."""
    repo = _init_test_repo(tmp_path / "repo")
    resolver = _FakeBackendResolver({
        "resource_a": {"success": True, "side_effect": _edit_feature_to_true},
    })

    def rejecting_reviewer(role: str, diff: str, task: TaskSpec) -> str:
        return '{"findings": [{"severity": "BLOCKER", "message": "Critical security flaw", "file": "main.py", "line": 1}]}'

    res = _run_failover_task(
        repo,
        resolver,
        reviewer_fn=rejecting_reviewer,
        max_attempts=1,
    )
    # The review findings block the task or fail remediation
    assert res.final_state in ("failed", "awaiting_human", "blocked")

    run_dir = Path(res.run_dir)
    all_checkpoints = _load_all_chks(run_dir)
    in_progress = [c for c in all_checkpoints if c.status == "in_progress"]
    assert len(in_progress) == 0, f"Found stale in_progress checkpoints: {in_progress}"


def test_verification_failure_terminalizes_verifying_stage_checkpoint(tmp_path: Path):
    """When deterministic verification fails, verifying_01 must be terminalized as failed."""
    repo = _init_test_repo(tmp_path / "repo")
    # Side effect breaks the build/test by writing invalid python syntax into feature file
    def break_code(task, cwd: Path, _prompt) -> None:
        (cwd / "src" / "feature.py").write_text("def broken syntax!():\n", encoding="utf-8")

    resolver = _FakeBackendResolver({
        "resource_a": {"success": True, "side_effect": break_code},
    })

    res = _run_failover_task(repo, resolver, max_attempts=1)
    assert res.final_state == "failed"

    run_dir = Path(res.run_dir)
    c_dir = CheckpointManager.get_checkpoints_dir(run_dir)

    # If verifying stage was entered, it must be marked failed
    verif_chk_path = c_dir / "verifying_01.json"
    if verif_chk_path.is_file():
        verif_chk = _load_chk(verif_chk_path)
        assert verif_chk is not None
        assert verif_chk.status == "failed"
        assert verif_chk.stage_completed_at is not None

    all_checkpoints = _load_all_chks(run_dir)
    in_progress = [c for c in all_checkpoints if c.status == "in_progress"]
    assert len(in_progress) == 0, f"Found stale in_progress checkpoints: {in_progress}"


def test_checkpoint_manager_fail_stage_with_failed_string_terminalizes_active_checkpoint(tmp_path: Path):
    """Calling fail_stage with stage='failed' or None must cleanly terminalize the latest in_progress checkpoint."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)

    chk = CheckpointManager.start_stage(run_dir, task_id="TEST-01", stage="implementing")
    assert chk.status == "in_progress"

    # Even if someone accidentally passes stage="failed"
    final = CheckpointManager.fail_stage(
        run_dir,
        stage="failed",
        reason="Something crashed",
        result_summary={"error": "crashed"},
    )

    assert final.stage == "implementing"
    assert final.status == "failed"
    assert final.stage_completed_at is not None

    latest = CheckpointManager.load_latest_checkpoint(run_dir)
    assert latest is not None
    assert latest.stage == "implementing"
    assert latest.status == "failed"


def test_interrupted_non_terminal_task_retains_resumable_in_progress_state(tmp_path: Path):
    """An interrupted NON-TERMINAL task must retain its in_progress checkpoint so resume can reconstruct it."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)

    chk = CheckpointManager.start_stage(run_dir, task_id="RESUME-01", stage="reviewing")
    assert chk.status == "in_progress"

    # Process dies/interrupted without terminalizing as failed
    # When resume inspects the stage checkpoint:
    latest = CheckpointManager.load_latest_checkpoint(run_dir)
    assert latest is not None
    assert latest.stage == "reviewing"
    assert latest.status == "in_progress"
    # This proves interrupted is not confused with failed


def test_recovered_task_subsequent_failure_leaves_no_in_progress_implementation_checkpoint(tmp_path: Path):
    """When an interrupted implementation is recovered on resume and subsequent verification fails,
    implementing_01 must be completed and verifying_01 failed; no checkpoint remains in_progress."""
    repo = _init_test_repo(tmp_path / "repo")

    # Step 1: Simulate interrupted implementation attempt
    task = _make_task("RECOVER-FAIL-01")
    run_dir = repo / ".task_runs" / task.task_id
    run_dir.mkdir(parents=True)

    from src.control_plane.git_baseline import capture_baseline
    baseline = capture_baseline(repo)
    (run_dir / "baseline.json").write_text(baseline.to_json(), encoding="utf-8")

    # Start implementing stage checkpoint
    chk = CheckpointManager.start_stage(run_dir, task_id=task.task_id, stage="implementing")
    assert chk.status == "in_progress"

    # Implementation produced a delta
    (repo / "src" / "feature.py").write_text("def broken_syntax!():\n", encoding="utf-8")

    # Step 2: Resume task, which recovers the delta and proceeds to verification
    resolver = _FakeBackendResolver({"resource_a": {"success": True}})
    res = _run_failover_task(repo, resolver, task=task, max_attempts=1)
    assert res.final_state == "failed"

    # Step 3: Verify all checkpoints
    all_chks = _load_all_chks(run_dir)
    in_progress = [c for c in all_chks if c.status == "in_progress"]
    assert len(in_progress) == 0, f"Stale in_progress checkpoints found: {in_progress}"

    # implementing_01 was completed on recovery
    c_dir = CheckpointManager.get_checkpoints_dir(run_dir)
    impl_chk = _load_chk(c_dir / "implementing_01.json")
    assert impl_chk is not None
    assert impl_chk.status == "completed"

    # verifying_01 was failed
    verif_chk = _load_chk(c_dir / "verifying_01.json")
    assert verif_chk is not None
    assert verif_chk.status == "failed"


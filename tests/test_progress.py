#!/usr/bin/env python3
"""
test_progress.py

Comprehensive deterministic test suite for operator-visible progress and
heartbeat tracking across governed HowlPlane operations.
"""

from datetime import datetime, timezone
import io
import os
from pathlib import Path
import subprocess
import time
from typing import Optional

from src.control_plane.agent_execution import (
    AgentBackend,
    AgentExecutionResult,
)
from src.control_plane.atomic_io import safe_load_json
from src.control_plane.launcher import (
    build_parser,
    cmd_status,
    cmd_work,
)
from src.control_plane.orchestrator import (
    GovernedTaskOrchestrator,
    OrchestrationConfig,
)
from src.control_plane.progress import (
    PROGRESS_SCHEMA_VERSION,
    TaskPhase,
    TaskProgressRecord,
    TaskProgressState,
    TaskProgressTracker,
    format_elapsed,
    format_last_heartbeat,
)
from src.control_plane.task_spec import TaskSpec
from src.control_plane.verification import VerificationPlan


def _init_test_git_repo(repo_path: Path) -> None:
    """Initializes a minimal git repository for testing."""
    str_path = str(repo_path)
    subprocess.run(
        ["git", "init", "-b", "main", str_path],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str_path, "config", "user.name", "Test Runner"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str_path, "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    (repo_path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str_path, "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str_path, "commit", "-m", "Initial commit"],
        check=True,
        capture_output=True,
    )


class SlowMockBackend(AgentBackend):
    """Mock agent backend that sleeps to simulate a long-running provider."""

    def __init__(
        self,
        agent_id: str = "mock_slow",
        sleep_duration: float = 0.25,
        success: bool = True,
    ):
        self.agent_id = agent_id
        self.sleep_duration = sleep_duration
        self.success = success
        self.call_count = 0

    def execute(
        self,
        task: TaskSpec,
        cwd: Path,
        role: str = "implementation",
        prompt_override: Optional[str] = None,
        timeout_seconds: int = 600,
        **kwargs,
    ) -> AgentExecutionResult:
        self.call_count += 1
        time.sleep(self.sleep_duration)
        if role == "implementation":
            (cwd / "feature.py").write_text(
                "# implemented\n", encoding="utf-8"
            )
        return AgentExecutionResult(
            agent_id=self.agent_id,
            role=role,
            command=f"slow_mock({self.agent_id})",
            success=self.success,
            exit_code=0 if self.success else 1,
            stdout="OK" if self.success else "",
            stderr="" if self.success else "Failed execution",
            duration_seconds=self.sleep_duration,
        )

    def is_available(self) -> bool:
        return True


# -----------------------------------------------------------------------------
# 1. Format Helper Unit Tests
# -----------------------------------------------------------------------------


def test_format_elapsed():
    """Verify wall-clock elapsed time formatting."""
    assert format_elapsed(0) == "00:00:00"
    assert format_elapsed(5) == "00:00:05"
    assert format_elapsed(30) == "00:00:30"
    assert format_elapsed(77) == "00:01:17"
    assert format_elapsed(3665) == "01:01:05"
    assert format_elapsed(-10) == "00:00:00"


def test_format_last_heartbeat():
    """Verify relative heartbeat freshness formatting."""
    assert format_last_heartbeat(None) == "unknown"
    assert format_last_heartbeat("") == "unknown"
    assert format_last_heartbeat("invalid-date") == "unknown"

    now_ts = 1700000000.0

    # 7 seconds ago
    dt_7s = datetime.fromtimestamp(now_ts - 7, timezone.utc).isoformat()
    assert format_last_heartbeat(dt_7s, now_ts=now_ts) == "7s ago"

    # 2 minutes ago
    dt_2m = datetime.fromtimestamp(now_ts - 125, timezone.utc).isoformat()
    assert format_last_heartbeat(dt_2m, now_ts=now_ts) == "2m ago"

    # 3 hours ago
    dt_3h = datetime.fromtimestamp(now_ts - 10800, timezone.utc).isoformat()
    assert format_last_heartbeat(dt_3h, now_ts=now_ts) == "3h ago"


# -----------------------------------------------------------------------------
# 2. TaskProgressTracker Unit Tests (Immediate Transitions & Heartbeats)
# -----------------------------------------------------------------------------


def test_tracker_immediate_transition_and_heartbeat(tmp_path):
    """Verify immediate output and periodic heartbeats during operations."""
    stream = io.StringIO()
    run_dir = tmp_path / ".task_runs" / "TASK-001"
    tracker = TaskProgressTracker(
        task_id="TASK-001",
        run_dir=run_dir,
        stream=stream,
        heartbeat_interval=0.08,
    )

    tracker.start(task_id="TASK-001", run_dir=run_dir)
    assert "[HowlPlane] Task TASK-001 started\n" in stream.getvalue()

    # Blocking operation with heartbeats
    with tracker.operation(
        phase=TaskPhase.IMPLEMENTING,
        resource_id="agy",
        role="implementation",
        details="started",
    ):
        time.sleep(0.25)

    out = stream.getvalue()
    assert "[HowlPlane] IMPLEMENTING | agy | started" in out
    assert "[HowlPlane] IMPLEMENTING | agy | elapsed" in out
    assert "still working" in out
    assert "[HowlPlane] IMPLEMENTATION COMPLETE | agy | elapsed" in out

    # Verify durable atomic progress file
    progress_file = run_dir / "progress.json"
    assert progress_file.is_file()
    data = safe_load_json(progress_file)
    assert data["task_id"] == "TASK-001"
    assert data["schema"] == PROGRESS_SCHEMA_VERSION
    assert data["phase"] == "IMPLEMENTING"
    assert data["resource_id"] == "agy"

    tracker.record_terminal(TaskProgressState.COMPLETE)
    assert "[HowlPlane] COMPLETE | elapsed" in stream.getvalue()

    data_after = safe_load_json(progress_file)
    assert data_after["state"] == "COMPLETE"
    assert data_after["phase"] == "COMPLETE"

    tracker.close()


def test_tracker_heartbeat_stops_immediately_on_exit(tmp_path):
    """Verify heartbeat thread terminates immediately when op finishes."""
    stream = io.StringIO()
    tracker = TaskProgressTracker(
        task_id="TASK-002",
        run_dir=tmp_path,
        stream=stream,
        heartbeat_interval=0.05,
    )

    with tracker.operation(
        phase=TaskPhase.REVIEWING,
        resource_id="codex",
        details="cycle 1",
    ):
        time.sleep(0.12)
        # Background thread is running
        assert tracker._heartbeat_thread is not None
        assert tracker._heartbeat_thread.is_alive()

    # Heartbeat thread should have joined and stopped
    assert tracker._heartbeat_thread is None
    lines_count_before = len(stream.getvalue().splitlines())

    # Wait another interval to ensure no trailing heartbeats are emitted
    time.sleep(0.1)
    lines_count_after = len(stream.getvalue().splitlines())
    assert lines_count_after == lines_count_before

    tracker.close()


def test_tracker_disabled_mode(tmp_path):
    """Verify tracker in disabled mode emits nothing and starts no threads."""
    stream = io.StringIO()
    tracker = TaskProgressTracker(
        task_id="TASK-003",
        run_dir=tmp_path,
        stream=stream,
        heartbeat_interval=0.05,
        enabled=False,
    )

    tracker.start()
    with tracker.operation(
        phase=TaskPhase.IMPLEMENTING, resource_id="claude_code"
    ):
        time.sleep(0.1)
    tracker.record_terminal(TaskProgressState.COMPLETE)
    tracker.close()

    assert stream.getvalue() == ""
    # Progress file is not created if tracker is disabled
    assert not (tmp_path / "progress.json").exists()


# -----------------------------------------------------------------------------
# 3. Verification Progress & Step Tracking
# -----------------------------------------------------------------------------


def test_verification_plan_progress_tracking(tmp_path):
    """Verify VerificationPlan emits phase transitions for steps."""
    stream = io.StringIO()
    run_dir = tmp_path / ".task_runs" / "TASK-VERIF"
    tracker = TaskProgressTracker(
        task_id="TASK-VERIF",
        run_dir=run_dir,
        stream=stream,
        heartbeat_interval=0.05,
    )

    plan = VerificationPlan(task_id="TASK-VERIF")
    plan.add_step(
        step_id="step_1",
        name="unit_test",
        category="unit_test",
        command="python3 -c \"import time; time.sleep(0.15)\"",
        required=True,
    )
    plan.add_step(
        step_id="step_2",
        name="lint_check",
        category="lint",
        command="python3 -c \"exit(0)\"",
        required=True,
    )

    status = plan.execute_all(cwd=str(tmp_path), progress_tracker=tracker)
    assert status == "passed"
    tracker.close()

    out = stream.getvalue()
    assert "[HowlPlane] VERIFYING | unit_test |" in out
    assert "[HowlPlane] VERIFYING | lint |" in out
    assert "still working" in out


# -----------------------------------------------------------------------------
# 4. Full Governed Orchestrator Progress Integration
# -----------------------------------------------------------------------------


def test_orchestrator_progress_full_governed_cycle(tmp_path):
    """Verify orchestrator runs progress phases with mock provider."""
    repo = tmp_path / "target_repo"
    repo.mkdir()
    _init_test_git_repo(repo)
    (repo / ".ai-project.toml").write_text(
        '[commands]\ntest = ["python3", "-c", "exit(0)"]\n', encoding="utf-8"
    )

    stream = io.StringIO()
    mock_backend = SlowMockBackend(
        agent_id="claude_code", sleep_duration=0.15, success=True
    )

    config = OrchestrationConfig(
        acquire_locks=False,
        skip_doctor=True,
        enable_howlframe_audit=False,
        heartbeat_interval=0.05,
        progress_stream=stream,
        custom_backend=mock_backend,
        custom_reviewer_fn=lambda r, d, t: "```yaml\nfindings: []\n```",
    )

    orchestrator = GovernedTaskOrchestrator(
        target_repo=repo,
        control_plane_root=repo,
        config=config,
    )

    spec = TaskSpec(
        task_id="TASK-FULL-01",
        repository=str(repo),
        objective="Implement feature",
        task_class="feature",
        risk_level="low",
    )

    res = orchestrator.run(spec)
    assert res.exit_code == 0
    assert res.final_state == "complete"

    out = stream.getvalue()
    # Check all phase transitions
    assert "[HowlPlane] Task TASK-FULL-01 started" in out
    assert "[HowlPlane] ROUTING | selecting implementation resource" in out
    assert "[HowlPlane] IMPLEMENTING | claude_code | started" in out
    assert "[HowlPlane] REVIEWING | claude_code | cycle 1" in out
    assert "[HowlPlane] VERIFYING |" in out
    assert "[HowlPlane] COMPLETE | elapsed" in out

    # Verify progress.json was persisted
    prog_file = repo / ".task_runs" / "TASK-FULL-01" / "progress.json"
    assert prog_file.is_file()
    data = safe_load_json(prog_file)
    assert data["state"] == "COMPLETE"
    assert data["phase"] == "COMPLETE"


def test_orchestrator_progress_terminal_failure(tmp_path):
    """Verify progress record correctly transitions to FAILED on error."""
    repo = tmp_path / "target_repo"
    repo.mkdir()
    _init_test_git_repo(repo)

    stream = io.StringIO()
    failing_backend = SlowMockBackend(
        agent_id="claude_code", sleep_duration=0.05, success=False
    )

    config = OrchestrationConfig(
        acquire_locks=False,
        skip_doctor=True,
        enable_howlframe_audit=False,
        heartbeat_interval=0.05,
        progress_stream=stream,
        custom_backend=failing_backend,
    )

    orchestrator = GovernedTaskOrchestrator(
        target_repo=repo,
        control_plane_root=repo,
        config=config,
    )

    spec = TaskSpec(
        task_id="TASK-FAIL-01",
        repository=str(repo),
        objective="Failing task",
        task_class="bug_fix",
        risk_level="low",
    )

    res = orchestrator.run(spec)
    assert res.exit_code != 0
    assert res.final_state == "failed"

    out = stream.getvalue()
    assert "[HowlPlane] FAILED | elapsed" in out

    prog_file = repo / ".task_runs" / "TASK-FAIL-01" / "progress.json"
    assert prog_file.is_file()
    data = safe_load_json(prog_file)
    assert data["state"] == "FAILED"
    assert data["phase"] == "FAILED"


# -----------------------------------------------------------------------------
# 5. `ai status` Integration Tests (Active vs Stale Process Reporting)
# -----------------------------------------------------------------------------


def test_status_reports_active_progress_and_stale_when_process_dead(
    tmp_path, capsys
):
    """Verify ai status reports active progress and marks dead PID STALE."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_test_git_repo(repo)

    run_dir = repo / ".task_runs" / "HOWLFRAM-123456"
    run_dir.mkdir(parents=True, exist_ok=True)

    spec = TaskSpec(
        task_id="HOWLFRAM-123456",
        repository=str(repo),
        objective="Refactor AST node visitor",
        task_class="refactor",
        risk_level="low",
        current_state="implementing",
    )
    spec.save_to_file(str(run_dir / "task.yaml"))

    # 1. Progress file with a dead PID (e.g. PID 999999)
    progress_file = run_dir / "progress.json"
    now_iso = datetime.now(timezone.utc).isoformat()
    prog_record = TaskProgressRecord(
        task_id="HOWLFRAM-123456",
        phase="IMPLEMENTING",
        resource_id="agy",
        role="implementation",
        started_at=now_iso,
        phase_started_at=now_iso,
        updated_at=now_iso,
        elapsed_seconds=134,  # 00:02:14
        state="RUNNING",
        pid=999999,  # Non-existent PID
    )
    progress_file.write_text(prog_record.to_json(), encoding="utf-8")

    parser = build_parser()
    args_status = parser.parse_args(["status", "--repo", str(repo)])
    ret = cmd_status(args_status)
    assert ret == 0

    out = capsys.readouterr().out
    assert "ACTIVE TASK RUNS (1):" in out
    assert "HOWLFRAM-123456" in out
    assert "State:          STALE (Process not running)" in out
    assert "Phase:          IMPLEMENTING" in out
    assert "Resource:       agy" in out
    assert "Elapsed:        00:02:14" in out
    assert "Last heartbeat:" in out

    # 2. Progress file with current process PID (live process)
    prog_record.pid = os.getpid()
    progress_file.write_text(prog_record.to_json(), encoding="utf-8")

    ret_live = cmd_status(args_status)
    assert ret_live == 0
    out_live = capsys.readouterr().out
    assert "State:          RUNNING" in out_live
    assert "Phase:          IMPLEMENTING" in out_live
    assert "Resource:       agy" in out_live


# -----------------------------------------------------------------------------
# 6. CLI Argument Parsing and Stream Isolation
# -----------------------------------------------------------------------------


def test_cli_progress_modes_and_stream_isolation(tmp_path, capsys):
    """Verify --progress and -q flags work without corrupting stdout."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_test_git_repo(repo)

    parser = build_parser()

    # 1. Dry run should not output progress lines
    args_dry = parser.parse_args(
        [
            "work",
            "Fix typo in docs",
            "--task-class",
            "docs",
            "--repo",
            str(repo),
            "--dry-run",
            "--skip-doctor",
        ]
    )
    ret_dry = cmd_work(args_dry)
    assert ret_dry == 0
    out_dry = capsys.readouterr()
    assert "AI ENGINEERING CONTROL PLANE — TASK INITIALIZED" in out_dry.out
    assert "[HowlPlane]" not in out_dry.out

    # 2. Verify --progress and -q argument options are parsed correctly
    args_prog = parser.parse_args(
        [
            "work",
            "Fix bug",
            "--task-class",
            "bug_fix",
            "--repo",
            str(repo),
            "--progress",
            "never",
        ]
    )
    assert args_prog.progress == "never"

    args_quiet = parser.parse_args(
        [
            "work",
            "Fix bug",
            "--task-class",
            "bug_fix",
            "--repo",
            str(repo),
            "-q",
        ]
    )
    assert args_quiet.quiet is True

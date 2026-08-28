"""
test_launcher.py

Comprehensive unit and integration tests for the thin global control-plane launcher (ai CLI).
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Optional
import pytest

import src.control_plane.launcher as launcher_module
from src.control_plane.synthesis.provider_pool import ProviderPoolManager

from src.control_plane.launcher import (
    find_git_repo_root,
    find_control_plane_root,
    infer_task_metadata,
    format_agent_launch_command,
    cmd_work,
    cmd_route,
    cmd_doctor,
    cmd_status,
    cmd_verify,
    main as launcher_main,
    TargetRepositoryNotFoundError,
    ControlPlaneNotFoundError,
)
from src.control_plane.task_spec import TaskSpec


# ============================================================================
# 1. Repository Discovery Tests
# ============================================================================

def test_find_git_repo_root_current(tmp_path):
    repo_dir = tmp_path / "my_project"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()

    discovered = find_git_repo_root(repo_dir)
    assert discovered == repo_dir.resolve()


def test_find_git_repo_root_subdirectory(tmp_path):
    repo_dir = tmp_path / "my_project"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    sub_dir = repo_dir / "src" / "deep" / "pkg"
    sub_dir.mkdir(parents=True)

    discovered = find_git_repo_root(sub_dir)
    assert discovered == repo_dir.resolve()


def test_find_git_repo_root_not_found(tmp_path):
    non_git = tmp_path / "plain_dir"
    non_git.mkdir()

    with pytest.raises(TargetRepositoryNotFoundError) as exc:
        find_git_repo_root(non_git)
    assert "no target Git repository found" in str(exc.value)


# ============================================================================
# 2. Control Plane Discovery Tests
# ============================================================================

def _make_fake_cp(path: Path, heading: str = "# Agents\n") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "AGENTS.md").write_text(heading, encoding="utf-8")
    (path / "src" / "control_plane").mkdir(parents=True, exist_ok=True)
    return path


def test_find_control_plane_root_override(tmp_path):
    fake_cp = _make_fake_cp(tmp_path / "fake_cp")
    res = find_control_plane_root(override_path=str(fake_cp))
    assert res == fake_cp.resolve()


@pytest.mark.parametrize("env_name", ["HOWLPLANE_HOME", "HOWLPLANE_DIR"])
def test_find_control_plane_root_env_vars(env_name, tmp_path, monkeypatch):
    for var in ("HOWLPLANE_HOME", "HOWLPLANE_DIR", "AI_KNOWLEDGE_LIBRARY"):
        monkeypatch.delenv(var, raising=False)
    fake_cp = _make_fake_cp(tmp_path / f"{env_name.lower()}_cp")
    monkeypatch.setenv(env_name, str(fake_cp))
    res = find_control_plane_root()
    assert res == fake_cp.resolve()


def test_find_control_plane_root_legacy_ai_knowledge_library_env(tmp_path, monkeypatch, capsys):
    for var in ("HOWLPLANE_HOME", "HOWLPLANE_DIR"):
        monkeypatch.delenv(var, raising=False)
    fake_cp = _make_fake_cp(tmp_path / "legacy_env_cp")
    monkeypatch.setenv("AI_KNOWLEDGE_LIBRARY", str(fake_cp))
    res = find_control_plane_root()
    assert res == fake_cp.resolve()
    err = capsys.readouterr().err
    assert "WARNING: AI_KNOWLEDGE_LIBRARY environment variable is deprecated" in err


def test_find_control_plane_root_precedence_howlplane_home_over_legacy(tmp_path, monkeypatch):
    fake_primary = _make_fake_cp(tmp_path / "primary_cp", heading="# Primary\n")
    fake_legacy = _make_fake_cp(tmp_path / "legacy_cp", heading="# Legacy\n")

    monkeypatch.setenv("HOWLPLANE_HOME", str(fake_primary))
    monkeypatch.setenv("AI_KNOWLEDGE_LIBRARY", str(fake_legacy))

    res = find_control_plane_root()
    assert res == fake_primary.resolve()


@pytest.mark.parametrize("config_subdir", ["howlplane", "ai-control-plane"])
def test_find_control_plane_root_config_files(config_subdir, tmp_path, monkeypatch):
    for var in ("HOWLPLANE_HOME", "HOWLPLANE_DIR", "AI_KNOWLEDGE_LIBRARY"):
        monkeypatch.delenv(var, raising=False)
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    fake_cp = _make_fake_cp(tmp_path / f"configured_{config_subdir}")
    cfg_dir = fake_home / ".config" / config_subdir
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(f'[control_plane]\npath = "{fake_cp}"\n', encoding="utf-8")

    res = find_control_plane_root()
    assert res == fake_cp.resolve()


def test_find_control_plane_root_invalid_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("HOWLPLANE_HOME", raising=False)
    monkeypatch.delenv("HOWLPLANE_DIR", raising=False)
    monkeypatch.delenv("AI_KNOWLEDGE_LIBRARY", raising=False)
    with pytest.raises(ControlPlaneNotFoundError):
        find_control_plane_root(override_path=str(tmp_path / "non_existent"))


# ============================================================================
# 3. Task Metadata Inference Tests
# ============================================================================

def test_infer_task_metadata_issue_number():
    task_id, task_class, risk, tier = infer_task_metadata("fix issue 552", "Career_Agent_Core")
    assert "552" in task_id
    assert task_class == "bug_fix"
    assert risk == "medium"
    assert tier == "tier_2"


def test_infer_task_metadata_security_critical():
    task_id, task_class, risk, tier = infer_task_metadata("patch auth credential vulnerability", "auth_service")
    assert task_class == "security_patch"
    assert risk in ("high", "critical")
    assert tier == "tier_1"


def test_infer_task_metadata_documentation_low():
    task_id, task_class, risk, tier = infer_task_metadata("update README typo comments", "docs_repo")
    assert task_class == "documentation"
    assert risk == "low"
    assert tier == "tier_3"


def test_infer_task_metadata_explicit_overrides():
    task_id, task_class, risk, tier = infer_task_metadata(
        objective="anything",
        repo_name="my_repo",
        explicit_id="CUSTOM-99",
        explicit_risk="high",
        explicit_tier="tier_1",
        explicit_class="refactor",
    )
    assert task_id == "CUSTOM-99"
    assert task_class == "refactor"
    assert risk == "high"
    assert tier == "tier_1"


# ============================================================================
# 5. CLI Execution Tests
# ============================================================================

def _make_test_repo(path: Path, files: Optional[dict] = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    if files:
        for fname, content in files.items():
            (path / fname).write_text(content, encoding="utf-8")
    return path


def _use_connected_test_pool(monkeypatch) -> None:
    pool = ProviderPoolManager(probe_on_start=False)
    monkeypatch.setattr(
        launcher_module.ProviderPoolManager,
        "from_config",
        classmethod(lambda cls, **kwargs: pool),
    )


def test_ai_route_subcommand(tmp_path, monkeypatch, capsys):
    _use_connected_test_pool(monkeypatch)
    repo_dir = _make_test_repo(tmp_path / "sample_repo")
    code = launcher_main(["route", "fix issue 101", "--repo", str(repo_dir)])
    assert code == 0
    captured = capsys.readouterr().out
    assert "Task class: bug_fix" in captured
    assert "Likely selected:" in captured


def test_ai_doctor_subcommand(tmp_path, capsys):
    repo_dir = _make_test_repo(tmp_path / "sample_repo")
    code = launcher_main(["doctor", "--repo", str(repo_dir)])
    assert code in (0, 1)
    captured = capsys.readouterr().out
    assert "WORKSPACE HEALTH DIAGNOSTICS" in captured


def test_ai_status_subcommand(tmp_path, capsys):
    repo_dir = _make_test_repo(tmp_path / "sample_repo", {"go.mod": "module sample\n"})
    code = launcher_main(["status", "--repo", str(repo_dir)])
    assert code == 0
    captured = capsys.readouterr().out
    assert "PROJECT STATUS: sample_repo" in captured
    assert "Project Stack:      go" in captured


def test_ai_work_subcommand_creates_run_artifacts(tmp_path, monkeypatch, capsys):
    _use_connected_test_pool(monkeypatch)
    repo_dir = _make_test_repo(tmp_path / "work_project", {"go.mod": "module workproj\n"})
    code = launcher_main([
        "work",
        "implement user profile validation",
        "--repo", str(repo_dir),
        "--task-id", "WORK-001",
        "--risk", "medium",
    ])
    assert code == 0
    captured = capsys.readouterr().out
    assert "TASK INITIALIZED" in captured
    assert "WORK-001" in captured
    assert "RECOMMENDED AGENT LAUNCH COMMAND" in captured

    run_dir = repo_dir / ".task_runs" / "WORK-001"
    assert run_dir.exists()
    assert (run_dir / "task.yaml").exists()
    assert (run_dir / "findings_template.yaml").exists()
    assert (run_dir / "verification_plan.json").exists()
    assert (run_dir / "reviews" / "correctness-reviewer.md").exists()

    spec = TaskSpec.load_from_file(str(run_dir / "task.yaml"))
    assert spec.task_id == "WORK-001"
    assert spec.repository == "work_project"


def test_ai_work_human_boundary_awaiting_human(tmp_path, monkeypatch, capsys):
    _use_connected_test_pool(monkeypatch)
    repo_dir = _make_test_repo(tmp_path / "infra_project")
    code = launcher_main([
        "work",
        "deploy production cluster",
        "--repo", str(repo_dir),
        "--task-id", "INFRA-001",
        "--actions", "terraform apply",
    ])
    assert code == 2
    captured = capsys.readouterr().out
    assert "AWAITING HUMAN APPROVAL" in captured

    dp_file = repo_dir / ".task_runs" / "INFRA-001" / "decision_packet.md"
    assert dp_file.exists()
    assert "Human Authority Decision Packet" in dp_file.read_text(encoding="utf-8")


def test_ai_non_git_failure_ux(tmp_path, capsys):
    non_git = tmp_path / "random_folder"
    non_git.mkdir()

    code = launcher_main(["work", "something", "--repo", str(non_git)])
    assert code == 1
    err_out = capsys.readouterr().err
    assert "ERROR: no target Git repository found" in err_out


def test_ai_howlframe_audit_subcommand(tmp_path, capsys):
    repo_dir = _make_test_repo(tmp_path / "audit_project", {
        "AGENTS.md": "# Context\n",
        "go.mod": "module auditproj\n",
    })
    code = launcher_main(["howlframe-audit", "--repo", str(repo_dir)])
    assert code == 0
    captured = capsys.readouterr().out
    assert "HOWLFRAME PROJECT CONTEXT AUDIT" in captured
    assert "audit_project" in captured


def test_ai_status_with_shadow_mode(tmp_path, capsys, monkeypatch):
    repo_dir = _make_test_repo(tmp_path / "status_project", {
        "AGENTS.md": "# Context\n",
        "go.mod": "module statusproj\n",
    })
    monkeypatch.setenv("HOWLPLANE_HOWLFRAME_DOGFOOD", "shadow")
    code = launcher_main(["status", "--repo", str(repo_dir)])
    assert code == 0
    captured = capsys.readouterr().out
    assert "HOWLFRAME DOGFOOD STATUS" in captured
    assert "shadow" in captured


# --- Terminal state outranks stale progress (HOWLFRAM-SLOPFIX-07 follow-up) ---


def _run_dir_with_progress(repo_dir: Path, task_id: str, current_state: str) -> Path:
    """Builds a run whose progress heartbeat still claims RUNNING.

    This is the real shape a cancelled or failed run leaves behind:
    `progress.json` is written by a live process and is not rewritten when the
    lifecycle reaches a terminal state.
    """
    run_dir = repo_dir / ".task_runs" / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "task.yaml").write_text(
        f"task_id: {task_id}\n"
        f"repository: {repo_dir}\n"
        "objective: bounded change\n"
        f"current_state: {current_state}\n",
        encoding="utf-8",
    )
    (run_dir / "progress.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "phase": "PREPARING",
                "state": "RUNNING",
                "resource_id": "claude_code",
                "elapsed_seconds": 0,
                "pid": 999999,
                "schema": "howlplane.task_progress/v1",
            }
        ),
        encoding="utf-8",
    )
    return run_dir


@pytest.mark.parametrize("terminal_state", ["cancelled", "failed", "complete"])
def test_terminal_task_not_reported_as_stale_progress(tmp_path, capsys, terminal_state):
    """A terminal run must not be headlined from a stale heartbeat.

    HOWLFRAM-SLOPFIX-06 was durably CANCELLED yet still rendered as
    "STALE (Process not running) / PREPARING", disagreeing with its own
    recommendation immediately below it.
    """
    repo_dir = _make_test_repo(tmp_path / "sample_repo", {"go.mod": "module sample\n"})
    _run_dir_with_progress(repo_dir, f"TASK-TERM-{terminal_state.upper()}", terminal_state)

    assert launcher_main(["status", "--repo", str(repo_dir)]) == 0
    captured = capsys.readouterr().out

    assert "STALE (Process not running)" not in captured
    assert "Phase:          PREPARING" not in captured
    assert terminal_state.upper() in captured


def test_live_run_still_reported_from_progress(tmp_path, capsys):
    """The progress view is unchanged for a run that has not reached a terminal state."""
    repo_dir = _make_test_repo(tmp_path / "sample_repo", {"go.mod": "module sample\n"})
    _run_dir_with_progress(repo_dir, "TASK-LIVE-01", "implementing")

    assert launcher_main(["status", "--repo", str(repo_dir)]) == 0
    captured = capsys.readouterr().out

    assert "Phase:          PREPARING" in captured

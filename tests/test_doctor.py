"""
test_doctor.py

Unit tests for src/infrastructure/doctor.py diagnostics.
"""

from howlplane.control_plane.evidence_ledger import EvidenceEntry, EvidenceLedger
from pathlib import Path
from howlplane.control_plane.doctor import (
    check_python_environment,
    check_dependencies,
    check_go_toolchain,
    check_git_status,
    check_git_hooks,
    check_slopslint,
    check_control_plane_ledger,
    check_operating_mode,
    run_diagnostics,
    main as doctor_main,
)


def test_check_python_environment(monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/fake/venv")
    check = check_python_environment()
    assert check.status == "ok"
    assert "fake/venv" in check.message

    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    check_no_venv = check_python_environment()
    assert check_no_venv.status == "warning"


def test_check_dependencies():
    check = check_dependencies()
    assert check.status == "ok"


def test_check_go_toolchain():
    check = check_go_toolchain()
    assert check.status in ("ok", "warning")


def test_check_git_status(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    check = check_git_status(tmp_path)
    assert check.status == "ok"

    non_git = tmp_path / "empty_dir"
    non_git.mkdir()
    check_bad = check_git_status(non_git)
    assert check_bad.status == "warning"


def test_check_git_hooks(tmp_path):
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)

    # Missing hooks
    check_missing = check_git_hooks(tmp_path)
    assert check_missing.status == "warning"
    assert "missing" in check_missing.message

    # Create dummy hooks
    pre_commit = hooks_dir / "pre-commit"
    pre_push = hooks_dir / "pre-push"
    pre_commit.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pre_push.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pre_commit.chmod(0o755)
    pre_push.chmod(0o755)

    check_ok = check_git_hooks(tmp_path)
    assert check_ok.status == "ok"


def test_check_slopslint():
    check = check_slopslint()
    assert check.status in ("ok", "warning")


def test_check_control_plane_ledger(tmp_path):
    check_empty = check_control_plane_ledger(tmp_path)
    assert check_empty.status == "ok"

    logs_dir = tmp_path / "logs" / "control_plane"
    logs_dir.mkdir(parents=True)
    ledger_file = logs_dir / "evidence_ledger.jsonl"
    ledger = EvidenceLedger(str(ledger_file))
    for task_id in ("T1", "T2"):
        ledger.append_entry(EvidenceEntry(task_id=task_id, agent_id="a", action="task_created"))

    check_valid = check_control_plane_ledger(tmp_path)
    assert check_valid.status == "ok"
    assert "2 schema-valid" in check_valid.message

    with open(ledger_file, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    check_bad = check_control_plane_ledger(tmp_path)
    assert check_bad.status == "error"
    assert "line 3" in check_bad.message and "malformed" in check_bad.message


def test_check_operating_mode():
    check_local = check_operating_mode({"operating_mode": "local_only"})
    assert check_local.status == "ok"
    assert "local_only" in check_local.message
    assert "100% Local Privacy" in check_local.message

    check_conn = check_operating_mode({"operating_mode": "connected"})
    assert check_conn.status == "ok"
    assert "connected" in check_conn.message

    check_invalid = check_operating_mode({"operating_mode": "invalid_mode"})
    assert check_invalid.status == "error"
    assert "Invalid operating mode" in check_invalid.message


def test_run_diagnostics():
    checks = run_diagnostics()
    assert len(checks) >= 9
    assert any(c.name == "AI Resource Configuration" for c in checks)
    assert all(c.status in ("ok", "warning", "error") for c in checks)


def test_doctor_main():
    code = doctor_main()
    assert code in (0, 1)

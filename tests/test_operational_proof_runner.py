"""Regression coverage for truthful deterministic component-suite receipts."""

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "operational_proof_runner",
    Path(__file__).parents[1] / "scripts/run_operational_proof.py",
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "source.txt").write_text("original\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "baseline",
        ],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def receipt(repo, command, timeout=3):
    path = repo.parent / (repo.name + "-receipt.json")
    run = runner.run_command(command, repo, timeout)
    runner.write_receipt(
        path, {"schema": runner.SCHEMA, "status": run["status"], "runs": [run]}
    )
    return path, run


def test_changed_source_invalidates_success(repo):
    path, run = receipt(repo, [sys.executable, "-c", "pass"])
    assert run["status"] == "passed"
    assert runner.check_receipt(path)
    (repo / "source.txt").write_text("regression\n")
    assert not runner.check_receipt(path)


def test_untracked_source_invalidates_success(repo):
    path, _ = receipt(repo, [sys.executable, "-c", "pass"])
    (repo / "new.py").write_text("raise RuntimeError\n")
    assert not runner.check_receipt(path)


def test_failed_command_never_passes(repo):
    path, run = receipt(repo, [sys.executable, "-c", "raise SystemExit(7)"])
    assert run["exit_code"] == 7
    assert run["status"] == "failed"
    assert not runner.check_receipt(path)


def test_timeout_is_recorded_as_failure(repo):
    path, run = receipt(
        repo, [sys.executable, "-c", "import time; time.sleep(30)"], 0.1
    )
    assert run["timed_out"]
    assert run["elapsed_seconds"] < 5
    assert not runner.check_receipt(path)


def test_both_local_prohibitions_reach_child_without_environment_receipt(
    repo, monkeypatch
):
    monkeypatch.setenv("FORBID_LOCAL_INFERENCE", "0")
    monkeypatch.setenv("HOWL_FORBID_LOCAL_INFERENCE", "0")
    monkeypatch.setenv("HOWLPLANE_LIVE_PROVIDERS", "1")
    monkeypatch.setenv("TEST_PRIVATE_SECRET", "not-for-receipts")
    code = (
        "import os; assert os.environ['FORBID_LOCAL_INFERENCE']=='1'; "
        "assert os.environ['HOWL_FORBID_LOCAL_INFERENCE']=='1'; "
        "assert 'HOWLPLANE_LIVE_PROVIDERS' not in os.environ"
    )
    path, run = receipt(repo, [sys.executable, "-c", code])
    assert run["status"] == "passed"
    assert "not-for-receipts" not in path.read_text()
    assert "environment" not in json.loads(path.read_text())


def test_source_mutation_during_suite_fails(repo):
    code = "from pathlib import Path; Path('source.txt').write_text('changed')"
    _, run = receipt(repo, [sys.executable, "-c", code])
    assert run["exit_code"] == 0
    assert run["status"] == "failed"


def test_missing_executable_is_not_success(repo):
    path, run = receipt(repo, [str(repo / "missing")])
    assert run["exit_code"] is None
    assert run["error"] == "FileNotFoundError"
    assert not runner.check_receipt(path)


@pytest.mark.parametrize(
    "body", ["{}", "[]", '{"schema":"howl.operational_proof/v1","runs":[null]}']
)
def test_check_rejects_missing_or_malformed_receipt(tmp_path, body):
    path = tmp_path / "bad.json"
    assert runner.main(["--check", str(path)]) == 1
    path.write_text(body)
    assert runner.main(["--check", str(path)]) == 1


def test_snapshot_records_symlink_without_reading_target(repo, tmp_path_factory):
    target = tmp_path_factory.mktemp("private") / "secret"
    target.write_text("first")
    os.symlink(target, repo / "link")
    before = runner.snapshot(repo)
    target.write_text("second")
    assert runner.snapshot(repo) == before


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf", "0", "-1"])
def test_cli_rejects_unbounded_timeout(tmp_path, timeout):
    with pytest.raises(SystemExit) as error:
        runner.main(
            [
                "--root",
                str(tmp_path),
                "--output",
                str(tmp_path / "receipt.json"),
                f"--timeout={timeout}",
            ]
        )
    assert error.value.code == 2
    assert not (tmp_path / "receipt.json").exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_output_cannot_overwrite_measured_source(repo, monkeypatch, symlink):
    if symlink:
        external = repo.parent / "external.txt"
        external.write_text("original\n")
        (repo / "source.txt").unlink()
        (repo / "source.txt").symlink_to(external)
    monkeypatch.setattr(
        runner, "run_command", lambda *args: pytest.fail("must not execute")
    )
    with pytest.raises(SystemExit) as error:
        runner.main(
            [
                "--root",
                str(repo.parent),
                "--plane",
                str(repo),
                "--group",
                "plane-recovery",
                "--output",
                str(repo / "source.txt"),
            ]
        )
    assert error.value.code == 2
    assert (repo / "source.txt").read_text() == "original\n"


def test_check_rejects_receipt_inside_measured_repo(repo):
    path, _ = receipt(repo, [sys.executable, "-c", "pass"])
    copied = repo / "receipt.json"
    copied.write_bytes(path.read_bytes())
    assert not runner.check_receipt(copied)


def test_successful_leader_cannot_leave_background_mutation(repo):
    marker = repo / "late-marker"
    child = "import time; from pathlib import Path; time.sleep(0.4); Path('late-marker').touch()"
    leader = (
        f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    )
    path, run = receipt(repo, [sys.executable, "-c", leader])
    assert run["exit_code"] == 0
    assert run["background_processes"]
    assert run["status"] == "failed"
    assert not runner.check_receipt(path)
    time.sleep(0.6)
    assert not marker.exists()

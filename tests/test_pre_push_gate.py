"""The pre-push gate is fast for branches and stays complete for main."""
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "pre_push_gate.sh"
ZERO = "0" * 40
BRANCH = f"refs/heads/feat/x {'a' * 40} refs/heads/feat/x {ZERO}\n"
MAIN = f"refs/heads/main {'a' * 40} refs/heads/main {'b' * 40}\n"
DELETE = f"(delete) {ZERO} refs/heads/feat/x {'b' * 40}\n"


@pytest.fixture
def harness(tmp_path):
    """A real Git repository with a fake make that records how the gate called it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "make.log"
    make = bin_dir / "make"
    make.write_text(f'#!/bin/sh\necho "$@|$HOWLPLANE_HOME" >> "{log}"\nexit "${{FAKE_MAKE_EXIT:-0}}"\n')
    make.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "HOWLPLANE_PUSH_GATE"}
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        HOWLPLANE_HOME="/stale/path",
    )

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=repo, env=env, check=True, capture_output=True,
        )

    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "one")
    git("commit", "-q", "--allow-empty", "-m", "two")

    def run(stdin, **extra):
        return subprocess.run(
            ["bash", str(GATE)], cwd=repo, env={**env, **extra}, input=stdin, capture_output=True, text=True
        )

    return run, log, repo


def calls(log):
    return log.read_text().splitlines() if log.exists() else []


def test_branch_push_runs_only_the_fast_check(harness):
    run, log, _repo = harness
    result = run(BRANCH)
    assert result.returncode == 0, result.stderr
    assert [line.split()[0] for line in calls(log)] == ["push-check"]
    assert "CI runs the full regression" in result.stdout


def test_a_push_targeting_main_runs_the_full_gate(harness):
    run, log, _repo = harness
    assert run(MAIN).returncode == 0
    assert calls(log)[0].startswith("test lint build docs")


def test_main_among_several_refs_still_runs_the_full_gate(harness):
    run, log, _repo = harness
    assert run(BRANCH + MAIN).returncode == 0
    assert calls(log)[0].startswith("test lint build docs")


def test_the_full_gate_can_be_requested_on_a_branch(harness):
    run, log, _repo = harness
    assert run(BRANCH, HOWLPLANE_PUSH_GATE="full").returncode == 0
    assert calls(log)[0].startswith("test lint build docs")


def test_deleting_a_branch_runs_no_tests(harness):
    run, log, _repo = harness
    assert run(DELETE).returncode == 0
    assert calls(log) == []


@pytest.mark.parametrize("stdin", [BRANCH, MAIN])
def test_a_failed_check_aborts_the_push(harness, stdin):
    run, _log, _repo = harness
    result = run(stdin, FAKE_MAKE_EXIT="2")
    assert result.returncode == 1
    assert "abort" in result.stdout.lower()


def test_the_gate_ignores_a_stale_inherited_howlplane_home(harness):
    run, log, repo = harness
    assert run(BRANCH).returncode == 0
    assert calls(log)[0].endswith(f"|{repo.resolve()}")


def test_the_base_is_passed_to_the_fast_check(harness):
    run, log, _repo = harness
    assert run(BRANCH).returncode == 0
    assert "PUSH_BASE=" in calls(log)[0]
    assert len(calls(log)[0].split("PUSH_BASE=")[1].split("|")[0]) == 40

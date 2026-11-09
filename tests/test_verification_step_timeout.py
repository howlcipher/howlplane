#!/usr/bin/env python3
"""
tests/test_verification_step_timeout.py

issues.md #28: every verification step was capped at a literal 300 seconds.
HowlPlane's own suite takes about 16 minutes, so HP-SELF-25 failed
verification on a timeout rather than a defect, and `resume --json` then
reported no steps and no error. The step's `make` was killed but the pytest
it started kept running.

These tests pin the fix: a per-step timeout a repository can declare per
category, a timeout recorded as its own failure class, the whole process group
killed, and a resumed failure that names the failing step.
"""

import json
import math
import os
import subprocess
import time
from pathlib import Path

import pytest

from howlplane.control_plane import verification as verification_module
from howlplane.control_plane.presentation.work_result import work_result_document
from howlplane.control_plane.project_adapter import ProjectAdapter
from howlplane.control_plane.verification import (
    DEFAULT_STEP_TIMEOUT_SECONDS,
    FAILURE_CLASS_TIMEOUT,
    VerificationError,
    VerificationPlan,
    VerificationStep,
)
from tests.test_pre_verification_escalation_resume import (
    _approve_and_resume,
    _park_before_verification,
    _repo,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _step(command, timeout_seconds=None, category="unit_test"):
    return VerificationStep(
        step_id="step-01", name="Suite", command=command,
        category=category, timeout_seconds=timeout_seconds,
    )


def _plan_with(step: VerificationStep) -> VerificationPlan:
    plan = VerificationPlan(task_id="TASK-T")
    plan.steps.append(step)
    return plan


def _capture_timeout(monkeypatch):
    seen = {}

    def fake(cmd_args, cwd, env, timeout):
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(cmd_args, 0, "", "")

    monkeypatch.setattr(verification_module, "_run_bounded", fake)
    return seen


# ---------------------------------------------------------------------------
# The limit comes from the step, with the old value as the default
# ---------------------------------------------------------------------------

def test_a_step_without_its_own_timeout_keeps_the_300_second_default(monkeypatch, tmp_path):
    seen = _capture_timeout(monkeypatch)
    step = _step(["true"])
    _plan_with(step).execute_step(step, cwd=str(tmp_path))
    assert DEFAULT_STEP_TIMEOUT_SECONDS == 300
    assert seen["timeout"] == 300


def test_a_step_runs_under_its_declared_timeout(monkeypatch, tmp_path):
    seen = _capture_timeout(monkeypatch)
    step = _step(["true"], timeout_seconds=1800)
    _plan_with(step).execute_step(step, cwd=str(tmp_path))
    assert seen["timeout"] == 1800
    assert step.status == "verified"


@pytest.mark.parametrize("bad", [0, -1, math.nan, math.inf, True, "60"])
def test_an_unusable_timeout_is_rejected(bad):
    with pytest.raises(VerificationError):
        _step(["true"], timeout_seconds=bad)


def test_a_saved_plan_keeps_its_timeouts_and_an_older_plan_still_loads():
    plan = _plan_with(_step(["true"], timeout_seconds=1800))
    assert VerificationPlan.from_dict(json.loads(plan.to_json())).steps[0].timeout_seconds == 1800

    older = json.loads(plan.to_json())
    del older["steps"][0]["timeout_seconds"]
    loaded = VerificationPlan.from_dict(older).steps[0]
    assert loaded.timeout_seconds is None
    assert loaded.effective_timeout_seconds == 300


def test_the_plan_schema_accepts_a_saved_plan_with_timeouts():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO_ROOT / "schemas" / "verification-plan.schema.json").read_text())
    plan = _plan_with(_step(["true"], timeout_seconds=1800))
    plan.steps[0].metadata["failure_class"] = FAILURE_CLASS_TIMEOUT
    jsonschema.validate(json.loads(plan.to_json()), schema)


# ---------------------------------------------------------------------------
# A timeout is its own failure, and nothing it started survives it
# ---------------------------------------------------------------------------

def test_a_timeout_is_recorded_as_a_timeout_with_its_limit(tmp_path):
    step = _step(["sh", "-c", "echo started; sleep 30"], timeout_seconds=0.5)
    started = time.monotonic()
    _plan_with(step).execute_step(step, cwd=str(tmp_path))

    assert time.monotonic() - started < 10
    assert step.status == "failed"
    assert step.exit_code == -1
    assert step.metadata["failure_class"] == FAILURE_CLASS_TIMEOUT
    assert step.metadata["timeout_seconds"] == 0.5
    assert "timed out after 0.5 seconds" in step.stderr
    assert "[tool.howlplane.verification.timeouts]" in step.stderr
    assert "started" in (step.stdout or "")


def test_an_ordinary_failure_is_not_called_a_timeout(tmp_path):
    step = _step(["false"], timeout_seconds=30)
    _plan_with(step).execute_step(step, cwd=str(tmp_path))
    assert step.status == "failed"
    assert "failure_class" not in step.metadata


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_a_timeout_kills_the_processes_the_step_started(tmp_path):
    pid_file = tmp_path / "child.pid"
    # Like `make test` starting pytest: the step's own process starts a
    # child, and the step times out while the child is still running.
    script = f"sleep 60 & echo $! > {pid_file}; wait"
    step = _step(["sh", "-c", script], timeout_seconds=1)
    _plan_with(step).execute_step(step, cwd=str(tmp_path))

    assert step.metadata["failure_class"] == FAILURE_CLASS_TIMEOUT
    child = int(pid_file.read_text().strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(child, 9)
    pytest.fail("the step's child process outlived the timeout")


# ---------------------------------------------------------------------------
# A repository declares its timeouts; the plan applies them per category
# ---------------------------------------------------------------------------

def _python_repo(tmp_path: Path, pyproject_extra: str = "") -> Path:
    (tmp_path / "Makefile").write_text("test:\n\ttrue\nlint:\n\ttrue\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n' + pyproject_extra)
    return tmp_path


def test_pyproject_timeouts_apply_to_their_category_only(tmp_path):
    repo = _python_repo(tmp_path, "[tool.howlplane.verification.timeouts]\nunit_test = 1800\n")
    plan = ProjectAdapter.create_verification_plan(ProjectAdapter.discover(repo), task_id="T")
    by_category = {s.category: s for s in plan.steps}

    assert by_category["unit_test"].timeout_seconds == 1800
    assert by_category["lint"].timeout_seconds is None
    assert by_category["lint"].effective_timeout_seconds == 300


def test_a_manifest_timeout_wins_over_pyproject(tmp_path):
    repo = _python_repo(tmp_path, "[tool.howlplane.verification.timeouts]\nunit_test = 1800\n")
    (repo / "project_manifest.yaml").write_text(
        "name: demo\ncommands:\n  test: [make, test]\nverification:\n  timeouts:\n    unit_test: 2400\n"
    )
    ctx = ProjectAdapter.discover(repo)
    assert ctx.step_timeouts == {"unit_test": 2400}


def test_unusable_declared_timeouts_are_ignored_and_reported(tmp_path):
    repo = _python_repo(
        tmp_path,
        '[tool.howlplane.verification.timeouts]\nunit_test = 0\nlint = "fast"\nnot_a_category = 60\n',
    )
    ctx = ProjectAdapter.discover(repo)
    assert ctx.step_timeouts == {}
    assert ctx.metadata["invalid_step_timeouts"] == {"unit_test": 0, "lint": "fast", "not_a_category": 60}


def test_the_manifest_schema_accepts_timeouts_and_rejects_unusable_ones():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO_ROOT / "schemas" / "ai-project.schema.json").read_text())
    base = {"schema_version": 1, "name": "demo"}
    jsonschema.validate({**base, "verification": {"timeouts": {"unit_test": 2400}}}, schema)
    for bad in ({"unit_test": 0}, {"unit_test": "fast"}, {"not_a_category": 60}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**base, "verification": {"timeouts": bad}}, schema)


def test_a_repository_without_declared_timeouts_is_unchanged(tmp_path):
    ctx = ProjectAdapter.discover(_python_repo(tmp_path))
    assert ctx.step_timeouts == {}
    assert "invalid_step_timeouts" not in ctx.metadata


def test_howlplane_declares_a_test_timeout_its_own_suite_fits_in():
    """The suite takes about 16 minutes; the default would fail every self-hosted task."""
    ctx = ProjectAdapter.discover(REPO_ROOT)
    assert ctx.step_timeouts.get("unit_test", 0) >= 1800


# ---------------------------------------------------------------------------
# A resumed failure says which step failed and why
# ---------------------------------------------------------------------------

def test_resume_names_the_step_that_timed_out(tmp_path):
    repo = _repo(tmp_path)
    plan = VerificationPlan(task_id="TASK-SLOW")
    plan.add_step(step_id="step-01", name="Lint", command=["true"], category="lint")
    plan.add_step(
        step_id="step-02", name="Automated test suite (make test)",
        command=["sleep", "30"], category="unit_test", timeout_seconds=0.5,
    )
    _park_before_verification(repo, "TASK-SLOW", plan=plan)

    res = _approve_and_resume(repo, "TASK-SLOW")

    assert res.final_state == "failed"
    assert "Automated test suite (make test)" in res.error_message
    assert "timed out after 0.5 seconds" in res.error_message

    document = work_result_document(res, roles=[])
    assert document["error"] == res.error_message
    steps = {s["name"]: s for s in document["verification"]["steps"]}
    assert document["verification"]["status"] == "failed"
    assert steps["Lint"]["status"] == "verified"
    assert steps["Automated test suite (make test)"]["failure_class"] == FAILURE_CLASS_TIMEOUT
    assert steps["Automated test suite (make test)"]["timeout_seconds"] == 0.5


def test_resume_names_an_ordinary_failing_step_too(tmp_path):
    repo = _repo(tmp_path)
    plan = VerificationPlan(task_id="TASK-RED")
    plan.add_step(step_id="step-01", name="Unit tests", command=["false"], category="unit_test")
    _park_before_verification(repo, "TASK-RED", plan=plan)

    res = _approve_and_resume(repo, "TASK-RED")

    assert res.final_state == "failed"
    assert "Unit tests: exit 1" in res.error_message
    assert res.verification_plan is not None
    assert work_result_document(res, roles=[])["verification"]["steps"][0]["failure_class"] is None

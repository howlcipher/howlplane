"""Task id validation, atomic run-state writes, HowlDream explore hardening."""

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from howlplane.control_plane import howldream_runner
from howlplane.control_plane.task_spec import TaskSpec, TaskSpecValidationError


def _spec_with_id(task_id):
    spec = TaskSpec.__new__(TaskSpec)
    spec.__dict__["task_id"] = task_id
    return spec


@pytest.mark.parametrize("bad", ["../x", "a/b", "a..b", ".hidden", "-x", "a b", "x" * 129])
def test_task_id_rejects_path_unsafe(bad):
    with pytest.raises(TaskSpecValidationError):
        _spec_with_id(bad).validate()


@pytest.mark.parametrize("good", ["TASK-101", "imp-51", "bug_12", "v1.2.3"])
def test_task_id_pattern_accepts(good):
    from howlplane.control_plane.task_spec import _TASK_ID_RE
    assert _TASK_ID_RE.match(good)


def _provider(tmp_path):
    p = howldream_runner.NativeHowlDreamProvider.__new__(howldream_runner.NativeHowlDreamProvider)
    return p


def _explore(tmp_path, run):
    import builtins
    real_import = builtins.__import__

    def no_howldream(name, *a, **k):
        if name.startswith("howldream"):
            raise ImportError(name)
        return real_import(name, *a, **k)

    p = _provider(tmp_path)
    budget = howldream_runner.ExplorationBudget()
    with mock.patch.dict("os.environ", {"HOWLDREAM_BIN": "/bin/true"}), \
         mock.patch.object(builtins, "__import__", side_effect=no_howldream), \
         mock.patch.object(howldream_runner.subprocess, "run", side_effect=run):
        return p.explore("obj", budget, tmp_path)


def test_explore_timeout_and_run_id_validation(tmp_path):
    def timeout(*a, **k):
        assert k.get("timeout")
        raise subprocess.TimeoutExpired("x", 1)
    with pytest.raises(RuntimeError):
        _explore(tmp_path, timeout)

    def evil(*a, **k):
        return mock.Mock(stdout='{"exploration_id": "../../etc"}')
    with pytest.raises(RuntimeError, match="invalid exploration_id"):
        _explore(tmp_path, evil)

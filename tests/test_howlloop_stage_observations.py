"""Optional stage observations follow the actual orchestration boundaries."""

import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from howlplane.control_plane import orchestration
from howlplane.control_plane.agent_execution import (
    LAUNCH_OUTCOME_KEY,
    LAUNCH_OUTCOME_NOT_INSTALLED,
    TIMEOUT_SOURCE_HARNESS,
    TIMEOUT_SOURCE_KEY,
)
from howlplane.control_plane.factory.improvement import observe_launch
from tests.test_orchestration_execution_budget import record_backend


@pytest.fixture
def observations(monkeypatch, tmp_path):
    events = []

    @contextmanager
    def observe(project, operation, **ids):
        status = SimpleNamespace(result="ok", detail=None, validation=None)
        yield status
        events.append((project, operation, ids, status))

    monkeypatch.setenv("HOWLLOOP_STORE", str(tmp_path / "observations.sqlite"))
    monkeypatch.setitem(sys.modules, "howlloop.producer", SimpleNamespace(observe=observe))
    return events


def test_queue_correlates_provider_and_verification_without_recording_commands(
    tmp_path, monkeypatch, observations,
):
    calls = record_backend(monkeypatch)
    session = uuid4()
    doc = {
        "id": session.hex, "goal": "synthetic private objective", "constraints": [],
        "policy": "PLAN ONLY", "attempts": [],
        "verify_command": [sys.executable, "-c", "print('synthetic private output')"],
    }
    dispatch = SimpleNamespace(task=SimpleNamespace(fingerprint="test-task"), action="resume")

    def launch(_, __):
        assert orchestration.execute_assignment(doc, "planning", "codex", "UNKNOWN", tmp_path).success
        assert orchestration.run_verification(doc, tmp_path)["exit_code"] == 0
        return {"status": "COMPLETE"}

    assert observe_launch(launch, dispatch, None) == {"status": "COMPLETE"}
    assert len(calls) == 1
    assert [operation for _, operation, _, _ in observations] == [
        "provider", "verification", "recovery",
    ]
    assert len({ids["run_id"] for _, _, ids, _ in observations}) == 1
    assert len({ids["task_id"] for _, _, ids, _ in observations}) == 1
    assert {ids["session_id"] for _, _, ids, _ in observations[:2]} == {str(session)}
    assert observations[1][-1].validation == "passed"
    assert "synthetic private" not in repr(observations)

    orchestration.run_verification(doc, tmp_path)
    assert observations[-1][2] == {"run_id": str(session), "session_id": str(session)}


@pytest.mark.parametrize(("outcome", "expected", "validation"), [
    (subprocess.CompletedProcess([], 3, "", ""), "failed", "failed"),
    (subprocess.CompletedProcess([], -15, "", ""), "interrupted", "unavailable"),
    (subprocess.TimeoutExpired([], 1), "timeout", "unavailable"),
    (FileNotFoundError("synthetic"), "unavailable", "unavailable"),
])
def test_verification_reports_observed_outcomes(
    tmp_path, monkeypatch, observations, outcome, expected, validation,
):
    def run(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(orchestration.subprocess, "run", run)
    orchestration.run_verification({"verify_command": ["synthetic"]}, tmp_path)
    assert observations[-1][-1].result == expected
    assert observations[-1][-1].validation == validation


@pytest.mark.parametrize(("metadata", "expected"), [
    ({LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_NOT_INSTALLED}, "unavailable"),
    ({TIMEOUT_SOURCE_KEY: TIMEOUT_SOURCE_HARNESS}, "timeout"),
    ({}, "failed"),
])
def test_provider_uses_execution_metadata_instead_of_transcript(
    tmp_path, monkeypatch, observations, metadata, expected,
):
    result = SimpleNamespace(
        success=False, metadata=metadata, timed_out=True, stdout="synthetic report about a timeout",
    )

    class Backend:
        def execute(self, task, cwd, role, prompt_override, timeout_seconds, model_id):
            return result

    monkeypatch.setattr(orchestration.AgentBackendRegistry, "get_backend", lambda agent: Backend())
    doc = {"id": uuid4().hex, "goal": "fixture", "constraints": [], "policy": "PLAN ONLY", "attempts": []}
    assert orchestration.execute_assignment(doc, "planning", "codex", "UNKNOWN", tmp_path) is result
    assert observations[-1][-1].result == expected
    assert "synthetic report" not in repr(observations)


@pytest.mark.parametrize("enabled", [False, True])
def test_missing_optional_package_preserves_verification(tmp_path, monkeypatch, enabled):
    monkeypatch.setitem(sys.modules, "howlloop.producer", None)
    if enabled:
        monkeypatch.setenv("HOWLLOOP_STORE", str(tmp_path / "observations.sqlite"))
    else:
        monkeypatch.delenv("HOWLLOOP_STORE", raising=False)
    result = orchestration.run_verification(
        {"id": uuid4().hex, "verify_command": [sys.executable, "-c", "print('fixture complete')"]},
        tmp_path,
    )
    assert result["exit_code"] == 0
    assert "fixture complete" in result["output"]
    assert not (tmp_path / "observations.sqlite").exists()

"""Deterministic precedence tests for normalized provider failures."""

import json
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest

from src.control_plane.agent_execution import (
    AgentExecutionResult,
    ClaudeCodeBackend,
    LAUNCH_OUTCOME_KEY,
    LAUNCH_OUTCOME_LAUNCHED,
    TERMINAL_PROVIDER_ERROR_KEY,
    TIMEOUT_SOURCE_HARNESS,
    TIMEOUT_SOURCE_KEY,
    TIMEOUT_SOURCE_TRANSCRIPT,
    TOOL_PERMISSION_DENIED,
    TOOL_PERMISSION_KEY,
)
from src.control_plane.resource_models import ProviderFailureClass
from src.control_plane.synthesis.provider_pool import ProviderPoolManager
from src.control_plane.task_spec import TaskSpec


def _failed_result(
    *,
    stdout: str = "",
    stderr: str = "",
    error_message: Optional[str] = None,
    exit_code: int = 1,
    timed_out: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> AgentExecutionResult:
    structural_metadata = {LAUNCH_OUTCOME_KEY: LAUNCH_OUTCOME_LAUNCHED}
    structural_metadata.update(metadata or {})
    return AgentExecutionResult(
        agent_id="claude_code",
        role="implementation",
        command="claude",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=1.0,
        success=False,
        timed_out=timed_out,
        error_message=error_message,
        metadata=structural_metadata,
    )


def _classify(result: AgentExecutionResult) -> ProviderFailureClass:
    return ProviderPoolManager().classify_failure("claude_code", result)


def test_session_limit_only_is_session_limit():
    result = _failed_result(
        stdout="You've hit your session limit · resets 10pm (America/Detroit)"
    )

    assert _classify(result) == ProviderFailureClass.SESSION_LIMIT


def test_permission_denial_only_requires_execution_permission():
    result = _failed_result(
        stdout="The required Bash command was denied.",
        metadata={TOOL_PERMISSION_KEY: TOOL_PERMISSION_DENIED},
    )

    assert _classify(result) == ProviderFailureClass.EXECUTION_PERMISSION_REQUIRED


def test_terminal_session_limit_outranks_earlier_permission_denial(tmp_path):
    terminal_error = "You've hit your session limit · resets 10pm (America/Detroit)"
    envelope = json.dumps(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": terminal_error,
            "permission_denials": [{"tool_name": "Bash"}],
        }
    )
    backend = ClaudeCodeBackend()
    task = TaskSpec(
        task_id="CLASSIFICATION_PRECEDENCE",
        repository="test_repository",
        objective="Exercise provider failure precedence",
    )

    with patch.object(backend, "is_available", return_value=True):
        with patch("subprocess.run") as run:
            run.return_value = SimpleNamespace(
                returncode=0,
                stdout=envelope,
                stderr="",
            )
            result = backend.execute(task, cwd=tmp_path)

    assert result.success is False
    assert result.metadata[TOOL_PERMISSION_KEY] == TOOL_PERMISSION_DENIED
    assert result.metadata[TERMINAL_PROVIDER_ERROR_KEY] == terminal_error
    assert _classify(result) == ProviderFailureClass.SESSION_LIMIT


@pytest.mark.parametrize(
    ("terminal_error", "expected"),
    [
        ("Error: quota exceeded", ProviderFailureClass.QUOTA_EXHAUSTED),
        ("Error: rate limit exceeded", ProviderFailureClass.RATE_LIMITED),
    ],
)
def test_terminal_capacity_error_outranks_earlier_permission_denial(
    terminal_error,
    expected,
):
    result = _failed_result(
        stderr=terminal_error,
        metadata={TOOL_PERMISSION_KEY: TOOL_PERMISSION_DENIED},
    )

    assert _classify(result) == expected


def test_terminal_permission_denial_outranks_earlier_session_warning_prose():
    result = _failed_result(
        stdout=(
            "The task quoted an earlier message saying session limit reached.\n"
            "The required command is currently blocked on approval."
        ),
        metadata={TOOL_PERMISSION_KEY: TOOL_PERMISSION_DENIED},
    )

    assert _classify(result) == ProviderFailureClass.EXECUTION_PERMISSION_REQUIRED


def test_harness_timeout_outranks_incidental_command_not_found_text():
    result = _failed_result(
        stderr="bash: formatter: command not found\nTimeout after 600s.",
        exit_code=-1,
        timed_out=True,
        metadata={TIMEOUT_SOURCE_KEY: TIMEOUT_SOURCE_HARNESS},
    )

    assert _classify(result) == ProviderFailureClass.EXECUTION_BUDGET_EXCEEDED


def test_provider_transport_timeout_is_transport_unavailable():
    result = _failed_result(
        stderr="Error: timeout waiting for response",
        timed_out=True,
        metadata={TIMEOUT_SOURCE_KEY: TIMEOUT_SOURCE_TRANSCRIPT},
    )

    assert _classify(result) == ProviderFailureClass.TRANSPORT_UNAVAILABLE


def test_authentication_expiration_requires_authentication():
    result = _failed_result(
        stderr="Authentication expired. Please log in again."
    )

    assert _classify(result) == ProviderFailureClass.AUTHENTICATION_REQUIRED


def test_malformed_and_ambiguous_output_fail_closed():
    malformed = _failed_result(stderr="Invalid JSON in provider response")
    ambiguous = _failed_result(stderr="Provider stopped without a reason")

    assert _classify(malformed) == ProviderFailureClass.MALFORMED_OUTPUT
    assert _classify(ambiguous) == ProviderFailureClass.ENGINEERING_FAILURE

"""
test_provider_permissions.py

Deterministic tests for bounded unattended provider permissions,
role-aware tool authorization, and honest failure classification.
"""

from types import SimpleNamespace
from unittest.mock import patch
import pytest

from src.control_plane.agent_execution import (
    TOOL_PERMISSION_DENIED,
    TOOL_PERMISSION_KEY,
    ClaudeCodeBackend,
)
from src.control_plane.orchestrator import (
    FAILURE_CLASS_PROVIDER_UNAVAILABLE,
    GovernedTaskOrchestrator,
)
from src.control_plane.provider_execution_profile import (
    command_to_bash_specifier,
)
from src.control_plane.resource_models import (
    ProviderFailureClass,
    ReadinessStatus,
)
from src.control_plane.synthesis.provider_pool import (
    ProviderPoolManager,
    TASK_SUITABILITY_PREFERENCES,
)
from src.control_plane.task_spec import TaskSpec
from src.infrastructure.config_loader import (
    ProviderExecutionProfileSettings,
    ProviderResourceSettings,
)


def _sample_task(prohibited=None) -> TaskSpec:
    return TaskSpec(
        task_id="TASK-PERM-01",
        repository="sample_repo",
        objective="Fix duplication defect",
        prohibited_actions=prohibited or [],
    )


def test_claude_implementation_receives_bounded_mutation_permissions(tmp_path):
    backend = ClaudeCodeBackend()
    cmd = backend.build_command(_sample_task(), tmp_path, "implementation", "prompt text")

    assert "claude" in cmd[0]
    assert "-p" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd
    assert "--allowedTools" in cmd

    tools_idx = cmd.index("--allowedTools")
    allowed = cmd[tools_idx + 1 :]

    for tool in ("Read", "Glob", "Grep", "Edit", "Write"):
        assert tool in allowed

    assert "--permission-mode" in cmd
    assert "acceptEdits" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert "bypassPermissions" not in cmd


def test_claude_review_role_does_not_gain_mutation_tools(tmp_path):
    backend = ClaudeCodeBackend()
    for review_role in ("review", "correctness-reviewer", "test-falsifier"):
        cmd = backend.build_command(_sample_task(), tmp_path, review_role, "review prompt")
        assert "--permission-mode" not in cmd
        if "--allowedTools" in cmd:
            tools_idx = cmd.index("--allowedTools")
            allowed = cmd[tools_idx + 1 :]
            assert "Edit" not in allowed
            assert "Write" not in allowed
            assert "Read" in allowed


def test_claude_remediation_role_gains_mutation_authority(tmp_path):
    backend = ClaudeCodeBackend()
    cmd = backend.build_command(_sample_task(), tmp_path, "remediation", "fix findings")
    assert "--permission-mode" in cmd
    assert "acceptEdits" in cmd
    tools_idx = cmd.index("--allowedTools")
    allowed = cmd[tools_idx + 1 :]
    assert "Edit" in allowed
    assert "Write" in allowed


def test_bash_permissions_are_bounded():
    assert command_to_bash_specifier(["go", "test", "./..."]) == "Bash(go test:*)"
    assert command_to_bash_specifier(["pytest", "-q"]) == "Bash(pytest:*)"
    assert command_to_bash_specifier(["make", "test"]) == "Bash(make test)"
    assert command_to_bash_specifier(["bash", "-c", "echo 1"]) == "Bash(bash -c echo 1)"
    assert command_to_bash_specifier([]) is None


def test_explicitly_disallowed_tools_remain_denied(tmp_path):
    operator_settings = ProviderResourceSettings(
        enabled=True,
        execution_profile=ProviderExecutionProfileSettings(
            disallowed_tools=["Write", "Bash(git log:*)"],
        ),
    )
    backend = ClaudeCodeBackend(operator_settings=operator_settings)
    cmd = backend.build_command(_sample_task(), tmp_path, "implementation", "prompt")

    assert "--disallowedTools" in cmd
    disallowed_idx = cmd.index("--disallowedTools")
    disallowed = cmd[disallowed_idx + 1 :]
    assert "Write" in disallowed
    assert "Bash(git log:*)" in disallowed

    tools_idx = cmd.index("--allowedTools")
    allowed = cmd[tools_idx + 1 : disallowed_idx]
    assert "Write" not in allowed
    assert "Bash(git log:*)" not in allowed


def test_task_prohibitions_override_defaults(tmp_path):
    task = _sample_task(prohibited=["Edit", "Write"])
    backend = ClaudeCodeBackend()
    cmd = backend.build_command(task, tmp_path, "implementation", "prompt")

    assert "--permission-mode" not in cmd
    if "--allowedTools" in cmd:
        tools_idx = cmd.index("--allowedTools")
        end_idx = cmd.index("--disallowedTools") if "--disallowedTools" in cmd else len(cmd)
        allowed = cmd[tools_idx + 1 : end_idx]
        assert "Edit" not in allowed
        assert "Write" not in allowed


def test_bypass_permissions_rejected_in_config():
    with pytest.raises(Exception):
        ProviderExecutionProfileSettings(permission_mode="bypassPermissions")


def test_claude_permission_denial_with_zero_delta_fails(tmp_path):
    backend = ClaudeCodeBackend()
    task = _sample_task()

    denial_json = (
        '{"type": "result", "result": "I am not permitted to use Edit without approval.", '
        '"permission_denials": [{"tool_name": "Edit"}]}'
    )

    with patch.object(backend, "is_available", return_value=True):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(
                returncode=0,
                stdout=denial_json,
                stderr="",
            )
            res = backend.execute(task, cwd=tmp_path, role="implementation")

    assert res.success is False
    assert res.metadata.get(TOOL_PERMISSION_KEY) == TOOL_PERMISSION_DENIED
    assert "Edit" in res.metadata.get("denied_tools", [])

    pool = ProviderPoolManager(operating_mode="connected")
    cls = pool.classify_failure("claude_code", res)
    assert cls == ProviderFailureClass.EXECUTION_PERMISSION_REQUIRED


def test_claude_permission_denial_in_plain_text(tmp_path):
    backend = ClaudeCodeBackend()
    task = _sample_task()

    with patch.object(backend, "is_available", return_value=True):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(
                returncode=0,
                stdout="Every attempt returned: This command requires approval from the user.",
                stderr="",
            )
            res = backend.execute(task, cwd=tmp_path, role="implementation")

    assert res.success is False
    assert res.metadata.get(TOOL_PERMISSION_KEY) == TOOL_PERMISSION_DENIED
    pool = ProviderPoolManager(operating_mode="connected")
    assert pool.classify_failure("claude_code", res) == ProviderFailureClass.EXECUTION_PERMISSION_REQUIRED


def test_execution_permission_participates_in_failover():
    orchestrator = GovernedTaskOrchestrator(target_repo=".")
    assert orchestrator._is_failover_eligible_failure(
        ProviderFailureClass.EXECUTION_PERMISSION_REQUIRED
    ) is True
    assert orchestrator._map_failure_class_to_orchestrator_class(
        ProviderFailureClass.EXECUTION_PERMISSION_REQUIRED
    ) == FAILURE_CLASS_PROVIDER_UNAVAILABLE


def test_successful_claude_run_with_edits_succeeds(tmp_path):
    backend = ClaudeCodeBackend()
    task = _sample_task()

    success_json = '{"type": "result", "result": "Successfully updated the code.", "permission_denials": []}'
    with patch.object(backend, "is_available", return_value=True):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(
                returncode=0,
                stdout=success_json,
                stderr="",
            )
            res = backend.execute(task, cwd=tmp_path, role="implementation")

    assert res.success is True
    assert res.stdout == "Successfully updated the code."
    assert TOOL_PERMISSION_KEY not in res.metadata


def test_readiness_distinct_from_mutation_capability():
    backend = ClaudeCodeBackend()
    with patch.object(backend, "is_available", return_value=True):
        ready = backend.probe_readiness()
        assert ready.status == ReadinessStatus.READY
        assert ready.unattended_mutation_capable is True

    denied_backend = ClaudeCodeBackend(
        operator_settings=ProviderResourceSettings(
            enabled=True,
            execution_profile=ProviderExecutionProfileSettings(disallowed_tools=["Edit", "Write"]),
        )
    )
    with patch.object(denied_backend, "is_available", return_value=True):
        denied_ready = denied_backend.probe_readiness()
        assert denied_ready.status == ReadinessStatus.READY
        assert denied_ready.unattended_mutation_capable is False


def test_provider_ordering_unchanged():
    routine = TASK_SUITABILITY_PREFERENCES.get("routine", [])
    assert routine == ["agy", "codex", "devin_cli", "claude_code", "local_ollama"]
    code_heavy = TASK_SUITABILITY_PREFERENCES.get("code_heavy", [])
    assert code_heavy == ["codex", "agy", "devin_cli", "claude_code", "local_ollama"]

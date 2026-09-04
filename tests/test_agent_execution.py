"""
test_agent_execution.py

Unit and integration tests for normalized agent execution abstraction.
"""
import pytest

from src.control_plane.agent_execution import (
    AgentBackendRegistry,
    AgentExecutionResult,
    AgyBackend,
    ClaudeCodeBackend,
    CodexBackend,
    FakeAgentBackend,
    SubprocessAgentBackend,
)
from src.control_plane.task_spec import TaskSpec


def test_agent_execution_result_serialization():
    res = AgentExecutionResult(
        agent_id="codex",
        role="implementation",
        command="codex 'Fix bug'",
        exit_code=0,
        stdout="Fixed issue",
        stderr="",
        duration_seconds=1.23,
        success=True,
        metadata={"tokens": 150},
    )
    data = res.to_dict()
    assert data["agent_id"] == "codex"
    assert data["success"] is True
    assert data["duration_seconds"] == 1.23

    restored = AgentExecutionResult.from_dict(data)
    assert restored.agent_id == res.agent_id
    assert restored.exit_code == 0
    assert restored.metadata["tokens"] == 150

    json_str = res.to_json()
    assert '"agent_id": "codex"' in json_str
    restored_json = AgentExecutionResult.from_json(json_str)
    assert restored_json.command == res.command


def test_fake_agent_backend_execution(tmp_path):
    spec = TaskSpec(
        task_id="TASK-EXEC-001",
        repository="repo_sample",
        objective="Create a hello.py file for testing",
    )

    def side_effect(task, cwd, prompt):
        (cwd / "hello.py").write_text("print('hello')", encoding="utf-8")

    backend = FakeAgentBackend(
        agent_id="fake_coder",
        default_exit_code=0,
        default_stdout="Created hello.py",
        side_effect=side_effect,
    )

    res = backend.execute(spec, cwd=tmp_path, role="implementation")
    assert res.success is True
    assert res.exit_code == 0
    assert res.stdout == "Created hello.py"
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hello')"
    assert len(backend.executed_calls) == 1
    assert backend.executed_calls[0]["task_id"] == spec.task_id


def test_fake_agent_backend_failure(tmp_path):
    spec = TaskSpec(
        task_id="TASK-002",
        repository="test_repo",
        objective="Fail intentionally",
    )
    backend = FakeAgentBackend(
        agent_id="fake_failing",
        default_exit_code=1,
        default_stdout="",
        default_stderr="Compilation failed",
    )
    res = backend.execute(spec, cwd=tmp_path, role="implementation")
    assert res.success is False
    assert res.exit_code == 1
    assert "Compilation failed" in res.stderr
    assert res.error_message == "Exit code 1"


def test_backend_registry():
    claude = AgentBackendRegistry.get_backend("claude_code")
    assert isinstance(claude, ClaudeCodeBackend)

    codex = AgentBackendRegistry.get_backend("codex")
    assert isinstance(codex, CodexBackend)

    custom = FakeAgentBackend("custom")
    retrieved = AgentBackendRegistry.get_backend("claude_code", custom_backend=custom)
    assert retrieved is custom


def test_subprocess_agent_backend_unavailable(tmp_path):
    spec = TaskSpec(
        task_id="TASK-003",
        repository="test_repo",
        objective="Run non-existent agent",
    )
    backend = SubprocessAgentBackend("nonexistent_binary_xyz_123", "nonexistent_binary_xyz_123")
    assert backend.is_available() is False

    res = backend.execute(spec, cwd=tmp_path)
    assert res.success is False
    assert res.exit_code == 127
    assert "not installed" in res.stderr


def test_codex_backend_uses_workspace_write_for_implementation(tmp_path):
    """
    Codex CLI defaults to a read-only sandbox; implementation/remediation
    roles must request workspace-write or the agent cannot create files.
    Regression for live acceptance canary failure where codex returned
    'Blocked by the read-only workspace: the requested journal was not created.'
    """
    spec = TaskSpec(
        task_id="TASK-CODEX-SANDBOX-001",
        repository="howlplane",
        objective="Create a journal file",
    )
    backend = CodexBackend()

    impl_cmd = backend.build_command(spec, tmp_path, "implementation", "prompt")
    assert "exec" in impl_cmd
    assert "--sandbox" in impl_cmd
    assert "workspace-write" in impl_cmd

    rem_cmd = backend.build_command(spec, tmp_path, "remediation", "prompt")
    assert "--sandbox" in rem_cmd
    assert "workspace-write" in rem_cmd

    review_cmd = backend.build_command(spec, tmp_path, "correctness-reviewer", "prompt")
    assert "--sandbox" not in review_cmd
    assert "workspace-write" not in review_cmd


def test_agy_print_timeout_expires_before_the_harness_kills_the_process(tmp_path):
    """agy must be given room to report its own timeout before the harness kills it.

    Both deadlines firing together is a race: `subprocess.TimeoutExpired`
    usually wins, so the same budget overrun is classified from a harness
    timeout instead of agy's transcript, nondeterministically, and agy's
    diagnostic output is discarded with the process.
    """
    spec = TaskSpec(
        task_id="TASK-AGY-TIMEOUT-001",
        repository="howlplane",
        objective="Run agy writer with timeout",
    )
    backend = AgyBackend()
    cmd = backend.build_command(spec, tmp_path, "writer", "prompt", timeout_seconds=600)
    assert "--print-timeout" in cmd
    idx = cmd.index("--print-timeout")
    assert cmd[idx + 1].endswith("s")
    assert int(cmd[idx + 1][:-1]) < 600


def test_agy_print_timeout_stays_positive_for_a_budget_below_the_headroom(tmp_path):
    """A tiny budget must still produce a usable duration, not zero or negative."""
    spec = TaskSpec(
        task_id="TASK-AGY-TIMEOUT-002",
        repository="howlplane",
        objective="Run agy writer with a very small budget",
    )
    cmd = AgyBackend().build_command(spec, tmp_path, "writer", "prompt", timeout_seconds=5)
    idx = cmd.index("--print-timeout")
    assert int(cmd[idx + 1][:-1]) >= 1


def test_all_registered_backends_support_build_command_with_timeout(tmp_path):
    spec = TaskSpec(
        task_id="TASK-POLYMORPHIC-TIMEOUT-001",
        repository="howlplane",
        objective="Verify backend build_command handles timeout_seconds polymorphically",
    )
    for agent_id in ["agy", "claude_code", "codex", "gemini_cli", "devin_cli"]:
        backend = AgentBackendRegistry.get_backend(agent_id)
        assert backend is not None
        cmd = backend.build_command(spec, tmp_path, "implementation", "prompt", timeout_seconds=450)
        assert isinstance(cmd, list)
        assert len(cmd) > 0


def test_backend_internal_type_error_is_propagated_and_not_swallowed(tmp_path):
    spec = TaskSpec(
        task_id="TASK-INTERNAL-TYPE-ERROR-001",
        repository="howlplane",
        objective="Verify internal TypeError in builder is propagated",
    )

    def broken_builder(t, c, r, p, timeout_seconds=300):
        raise TypeError("internal TypeError within backend implementation")

    backend = SubprocessAgentBackend("broken", "broken", broken_builder)
    with pytest.raises(TypeError, match="internal TypeError within backend implementation"):
        backend.build_command(spec, tmp_path, "implementation", "prompt", timeout_seconds=300)


def test_backend_legacy_four_argument_builder_compatibility(tmp_path):
    spec = TaskSpec(
        task_id="TASK-LEGACY-BUILDER-001",
        repository="howlplane",
        objective="Verify legacy 4-arg builder works via signature inspection",
    )

    def legacy_builder(t, c, r, p):
        return ["legacy", "-p", p]

    backend = SubprocessAgentBackend("legacy", "legacy", legacy_builder)
    cmd = backend.build_command(spec, tmp_path, "implementation", "prompt", timeout_seconds=300)
    assert cmd == ["legacy", "-p", "prompt"]


def test_legacy_builder_internal_type_error_is_propagated(tmp_path):
    spec = TaskSpec(
        task_id="TASK-LEGACY-ERR-001",
        repository="howlplane",
        objective="Verify internal TypeError in legacy builder is propagated",
    )

    def broken_legacy_builder(t, c, r, p):
        raise TypeError("internal TypeError within legacy builder")

    backend = SubprocessAgentBackend("broken_legacy", "broken_legacy", broken_legacy_builder)
    with pytest.raises(TypeError, match="internal TypeError within legacy builder"):
        backend.build_command(spec, tmp_path, "implementation", "prompt", timeout_seconds=300)


def test_execute_propagates_timeout_seconds_to_build_command(tmp_path, monkeypatch):
    spec = TaskSpec(
        task_id="TASK-EXEC-TIMEOUT-001",
        repository="howlplane",
        objective="Verify execute passes timeout_seconds to build_command",
    )
    recorded_timeout = {}

    def builder(t, c, r, p, timeout_seconds=300):
        recorded_timeout["val"] = timeout_seconds
        return ["echo", "done"]

    backend = SubprocessAgentBackend("test_exec", "echo", builder)
    monkeypatch.setattr(backend, "is_available", lambda: True)
    backend.execute(spec, tmp_path, timeout_seconds=420)
    assert recorded_timeout.get("val") == 420

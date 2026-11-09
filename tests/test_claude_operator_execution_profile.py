"""The operator's `[ai_resources.providers.claude_code.execution_profile]` reaches Claude.

HP-SELF-30A: the config granted `pytest:*`, but the shared Claude backend was built
with no operator settings, so the grant never applied. The remediation prompt told
the agent its shell held only `make test`, and each remediation ran the 16-minute
full suite instead of the one test file the task named, until the third ran out
of time.
"""

import pytest

from howlplane.control_plane import config_loader
from howlplane.control_plane.agent_execution import (
    READ_ONLY_ROLE_NOTE,
    AgentBackendRegistry,
    ClaudeCodeBackend,
)
from howlplane.control_plane.task_spec import TaskSpec

OPERATOR_TOML = """
[ai_resources.providers.claude_code.execution_profile]
extra_allowed_bash = ["pytest:*", "python3 -m pytest:*"]
disallowed_tools = ["WebFetch"]
"""


def _task() -> TaskSpec:
    return TaskSpec(task_id="HP-OPCFG", repository="repo", objective="test")


@pytest.fixture
def operator_config(tmp_path, monkeypatch):
    """Points the shared config loader at an operator TOML for this test only."""
    path = tmp_path / "config.toml"
    path.write_text(OPERATOR_TOML, encoding="utf-8")
    loader = config_loader.ConfigLoader(local_config_path=str(path))
    monkeypatch.setattr(config_loader, "default_loader", loader, raising=False)
    return loader


def test_the_shared_claude_backend_grants_the_operator_extra_commands(tmp_path, operator_config):
    backend = AgentBackendRegistry.get_backend("claude_code")
    profile = backend.build_execution_profile(_task(), tmp_path, "remediation")

    assert "Bash(pytest:*)" in profile.bash_specifiers
    assert "Bash(python3 -m pytest:*)" in profile.bash_specifiers
    assert "operator" in profile.derivation["bash_sources"]


def test_the_prompt_lists_the_operator_commands_the_agent_really_holds(tmp_path, operator_config):
    backend = AgentBackendRegistry.get_backend("claude_code")
    command = backend.build_command(_task(), tmp_path, "remediation", "PROMPT")

    prompt = command[2]
    assert "`pytest ...`" in prompt and "`python3 -m pytest ...`" in prompt
    allowed = command[command.index("--allowedTools") + 1:]
    assert "Bash(pytest:*)" in allowed


def test_operator_denials_apply_to_the_shared_backend(tmp_path, operator_config):
    backend = AgentBackendRegistry.get_backend("claude_code")
    profile = backend.build_execution_profile(_task(), tmp_path, "implementation")

    assert "WebFetch" in profile.disallowed_tools


def test_operator_commands_never_reach_a_read_only_role(tmp_path, operator_config):
    backend = AgentBackendRegistry.get_backend("claude_code")
    profile = backend.build_execution_profile(_task(), tmp_path, "review")

    assert profile.bash_specifiers == ()
    assert READ_ONLY_ROLE_NOTE in backend.build_command(_task(), tmp_path, "review", "PROMPT")[2]


def test_a_directly_built_backend_still_takes_only_the_settings_it_is_given(tmp_path, operator_config):
    profile = ClaudeCodeBackend().build_execution_profile(_task(), tmp_path, "remediation")

    assert "Bash(pytest:*)" not in profile.bash_specifiers


def test_an_unreadable_operator_config_grants_nothing_extra_and_does_not_fail(tmp_path, monkeypatch):
    def broken_loader():
        raise ValueError("invalid config.toml")

    monkeypatch.setattr(config_loader, "get_default_loader", broken_loader)
    backend = AgentBackendRegistry.get_backend("claude_code")
    profile = backend.build_execution_profile(_task(), tmp_path, "remediation")

    assert "Bash(pytest:*)" not in profile.bash_specifiers
    assert "Bash(git diff:*)" in profile.bash_specifiers

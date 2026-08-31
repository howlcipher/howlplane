#!/usr/bin/env python3
"""
test_role_binding.py

Unit tests for domain-neutral role execution and binding in HowlPlane.
"""

import os
from unittest.mock import patch

from src.control_plane.agent_execution import FakeAgentBackend
from src.control_plane.agent_registry import AgentProfile, AgentRegistry
from src.control_plane.role_binding import (
    IndependenceStatus,
    RoleBinding,
    RoleBindingRegistry,
    RoleDescriptor,
    RoleDispatcher,
    RoleExecutionRequest,
    extract_structured_output,
)


def test_role_descriptor_creation_and_registration():
    registry = RoleBindingRegistry()
    desc = RoleDescriptor(
        domain="writing",
        role="humanizer",
        capability="text_transform",
        description="Removes generic AI tone and varies syntax.",
        requires_reviewer_independence=True,
    )
    registry.register_descriptor(desc)

    retrieved = registry.get_descriptor("writing", "humanizer")
    assert retrieved is not None
    assert retrieved.domain == "writing"
    assert retrieved.role == "humanizer"
    assert retrieved.capability == "text_transform"
    assert retrieved.requires_reviewer_independence is True


def test_role_binding_registration_and_lookup():
    registry = RoleBindingRegistry()
    binding = RoleBinding(
        domain="writing",
        role="humanizer",
        provider="claude_code",
        model="claude-3-5-sonnet",
        capability="text_transform",
    )
    registry.register_binding(binding)

    assert registry.get_binding("writing", "humanizer") == binding
    assert registry.get_binding("WRITING", "HUMANIZER") == binding
    assert registry.get_binding("writing", "editor") is None
    assert len(registry.list_bindings("writing")) == 1
    assert len(registry.list_bindings("software")) == 0


def test_role_binding_load_from_config():
    registry = RoleBindingRegistry()
    config_data = {
        "roles": {
            "writing": {
                "humanizer": {
                    "provider": "claude_code",
                    "capability": "text_transform",
                    "timeout_seconds": 120,
                },
                "meaning_reviewer": "codex",
            },
            "software": {
                "tester": "agy",
            },
        }
    }
    registry.load_from_config(config_data)

    humanizer_b = registry.get_binding("writing", "humanizer")
    assert humanizer_b is not None
    assert humanizer_b.provider == "claude_code"
    assert humanizer_b.timeout_seconds == 120

    reviewer_b = registry.get_binding("writing", "meaning_reviewer")
    assert reviewer_b is not None
    assert reviewer_b.provider == "codex"

    tester_b = registry.get_binding("software", "tester")
    assert tester_b is not None
    assert tester_b.provider == "agy"


def test_role_binding_load_from_env():
    registry = RoleBindingRegistry()
    env_vars = {
        "HOWLPLANE_ROLE_WRITING_HUMANIZER": "claude_code",
        "HOWLPLANE_ROLE_WRITING_EDITOR": "codex",
    }
    with patch.dict(os.environ, env_vars):
        registry.load_from_env()

    h_binding = registry.get_binding("writing", "humanizer")
    assert h_binding is not None
    assert h_binding.provider == "claude_code"

    e_binding = registry.get_binding("writing", "editor")
    assert e_binding is not None
    assert e_binding.provider == "codex"


def test_extract_structured_output():
    json_md = """Here is the result:
```json
{
  "status": "PASS",
  "score": 0.95,
  "notes": ["clean text"]
}
```
Done."""
    res = extract_structured_output(json_md)
    assert res == {"status": "PASS", "score": 0.95, "notes": ["clean text"]}

    yaml_md = """Analysis:
```yaml
resulting_text: "Clean prose."
changes_made:
  - "simplified phrasing"
rationale: "Better cadence"
```"""
    res_yaml = extract_structured_output(yaml_md)
    assert res_yaml["resulting_text"] == "Clean prose."
    assert res_yaml["changes_made"] == ["simplified phrasing"]

    raw_json = '{"verdict": "FAIL", "reason": "meaning shifted"}'
    assert extract_structured_output(raw_json) == {
        "verdict": "FAIL",
        "reason": "meaning shifted",
    }

    assert extract_structured_output("Just some unstructured text") is None


def test_dispatcher_unconfigured_role():
    registry = RoleBindingRegistry()
    dispatcher = RoleDispatcher(binding_registry=registry)

    req = RoleExecutionRequest(
        domain="writing", role="humanizer", prompt="Rewrite this."
    )
    result = dispatcher.execute(req)

    assert result.success is False
    assert "No executor or provider configured" in result.error_message
    assert result.independence_status == IndependenceStatus.UNAVAILABLE.value


def test_dispatcher_successful_execution_with_fake_backend():
    registry = RoleBindingRegistry()
    registry.register_binding(
        RoleBinding(
            domain="writing",
            role="humanizer",
            provider="fake_humanizer",
        )
    )

    fake_backend = FakeAgentBackend(
        agent_id="fake_humanizer",
        default_stdout="""```yaml
resulting_text: "Humanized output prose."
changes_made:
  - "removed mechanical transition"
rationale: "Direct sentence structure."
```""",
    )

    dispatcher = RoleDispatcher(binding_registry=registry)
    req = RoleExecutionRequest(
        domain="writing",
        role="humanizer",
        prompt="Original text to humanize",
    )

    result = dispatcher.execute(req, custom_backend=fake_backend)
    assert result.success is True
    assert result.provider == "fake_humanizer"
    assert result.structured_output is not None
    assert result.structured_output["resulting_text"] == (
        "Humanized output prose."
    )
    assert len(result.structured_output["changes_made"]) == 1


def test_dispatcher_reviewer_independence_multiple_providers():
    registry = RoleBindingRegistry()
    registry.register_binding(
        RoleBinding(
            domain="writing",
            role="meaning_reviewer",
            provider="claude_code",
        )
    )

    agent_registry = AgentRegistry()

    dispatcher = RoleDispatcher(
        binding_registry=registry,
        agent_registry=agent_registry,
    )

    provider_id, _, status = dispatcher.resolve_provider(
        domain="writing",
        role="meaning_reviewer",
        avoid_provider="claude_code",
    )

    assert provider_id != "claude_code"
    assert status == IndependenceStatus.INDEPENDENT.value


def test_dispatcher_reviewer_independence_same_provider_honesty():
    registry = RoleBindingRegistry()
    registry.register_binding(
        RoleBinding(
            domain="writing",
            role="meaning_reviewer",
            provider="single_provider",
        )
    )

    single_profile = AgentProfile(
        agent_id="single_provider",
        name="Single Provider",
        provider="single",
        interface="cli",
    )
    agent_registry = AgentRegistry(agents=[single_profile])

    dispatcher = RoleDispatcher(
        binding_registry=registry,
        agent_registry=agent_registry,
    )

    provider_id, _, status = dispatcher.resolve_provider(
        domain="writing",
        role="meaning_reviewer",
        avoid_provider="single_provider",
    )

    assert provider_id == "single_provider"
    assert status == IndependenceStatus.SAME_PROVIDER.value


def test_dispatcher_backend_failure_and_timeout():
    registry = RoleBindingRegistry()
    registry.register_binding(
        RoleBinding(
            domain="writing",
            role="humanizer",
            provider="fail_backend",
        )
    )

    fake_fail = FakeAgentBackend(
        agent_id="fail_backend",
        default_exit_code=1,
        default_stdout="",
        default_stderr="Provider quota exceeded",
    )

    dispatcher = RoleDispatcher(binding_registry=registry)
    req = RoleExecutionRequest(
        domain="writing", role="humanizer", prompt="test"
    )
    res = dispatcher.execute(req, custom_backend=fake_fail)

    assert res.success is False
    assert (
        "Provider quota exceeded" in res.error_message
        or "Exit code 1" in res.error_message
    )

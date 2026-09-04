import pytest
import json
from unittest.mock import patch, MagicMock
from src.core.orchestrator import Agent, Orchestrator, build_tier_agent, tier_setting
from src.core.provider_preflight import PreflightResult

class MockMessage:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

class MockToolCall:
    def __init__(self, name, arguments):
        class Function:
            def __init__(self, n, a):
                self.name = n
                self.arguments = a
        self.function = Function(name, arguments)

def test_agent_initialization():
    agent = Agent("TestAgent", "You are a test.", "gemini/gemini-1.5-pro")
    assert agent.name == "TestAgent"
    assert agent.system_prompt == "You are a test."

@patch("litellm.completion_cost")
@patch("litellm.completion")
def test_agent_generate_response(mock_completion, mock_cost):
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="Mocked answer"))]
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 20
    mock_response.usage.total_tokens = 30
    mock_response.model = "gemini/gemini-1.5-pro"
    mock_completion.return_value = mock_response
    mock_cost.return_value = 0.05
    
    agent = Agent("TestAgent", "You are a test.", "gemini/gemini-1.5-pro")
    
    with patch("src.core.orchestrator.log_telemetry") as mock_log:
        response = agent.generate_response("Hello", context="Some context")
        assert response.content == "Mocked answer"
        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert kwargs["prompt_tokens"] == 10

@patch("litellm.completion_cost", return_value=0.0)
@patch("litellm.completion")
def test_agent_timeout_passed_to_litellm(mock_completion, mock_cost):
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="ok"))]
    mock_response.usage.prompt_tokens = 1
    mock_response.usage.completion_tokens = 1
    mock_response.usage.total_tokens = 2
    mock_completion.return_value = mock_response

    agent = Agent("TestAgent", "You are a test.", "ollama/qwen3", timeout=1800.0)
    with patch("src.core.orchestrator.log_telemetry"):
        agent.generate_response("Hello")
    assert mock_completion.call_args.kwargs["timeout"] == 1800.0


@patch("litellm.completion_cost", return_value=0.0)
@patch("litellm.completion")
def test_agent_without_timeout_omits_kwarg(mock_completion, mock_cost):
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="ok"))]
    mock_response.usage.prompt_tokens = 1
    mock_response.usage.completion_tokens = 1
    mock_response.usage.total_tokens = 2
    mock_completion.return_value = mock_response

    agent = Agent("TestAgent", "You are a test.", "ollama/qwen3")
    with patch("src.core.orchestrator.log_telemetry"):
        agent.generate_response("Hello")
    assert "timeout" not in mock_completion.call_args.kwargs


@patch("litellm.completion_cost", return_value=0.0)
@patch("litellm.completion")
def test_agent_response_format_passed_to_litellm(mock_completion, mock_cost):
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="ok"))]
    mock_response.usage.prompt_tokens = 1
    mock_response.usage.completion_tokens = 1
    mock_response.usage.total_tokens = 2
    mock_completion.return_value = mock_response

    rf = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}
    agent = Agent("TestAgent", "You are a test.", "ollama/qwen3", response_format=rf)
    with patch("src.core.orchestrator.log_telemetry"):
        agent.generate_response("Hello")
    assert mock_completion.call_args.kwargs["response_format"] == rf


@patch("litellm.completion_cost", return_value=0.0)
@patch("litellm.completion")
def test_agent_without_response_format_omits_kwarg(mock_completion, mock_cost):
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content="ok"))]
    mock_response.usage.prompt_tokens = 1
    mock_response.usage.completion_tokens = 1
    mock_response.usage.total_tokens = 2
    mock_completion.return_value = mock_response

    agent = Agent("TestAgent", "You are a test.", "ollama/qwen3")
    with patch("src.core.orchestrator.log_telemetry"):
        agent.generate_response("Hello")
    assert "response_format" not in mock_completion.call_args.kwargs


def test_tier_setting_fallbacks():
    overrides = {"tier_1": 900.0, "tier_2": 0.0}
    assert tier_setting(overrides, 1, 600.0) == 900.0
    # Zero and missing overrides both fall back to the default.
    assert tier_setting(overrides, 2, 600.0) == 600.0
    assert tier_setting(overrides, 3, 600.0) == 600.0
    assert tier_setting(None, 1, 600.0) == 600.0
    # Same helper backs per tier models: empty string falls back too.
    assert tier_setting({"tier_1": ""}, 1, "default-model") == "default-model"


def test_human_proxy_no_command(orchestrator_factory):
    orchestrator = orchestrator_factory()
    result = orchestrator.human_proxy_intercept(None)
    assert result is True

@patch("builtins.input", return_value="y")
def test_human_proxy_authorized(mock_input, orchestrator_factory):
    orchestrator = orchestrator_factory()
    tool_calls = [MockToolCall("execute_bash_command", json.dumps({"command": "echo hello"}))]
    result = orchestrator.human_proxy_intercept(tool_calls)
    assert result is True

@patch("builtins.input", return_value="n")
def test_human_proxy_rejected(mock_input, orchestrator_factory):
    orchestrator = orchestrator_factory()
    tool_calls = [MockToolCall("execute_bash_command", json.dumps({"command": "echo hello"}))]
    result = orchestrator.human_proxy_intercept(tool_calls)
    assert result is False

@patch("builtins.input", side_effect=["", "maybe", "y"])
def test_human_proxy_invalid_then_authorized(mock_input, orchestrator_factory):
    orchestrator = orchestrator_factory()
    tool_calls = [MockToolCall("execute_bash_command", json.dumps({"command": "echo hello"}))]
    result = orchestrator.human_proxy_intercept(tool_calls)
    assert result is True
    assert mock_input.call_count == 3

@patch("src.core.provider_preflight.preflight_models")
@patch("src.core.orchestrator.Agent.generate_response")
@patch("src.core.orchestrator.Orchestrator.human_proxy_intercept", return_value=True)
def test_orchestrator_run_loop_approved_immediately(mock_proxy, mock_generate, mock_preflight, orchestrator_factory):
    # run_loop preflights the provider before the multi-agent loop (item 33);
    # stub it out so this test exercises the approve/reject flow, not a real
    # network check against whatever provider config/settings.yaml points at.
    mock_preflight.return_value = PreflightResult(ok=True, checked_models=["stub"])
    orchestrator = orchestrator_factory()

    mock_generate.side_effect = [
        MockMessage("Here is my research draft"),
        MockMessage("APPROVED"),
        MockMessage("Humanized draft")
    ]

    orchestrator.run_loop("Test query")

    assert mock_generate.call_count == 3
    mock_proxy.assert_called_once()

@patch("src.core.provider_preflight.preflight_models")
@patch("src.core.orchestrator.Agent.generate_response")
@patch("src.core.orchestrator.Orchestrator.human_proxy_intercept", return_value=True)
def test_orchestrator_run_loop_rejected_then_approved(mock_proxy, mock_generate, mock_preflight, orchestrator_factory):
    mock_preflight.return_value = PreflightResult(ok=True, checked_models=["stub"])
    orchestrator = orchestrator_factory()

    mock_generate.side_effect = [
        MockMessage("First draft"), MockMessage("REJECTED. Fix it."),
        MockMessage("Second draft"), MockMessage("APPROVED"),
        MockMessage("Humanized second draft")
    ]

    orchestrator.run_loop("Test query")

    assert mock_generate.call_count == 5
    mock_proxy.assert_called_once()

@patch("src.core.provider_preflight.preflight_models")
@patch("src.core.orchestrator.Agent.generate_response")
@patch("src.core.orchestrator.Orchestrator.human_proxy_intercept", return_value=True)
def test_orchestrator_run_loop_rejected_with_approved_substring(mock_proxy, mock_generate, mock_preflight, orchestrator_factory):
    mock_preflight.return_value = PreflightResult(ok=True, checked_models=["stub"])
    orchestrator = orchestrator_factory()

    mock_generate.side_effect = [
        MockMessage("First draft"), MockMessage("This is NOT APPROVED because it lacks detail."),
        MockMessage("Second draft"), MockMessage("APPROVED"),
        MockMessage("Humanized second draft")
    ]

    orchestrator.run_loop("Test query")

    # If the bug was present, mock_generate would only be called 3 times (it would pass on the first "APPROVED" substring)
    assert mock_generate.call_count == 5
    mock_proxy.assert_called_once()

@patch("src.core.provider_preflight.preflight_models")
@patch("src.core.orchestrator.Agent.generate_response")
@patch("src.core.orchestrator.Orchestrator.human_proxy_intercept", return_value=True)
def test_orchestrator_run_loop_exhausts_max_iterations(mock_proxy, mock_generate, mock_preflight, orchestrator_factory):
    mock_preflight.return_value = PreflightResult(ok=True, checked_models=["stub"])
    orchestrator = orchestrator_factory()

    mock_generate.side_effect = [
        MockMessage("First draft"), MockMessage("REJECTED"),
        MockMessage("Second draft"), MockMessage("REJECTED"),
        MockMessage("Third draft"), MockMessage("REJECTED"),
    ]

    result = orchestrator.run_loop("Test query")
    
    assert result is not None
    assert result.get("qa_exhausted") is True
    assert "[UNREVIEWED DRAFT - QA REJECTED]" in result.get("draft_content", "")
    assert mock_generate.call_count == 6

@patch("src.core.provider_preflight.preflight_models")
@patch("src.core.orchestrator.Agent.generate_response")
@patch("src.core.orchestrator.Orchestrator.human_proxy_intercept", return_value=True)
def test_orchestrator_run_loop_approved_no_exhaustion_marker(mock_proxy, mock_generate, mock_preflight, orchestrator_factory):
    mock_preflight.return_value = PreflightResult(ok=True, checked_models=["stub"])
    orchestrator = orchestrator_factory()

    mock_generate.side_effect = [
        MockMessage("First draft"), MockMessage("APPROVED"),
        MockMessage("Humanized draft")
    ]

    result = orchestrator.run_loop("Test query")

    assert result is not None
    assert result.get("qa_exhausted") is False
    assert "[UNREVIEWED DRAFT - QA REJECTED]" not in result.get("draft_content", "")
    assert mock_generate.call_count == 3

@patch("src.core.provider_preflight.preflight_models")
@patch("src.core.orchestrator.Agent.generate_response")
@patch("src.core.orchestrator.Orchestrator.human_proxy_intercept", return_value=True)
def test_orchestrator_run_loop_humanize_neutral_by_default(mock_proxy, mock_generate, mock_preflight, orchestrator_factory):
    mock_preflight.return_value = PreflightResult(ok=True, checked_models=["stub"])
    orchestrator = orchestrator_factory()

    mock_generate.side_effect = [
        MockMessage("First draft"), MockMessage("APPROVED"),
        MockMessage("Humanized draft")
    ]

    result = orchestrator.run_loop("Test query")

    assert result is not None
    assert mock_generate.call_count == 3
    # Check that the 3rd generate_response call (humanize) has the neutral prompt
    humanize_call_args = mock_generate.call_args_list[2][0]
    prompt = humanize_call_args[0]
    assert "clarity and readability" in prompt
    assert "burstiness and perplexity" not in prompt

@patch("src.core.provider_preflight.preflight_models")
@patch("src.core.orchestrator.Agent.generate_response")
@patch("src.core.orchestrator.Orchestrator.human_proxy_intercept", return_value=True)
def test_orchestrator_run_loop_humanize_stealth_with_command(mock_proxy, mock_generate, mock_preflight, orchestrator_factory):
    mock_preflight.return_value = PreflightResult(ok=True, checked_models=["stub"])
    orchestrator = orchestrator_factory()

    mock_generate.side_effect = [
        MockMessage("First draft"), MockMessage("APPROVED"),
        MockMessage("Humanized draft")
    ]

    # Use the /stealth command
    result = orchestrator.run_loop("/stealth Test query")

    assert result is not None
    assert mock_generate.call_count == 3
    # Check that the 3rd generate_response call (humanize) has the stealth prompt
    humanize_call_args = mock_generate.call_args_list[2][0]
    prompt = humanize_call_args[0]
    assert "burstiness and perplexity to bypass AI detectors" in prompt

@patch("src.core.provider_preflight.preflight_models")
@patch("src.core.orchestrator.Agent.generate_response")
@patch("src.core.orchestrator.Orchestrator.human_proxy_intercept", return_value=True)
def test_orchestrator_run_loop_humanize_disabled_via_config(mock_proxy, mock_generate, mock_preflight, orchestrator_factory):
    mock_preflight.return_value = PreflightResult(ok=True, checked_models=["stub"])
    orchestrator = orchestrator_factory()
    orchestrator.cfg["humanize"] = {"enabled": False}

    mock_generate.side_effect = [
        MockMessage("First draft"), MockMessage("APPROVED"),
    ]

    result = orchestrator.run_loop("Test query")

    assert result is not None
    # Check that generate_response was only called twice (no humanize call)
    assert mock_generate.call_count == 2
    assert result.get("draft_content") == "First draft"

def test_main(monkeypatch):
    import sys
    from src.core.orchestrator import main

    # CLI argument forwarding is the contract here. Constructing the real
    # orchestrator would also start configured MCP processes, which belongs to
    # explicit integration coverage rather than this CLI unit test.
    with patch("src.core.orchestrator.Orchestrator") as mock_orchestrator:
        monkeypatch.setattr(sys, "argv", ["orchestrator.py", "What is AI?"])
        main()
        mock_orchestrator.return_value.run_loop.assert_called_once_with("What is AI?")


def test_build_tier_agent_returns_plain_agent_for_litellm_model():
    agent = build_tier_agent("Tier3", "prompt", "gemini/gemini-1.5-pro", timeout=30)
    assert isinstance(agent, Agent)
    assert agent.model == "gemini/gemini-1.5-pro"
    assert agent.timeout == 30


def test_build_tier_agent_returns_claude_code_agent_for_sentinel():
    from src.core.claude_code_backend import ClaudeCodeAgent

    agent = build_tier_agent("Tier1", "prompt", "claude_code", timeout=60)
    assert isinstance(agent, ClaudeCodeAgent)
    assert agent.model == "claude_code"
    assert agent.timeout == 60

def test_orchestrator_factory_provides_shutdown_callable(orchestrator_factory):
    orchestrator = orchestrator_factory()
    assert callable(orchestrator.shutdown)
    assert orchestrator.mcp_clients == {}

@patch("src.core.mcp_client.SyncMCPClient")
def test_orchestrator_shutdown_calls_close_on_every_mcp_client(mock_sync_cls):
    created_clients = []

    def _new_client(*args, **kwargs):
        client = MagicMock()
        client.get_tools.return_value = []
        created_clients.append(client)
        return client

    mock_sync_cls.side_effect = _new_client

    orchestrator = Orchestrator()
    assert created_clients, "expected at least one MCP client to be constructed"
    orchestrator.shutdown()

    for client in created_clients:
        client.close.assert_called_once()
        # connect() must be bounded: a hanging/missing MCP server should
        # not be able to block Orchestrator construction forever.
        client.connect.assert_called_once_with(
            timeout=orchestrator.cfg.get("mcp_connect_timeout", 30.0)
        )


@pytest.mark.parametrize(
    "mode,expect_client_called",
    [
        ("local_only", False),
        ("connected", True),
    ],
)
@patch("src.core.orchestrator.Client")
@patch("src.core.orchestrator.Agent.generate_response")
def test_qa_node_operating_mode_langsmith_gating(
    mock_generate, mock_client_cls, orchestrator_factory, mode, expect_client_called
):
    mock_client_instance = MagicMock()
    mock_client_cls.return_value = mock_client_instance
    mock_generate.return_value = MockMessage("APPROVED")
    orchestrator = orchestrator_factory()
    orchestrator.cfg["operating_mode"] = mode

    app = orchestrator._build_graph()
    state = {
        "query": "test",
        "context": "",
        "draft_content": "some draft",
        "tool_calls": [],
        "iteration": 1,
        "qa_approved": False,
        "qa_exhausted": False,
        "max_iterations": 1,
        "stealth_mode": False,
    }
    app.invoke(
        state, config={"configurable": {"run_id": "test-run-123", "thread_id": "1"}}
    )
    if expect_client_called:
        mock_client_cls.assert_called_once()
        mock_client_instance.create_feedback.assert_called_once_with(
            "test-run-123", key="qa_approval", score=1.0
        )
    else:
        mock_client_cls.assert_not_called()

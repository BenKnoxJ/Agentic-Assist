"""Tests for the options run_agent hands the SDK.

These pin the containment wiring (THREAT-MODEL.md): the CLI subprocess
ships Claude Code's built-in tools, so keeping them out of an agent's reach
is a configuration the SDK must actually receive - not an absence. A fake
client captures the options; no subprocess is spawned.
"""

import pytest

from gojo.agents import runner


class FakeClient:
    captured: dict = {}

    def __init__(self, options):
        FakeClient.captured["options"] = options

    async def connect(self) -> None:
        pass

    async def query(self, prompt: str) -> None:
        pass

    async def receive_response(self):
        return
        yield  # makes this an async generator that yields nothing

    async def disconnect(self) -> None:
        pass


@pytest.fixture
def captured(monkeypatch) -> dict:
    FakeClient.captured = {}
    monkeypatch.setattr(runner, "ClaudeSDKClient", FakeClient)
    return FakeClient.captured


async def test_builtin_tools_are_explicitly_denied(captured) -> None:
    """Deny-by-default is a behavioural default, not a guarantee. This is
    the explicit denial the threat model cites - if the SDK's default ever
    loosens, this wiring still holds."""
    await runner.run_agent("hi")

    denied = captured["options"].disallowed_tools
    for name in ("Bash", "Write", "Edit", "Read", "Glob", "Grep", "WebFetch", "WebSearch"):
        assert name in denied


async def test_strict_mcp_config_is_always_on(captured) -> None:
    """No filesystem MCP config may leak into Gojo's agents - the same
    isolation intent as setting_sources=[]."""
    await runner.run_agent("hi")

    options = captured["options"]
    assert options.strict_mcp_config is True
    assert options.setting_sources == []


async def test_mcp_servers_pass_through(captured) -> None:
    marker = {"gather": {"type": "sdk", "name": "gather"}}
    await runner.run_agent("hi", mcp_servers=marker)

    assert captured["options"].mcp_servers == marker


async def test_mcp_servers_default_to_none_given(captured) -> None:
    await runner.run_agent("hi")

    assert captured["options"].mcp_servers == {}


class SpeakingFakeClient(FakeClient):
    """Yields a real answer and a real result, like a live turn does."""

    async def receive_response(self):
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        yield AssistantMessage(content=[TextBlock(text="answer")], model="test-model")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=3,
            session_id="s9",
            total_cost_usd=0.0123,
        )


async def test_metadata_survives_the_traced_path(monkeypatch) -> None:
    """9.2/13.8: cost, turns and session id are the only reasoning signal
    that crosses the subprocess boundary - the trace span must not eat them."""
    monkeypatch.setattr(runner, "ClaudeSDKClient", SpeakingFakeClient)

    result = await runner.run_agent("hi")

    assert result.text == "answer"
    assert result.session_id == "s9"
    assert result.cost_usd == 0.0123
    assert result.num_turns == 3


def test_sdk_exchange_is_wrapped_in_a_langsmith_span() -> None:
    """Pins the @traceable decoration (13.8). With tracing off it no-ops;
    with tracing on it is what makes agent nodes show their reasoning
    metadata as a child run instead of an empty box."""
    assert hasattr(runner._traced_sdk_exchange, "__wrapped__")

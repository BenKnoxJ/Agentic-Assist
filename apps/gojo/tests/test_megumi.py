"""Tests for megumi's wiring: which tools the gather agent actually gets,
and how carried context enters the prompt.

The seam is run_agent (6.3 rule 2) - these tests capture its kwargs and
never touch the SDK.
"""

import pytest

from gojo.agents import megumi
from gojo.agents.runner import AgentResult
from gojo.agents.tools import GATHER_TOOL_NAMES


@pytest.fixture
def captured(monkeypatch) -> dict:
    seen: dict = {}

    async def fake_run_agent(
        prompt,
        system_prompt="",
        allowed_tools=None,
        max_turns=None,
        resume=None,
        mcp_servers=None,
    ):
        seen.update(
            prompt=prompt,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
        )
        return AgentResult(text="ok", session_id="s1")

    monkeypatch.setattr(megumi, "run_agent", fake_run_agent)
    return seen


async def test_gather_gets_the_gather_server_and_only_its_tools(captured) -> None:
    await megumi.gather("what needs my attention today")

    assert captured["allowed_tools"] == GATHER_TOOL_NAMES
    assert "gather" in captured["mcp_servers"]


async def test_summarise_mode_carries_no_tools(captured) -> None:
    """/compact must not spend tool calls or hand the summariser a way to
    fetch more untrusted content mid-summary."""
    await megumi.gather("summarise this", use_tools=False)

    assert captured["allowed_tools"] == []
    assert not captured["mcp_servers"]


async def test_carried_summary_is_wrapped_as_external_data(captured) -> None:
    """The summary distils a transcript that contained untrusted mail
    content - it re-enters the next session as data, never as bare prompt."""
    await megumi.gather("hello", summary="earlier we discussed the cert renewal")

    assert '<external-data source="conversation-summary">' in captured["prompt"]
    assert "earlier we discussed the cert renewal" in captured["prompt"]
    assert captured["prompt"].strip().startswith("Context")


async def test_system_prompt_declares_external_data_untrusted(captured) -> None:
    """The wrapper only works if the agent is told what it means."""
    await megumi.gather("hello")

    assert "external-data" in captured["system_prompt"]
    assert "untrusted" in captured["system_prompt"].lower()

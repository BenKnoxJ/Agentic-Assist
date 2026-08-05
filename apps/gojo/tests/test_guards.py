"""Tests for the two runaway guards — §9.3.

The doc says use both because they catch different failures: the timeout
catches a hung subprocess (no turns, no recursion, just never returns), the
budget catches an agent that is progressing but should stop. A test for one
is not a test for the other.
"""

import asyncio

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from gojo import orchestrator
from gojo.agents.runner import AgentResult
from gojo.config import Settings
from gojo.orchestrator import BUDGET_EXHAUSTED, GraphTimeout, run_turn


def settings_with(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


async def test_budget_exhausted_returns_gracefully_without_calling_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over budget must not invoke the SDK at all - that is the whole point."""
    called = False

    async def should_not_run(
        message: str, resume: str | None = None, summary: str = ""
    ) -> AgentResult:
        nonlocal called
        called = True
        return AgentResult(text="should never be seen")

    monkeypatch.setattr(orchestrator, "gather", should_not_run)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: settings_with())

    result = await orchestrator.megumi(
        {"message": "anything", "agent_calls": 5, "session_id": None}
    )

    assert called is False
    assert result["findings"] == [BUDGET_EXHAUSTED]
    assert result["steps"] == ["megumi:over-budget"]


async def test_under_budget_runs_and_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(
        message: str, resume: str | None = None, summary: str = ""
    ) -> AgentResult:
        return AgentResult(text="ok", session_id="s1")

    monkeypatch.setattr(orchestrator, "gather", fake)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: settings_with())

    result = await orchestrator.megumi(
        {"message": "anything", "agent_calls": 2, "session_id": None}
    )

    assert result["agent_calls"] == 3
    assert result["findings"] == ["ok"]


async def test_new_turn_resets_the_budget() -> None:
    """Without this the budget would be spent once and never refill."""
    assert orchestrator.new_turn({})["agent_calls"] == 0


async def test_timeout_raises_rather_than_hanging(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung agent must not hold the request open indefinitely."""

    async def never_returns(
        message: str, resume: str | None = None, summary: str = ""
    ) -> AgentResult:
        await asyncio.sleep(30)
        return AgentResult(text="too late")

    monkeypatch.setattr(orchestrator, "gather", never_returns)
    monkeypatch.setattr(
        orchestrator, "get_settings", lambda: settings_with(graph_timeout_seconds=0.3)
    )

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        with pytest.raises(GraphTimeout):
            await run_turn(graph, "hello", "conv-timeout")


async def test_recursion_limit_is_passed_explicitly(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """9.3 wants it set, not inherited, so a default change cannot move it."""
    seen: dict = {}

    class SpyGraph:
        async def ainvoke(self, initial, config):
            seen.update(config)
            return {"reply": "x", "steps": [], "findings": []}

    monkeypatch.setattr(
        orchestrator, "get_settings", lambda: settings_with(recursion_limit=7)
    )
    await run_turn(SpyGraph(), "hello", "conv-a")

    assert seen["recursion_limit"] == 7
    assert seen["configurable"]["thread_id"] == "conv-a"


async def test_an_empty_agent_answer_never_becomes_an_empty_reply(monkeypatch) -> None:
    """Teams rejects an empty message with 400 BadSyntax, and the SDK surfaces
    that into the chat as 'Exception caught'. Found live on 5 Aug 2026 when a
    split message made megumi return empty text ("" is falsy but [""] is not).
    """
    from gojo import orchestrator
    from gojo.agents.runner import AgentResult

    async def empty_gather(message: str, resume: str | None = None, summary: str = ""):
        return AgentResult(text="", session_id="s1")

    monkeypatch.setattr(orchestrator, "gather", empty_gather)
    graph = orchestrator.build_graph()

    result = await graph.ainvoke({"message": "hello", "steps": [], "findings": []})

    assert result["reply"].strip() != ""

"""Tests for the session commands.

Run against a real graph and a real checkpointer, because what these
commands do is manipulate persisted state — asserting against a mock would
test the mock.
"""

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from gojo import commands, orchestrator
from gojo.agents.runner import AgentResult
from gojo.commands import COMPACT_EMPTY, COMPACT_FAILED, HELP, NEW_DONE, is_command


@pytest.fixture
def gather(monkeypatch: pytest.MonkeyPatch):
    """Records prompts and returns a predictable session id per call."""
    calls: list[dict] = []

    async def fake(
        message: str,
        resume: str | None = None,
        summary: str = "",
        use_tools: bool = True,
    ) -> AgentResult:
        calls.append(
            {"message": message, "resume": resume, "summary": summary, "use_tools": use_tools}
        )
        return AgentResult(text=f"reply {len(calls)}", session_id=f"session-{len(calls)}")

    monkeypatch.setattr(orchestrator, "gather", fake)
    monkeypatch.setattr(commands, "gather", fake)
    return calls


async def send(graph, text: str, thread_id: str = "conv") -> dict:
    return await graph.ainvoke(
        {"message": text, "steps": [], "findings": []},
        {"configurable": {"thread_id": thread_id}},
    )


class TestRecognition:
    def test_slash_prefix_is_a_command(self) -> None:
        assert is_command("/new") is True

    def test_leading_whitespace_still_counts(self) -> None:
        assert is_command("  /compact") is True

    def test_a_question_is_not_a_command(self) -> None:
        assert is_command("what needs my attention today") is False

    def test_a_mid_sentence_slash_is_not_a_command(self) -> None:
        """Otherwise "and/or" style text would be swallowed as a command."""
        assert is_command("check the and/or logic") is False


async def test_unknown_command_returns_help_rather_than_asking_an_agent(
    tmp_path, gather
) -> None:
    """A typo must not silently become a prompt."""
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        assert await commands.handle(graph, "/compcat", "conv") == HELP
    assert gather == []


async def test_new_clears_the_session_so_the_next_turn_starts_fresh(
    tmp_path, gather
) -> None:
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await send(graph, "first")
        assert await commands.handle(graph, "/new", "conv") == NEW_DONE
        await send(graph, "second")

    # Turn two would have resumed session-1 without /new.
    assert [c["resume"] for c in gather] == [None, None]


async def test_compact_summarises_then_starts_a_fresh_session(tmp_path, gather) -> None:
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await send(graph, "first")
        reply = await commands.handle(graph, "/compact", "conv")
        await send(graph, "second")

    # The summarisation call resumed the live session...
    assert gather[1]["resume"] == "session-1"
    # ...tool-free: a summary must not spend tool calls or fetch more
    # untrusted content mid-summary (step 4).
    assert gather[1]["use_tools"] is False
    # ...and the turn after it started fresh, carrying the summary instead.
    assert gather[2]["resume"] is None
    assert gather[2]["summary"] == "reply 2"
    assert "reply 2" in reply


async def test_compact_on_an_empty_conversation_says_so(tmp_path, gather) -> None:
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        assert await commands.handle(graph, "/compact", "conv") == COMPACT_EMPTY
    assert gather == []


async def test_failed_summary_leaves_the_conversation_intact(
    tmp_path, monkeypatch: pytest.MonkeyPatch, gather
) -> None:
    """A failed compaction must not discard what it was meant to preserve."""
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await send(graph, "first")

        async def empty(message, resume=None, summary="", use_tools=True):
            return AgentResult(text="", session_id=None)

        monkeypatch.setattr(commands, "gather", empty)
        assert await commands.handle(graph, "/compact", "conv") == COMPACT_FAILED

        state = await graph.aget_state({"configurable": {"thread_id": "conv"}})

    assert state.values["session_id"] == "session-1"


async def test_new_on_one_thread_does_not_affect_another(tmp_path, gather) -> None:
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await send(graph, "hello", "conv-a")
        await send(graph, "hello", "conv-b")
        await commands.handle(graph, "/new", "conv-a")
        await send(graph, "again", "conv-b")

    # conv-b resumed normally despite /new on conv-a.
    assert gather[2]["resume"] == "session-2"


async def test_new_clears_the_conversations_owed_replies(tmp_path) -> None:
    """A discarded conversation must not be answered afterwards.

    The recovery guard matches turn ids, and aupdate_state does not change
    one - so /new has to say so explicitly (ADR 0008).
    """
    import aiosqlite

    from gojo import outbox

    class FakeGraph:
        async def aupdate_state(self, config, values):
            return None

    async with aiosqlite.connect(str(tmp_path / "cp.sqlite")) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", "{}")
        await outbox.record(conn, "turn2", "conv-b", "{}")

        await commands.handle(FakeGraph(), "/new", "conv-a", conn)

        assert [r.turn_id for r in await outbox.list_owed(conn)] == ["turn2"]


async def test_new_without_an_outbox_still_works() -> None:
    """/chat passes no connection."""

    class FakeGraph:
        async def aupdate_state(self, config, values):
            return None

    assert await commands.handle(FakeGraph(), "/new", "conv-a") == NEW_DONE

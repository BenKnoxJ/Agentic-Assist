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
        async def aget_state(self, config):
            from types import SimpleNamespace

            return SimpleNamespace(next=(), interrupts=(), values={})

        async def aupdate_state(self, config, values, as_node=None):
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
        async def aget_state(self, config):
            from types import SimpleNamespace

            return SimpleNamespace(next=(), interrupts=(), values={})

        async def aupdate_state(self, config, values, as_node=None):
            return None

    assert await commands.handle(FakeGraph(), "/new", "conv-a") == NEW_DONE


# --- Step 5: commands on a gate-paused thread (ADR 0011) ---

VALID_PROPOSAL_JSON = (
    '{"op": "draft", "kind": "new", "to": ["amy@example.org"], '
    '"subject": "Setup session", "body": "Hi Amy."}'
)


@pytest.fixture
def composing(monkeypatch: pytest.MonkeyPatch):
    async def fake(message, resume=None, summary=""):
        return AgentResult(text=VALID_PROPOSAL_JSON, session_id="s-sukuna")

    monkeypatch.setattr(orchestrator, "compose", fake)


@pytest.fixture
async def ledger(tmp_path):
    import aiosqlite

    from gojo import actions

    conn = await aiosqlite.connect(tmp_path / "actions.sqlite")
    await actions.create_table(conn)
    actions.use_connection(conn)
    yield conn
    await conn.close()


async def pause_gate(graph) -> None:
    result = await orchestrator.run_locked(
        graph, "draft an email to amy about setup", "conv"
    )
    assert "__interrupt__" in result


async def test_new_on_a_paused_gate_cancels_it_first(
    tmp_path, gather, composing, ledger
) -> None:
    """M1 ordering: the gate check must run BEFORE the values-update, which
    would blind snapshot.interrupts while leaving the gate resumable."""

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await pause_gate(graph)

        assert await commands.handle(graph, "/new", "conv") == NEW_DONE

        snapshot = await graph.aget_state({"configurable": {"thread_id": "conv"}})
        assert not orchestrator.gate_pending(snapshot)

    rows = [r async for r in await ledger.execute("SELECT status FROM actions")]
    assert [r[0] for r in rows] == ["cancelled"]


async def test_compact_on_a_paused_gate_refuses(
    tmp_path, gather, composing, ledger
) -> None:
    """Compacting would blind the gate guards (M1) - refuse until the
    pending action is decided."""
    from gojo.commands import COMPACT_BLOCKED

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await pause_gate(graph)
        summarise_calls_before = len(gather)

        assert await commands.handle(graph, "/compact", "conv") == COMPACT_BLOCKED

        assert len(gather) == summarise_calls_before  # no summary was attempted
        snapshot = await graph.aget_state({"configurable": {"thread_id": "conv"}})
        assert orchestrator.gate_pending(snapshot)  # the gate is untouched

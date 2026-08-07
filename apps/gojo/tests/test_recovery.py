"""Tests for resuming an interrupted turn and delivering what was owed.

ADR 0008. The properties: a turn that died mid-graph is finished from its
checkpoint rather than replayed; the turn id that identifies it is stable
across that turn's own progress and changes on the next.
"""

import asyncio

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from microsoft_agents.activity import (
    Activity,
    ActivityTypes,
    ChannelAccount,
    ConversationAccount,
)

from gojo import orchestrator, outbox
from gojo.agents.runner import AgentResult
from gojo.recovery import recover_owed_replies
from gojo.teams import FAILED

AGENT_ID = "2b6bad70-0000-0000-0000-000000000000"


class CrashOnceGather:
    """Fails the first call, succeeds afterwards. Stands in for a crash."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(
        self, message: str, resume: str | None = None, summary: str = ""
    ) -> AgentResult:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated crash inside megumi")
        return AgentResult(text=f"answer to {message}", session_id="s1")


@pytest.fixture
def crashing(monkeypatch: pytest.MonkeyPatch) -> CrashOnceGather:
    recorder = CrashOnceGather()
    monkeypatch.setattr(orchestrator, "gather", recorder)
    return recorder


async def crash_a_turn(graph, thread_id: str = "conv-a", message: str = "OLD") -> None:
    """Run a turn that dies inside megumi, leaving the thread pending."""
    with pytest.raises(RuntimeError):
        await graph.ainvoke(
            {"message": message, "steps": [], "findings": []},
            {"configurable": {"thread_id": thread_id}},
        )


async def state_of(graph, thread_id: str = "conv-a"):
    return await graph.aget_state({"configurable": {"thread_id": thread_id}})


async def test_new_turn_stamps_the_current_turn_id(tmp_path, crashing) -> None:
    """The guard depends on this being in state at all."""
    from gojo.logs import new_turn_id

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        expected = new_turn_id()
        await crash_a_turn(graph)

        assert (await state_of(graph)).values["turn_id"] == expected


async def test_the_turn_id_is_stable_while_the_turn_progresses(tmp_path) -> None:
    """Revision 2's bug. A checkpoint id moves here; a turn id must not.

    The turn runs in a background task exactly as teams.py runs it, and the id
    is read once mid-flight (where the ACK happens) and once after completion
    (where the crash happens). Those must agree, or recovery abandons a reply
    it should deliver.
    """
    from gojo.logs import new_turn_id

    async def slow(message: str, resume: str | None = None, summary: str = ""):
        await asyncio.sleep(0.4)
        return AgentResult(text="the answer", session_id="s1")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(orchestrator, "gather", slow)
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
            graph = orchestrator.build_graph(checkpointer=cp)
            new_turn_id()
            task = asyncio.create_task(
                graph.ainvoke(
                    {"message": "Q", "steps": [], "findings": []},
                    {"configurable": {"thread_id": "conv-a"}},
                )
            )
            await asyncio.sleep(0.15)
            at_ack = (await state_of(graph)).values["turn_id"]
            await task
            at_crash = (await state_of(graph)).values["turn_id"]

    assert at_ack == at_crash


async def test_a_new_turn_changes_the_turn_id(tmp_path, crashing) -> None:
    """Which is what lets recovery tell 'still waiting' from 'moved on'."""
    from gojo.logs import new_turn_id

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        new_turn_id()
        await crash_a_turn(graph)
        first = (await state_of(graph)).values["turn_id"]

        new_turn_id()
        await graph.ainvoke(
            {"message": "NEW", "steps": [], "findings": []},
            {"configurable": {"thread_id": "conv-a"}},
        )

        assert (await state_of(graph)).values["turn_id"] != first


async def test_resume_finishes_a_turn_that_died_mid_graph(tmp_path, crashing) -> None:
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await crash_a_turn(graph)

        result = await orchestrator.resume_turn(graph, "conv-a")

    assert result["reply"] == "answer to OLD"


async def test_resume_does_not_replay_a_completed_turn(tmp_path, crashing) -> None:
    """A crash between finishing and delivering must not re-pay for the agent."""
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await crash_a_turn(graph)
        await orchestrator.resume_turn(graph, "conv-a")
        calls = crashing.calls

        second = await orchestrator.resume_turn(graph, "conv-a")

    assert second["reply"] == "answer to OLD"
    assert crashing.calls == calls


async def test_resume_applies_the_wall_clock_guard(
    tmp_path, crashing, monkeypatch
) -> None:
    """9.3's timeout must cover the recovery path, not just live turns.

    Deterministic shape: the crash leaves a pending thread with checkpoints
    on disk, then the resume runs against an agent that hangs. (An earlier
    version cancelled a live task instead, which raced the first checkpoint
    write and flaked with EmptyInputError when the cancel won.)

    Patches orchestrator.get_settings the way test_guards.py does, rather
    than mutating the lru_cached singleton built from the developer's real
    .env.
    """
    from gojo.config import Settings

    async def hangs(message: str, resume: str | None = None, summary: str = ""):
        await asyncio.sleep(60)

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await crash_a_turn(graph)

        monkeypatch.setattr(orchestrator, "gather", hangs)
        monkeypatch.setattr(
            orchestrator,
            "get_settings",
            lambda: Settings(_env_file=None, graph_timeout_seconds=0.2),
        )

        with pytest.raises(orchestrator.GraphTimeout):
            await orchestrator.resume_turn(graph, "conv-a")


async def test_overlapping_turns_serialise_instead_of_forking(tmp_path) -> None:
    """Two messages back to back must queue, not corrupt the thread (ADR 0009)."""
    from gojo.logs import new_turn_id

    order: list[str] = []

    async def tracked(message: str, resume: str | None = None, summary: str = ""):
        order.append(f"start:{message}")
        await asyncio.sleep(0.2)
        order.append(f"end:{message}")
        return AgentResult(text=f"ans:{message}", session_id=f"s:{message}")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(orchestrator, "gather", tracked)
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
            graph = orchestrator.build_graph(checkpointer=cp)
            new_turn_id()
            t1 = asyncio.create_task(orchestrator.run_locked(graph, "A", "conv-a"))
            await asyncio.sleep(0.05)
            new_turn_id()
            t2 = asyncio.create_task(orchestrator.run_locked(graph, "B", "conv-a"))
            await t1
            await t2

    # A finishes entirely before B starts - no interleaving, no fork.
    assert order == ["start:A", "end:A", "start:B", "end:B"]


async def test_new_during_a_turn_is_not_reverted(tmp_path) -> None:
    """The live bug that motivated ADR 0009: without serialisation, the
    in-flight turn's final writes land after /new's aupdate_state and
    silently resurrect the session the user discarded. Under the lock, /new
    waits - answer first, then forget, and forget sticks.
    """
    from gojo import commands
    from gojo.logs import new_turn_id

    async def slow(message: str, resume: str | None = None, summary: str = ""):
        await asyncio.sleep(0.3)
        return AgentResult(text="the answer", session_id="s-live")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(orchestrator, "gather", slow)
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
            graph = orchestrator.build_graph(checkpointer=cp)
            new_turn_id()
            task = asyncio.create_task(
                orchestrator.run_locked(graph, "hello", "conv-a")
            )
            await asyncio.sleep(0.1)

            reply = await commands.handle(graph, "/new", "conv-a")
            await task
            state = await graph.aget_state(
                {"configurable": {"thread_id": "conv-a"}}
            )

    assert reply == commands.NEW_DONE
    assert state.values.get("session_id") is None


def a_reference(conversation_id: str = "conv-a"):
    activity = Activity(
        type=ActivityTypes.message,
        text="hello",
        channel_id="msteams",
        service_url="https://smba.trafficmanager.net/uk/",
        from_property=ChannelAccount(id="29:user", name="Ben Knox"),
        recipient=ChannelAccount(id="28:bot", name="Gojo"),
        conversation=ConversationAccount(id=conversation_id, tenant_id="tenant"),
    )
    return activity.get_conversation_reference()


class RecordingAdapter:
    """Captures what was sent, by running the callback the real adapter runs."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.continuations: list[object] = []

    async def continue_conversation(self, agent_id, continuation, callback):
        self.continuations.append(continuation)

        class Ctx:
            def __init__(self, sink):
                self._sink = sink

            async def send_activity(self, text):
                self._sink.append(text)

        await callback(Ctx(self.sent))


class BrokenAdapter:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def continue_conversation(self, agent_id, continuation, callback):
        raise RuntimeError("service url unreachable")


async def owe(conn, turn_id: str, thread_id: str = "conv-a") -> None:
    """Record a debt the way teams.py does - from the turn id, not from state.

    ⚠ Deliberately does NOT read the graph. Revision 2's helper captured the
    guard value from the graph after the crash, which made every recovery test
    agree with whatever recovery later observed. The tests passed and the
    mechanism was broken.
    """
    await outbox.create_table(conn)
    await outbox.record(conn, turn_id, thread_id, a_reference(thread_id).model_dump_json())


async def test_a_crashed_turn_is_resumed_and_delivered(tmp_path, crashing) -> None:
    """The whole point of ADR 0008, end to end."""
    from gojo.logs import new_turn_id

    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        turn = new_turn_id()
        await crash_a_turn(graph)

        async with aiosqlite.connect(db) as conn:
            await owe(conn, turn)

            assert await recover_owed_replies(conn, graph, adapter, AGENT_ID) == 1
            assert adapter.sent == ["answer to OLD"]
            assert await outbox.list_owed(conn) == []


async def test_a_completed_turn_is_still_delivered(tmp_path) -> None:
    """Revision 2's critical bug, as a regression test.

    The crash lands *after* the graph finished. A checkpoint-based guard sees
    a moved pin here and abandons the reply; a turn-id guard delivers it.
    """
    from gojo.logs import new_turn_id

    async def ok(message: str, resume: str | None = None, summary: str = ""):
        return AgentResult(text="the answer", session_id="s1")

    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(orchestrator, "gather", ok)
        async with AsyncSqliteSaver.from_conn_string(db) as cp:
            graph = orchestrator.build_graph(checkpointer=cp)
            turn = new_turn_id()
            await graph.ainvoke(
                {"message": "Q", "steps": [], "findings": []},
                {"configurable": {"thread_id": "conv-a"}},
            )

            async with aiosqlite.connect(db) as conn:
                await owe(conn, turn)

                assert await recover_owed_replies(conn, graph, adapter, AGENT_ID) == 1
                assert adapter.sent == ["the answer"]


async def test_a_moved_on_thread_is_abandoned_not_delivered_twice(
    tmp_path, crashing
) -> None:
    """Unguarded, ainvoke(None) returns the NEW turn's state and it is sent."""
    from gojo.logs import new_turn_id

    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        turn = new_turn_id()
        await crash_a_turn(graph)

        async with aiosqlite.connect(db) as conn:
            await owe(conn, turn)

            new_turn_id()
            await graph.ainvoke(
                {"message": "NEW", "steps": [], "findings": []},
                {"configurable": {"thread_id": "conv-a"}},
            )

            assert await recover_owed_replies(conn, graph, adapter, AGENT_ID) == 0
            assert adapter.sent == []
            assert await outbox.list_owed(conn) == []


async def test_new_during_recovery_waits_for_the_lock(tmp_path, crashing) -> None:
    """/new issued while recovery holds the thread queues behind it (ADR 0009).

    The delivered answer arrives first, then the conversation is forgotten
    and its rows are gone. Revision 4 tried to solve this window with a
    pre-delivery re-check; the lock removes the window instead.
    """
    from gojo import commands
    from gojo.logs import new_turn_id

    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        turn = new_turn_id()
        await crash_a_turn(graph)

        async with aiosqlite.connect(db) as conn:
            await owe(conn, turn)

            recovery = asyncio.create_task(
                recover_owed_replies(conn, graph, adapter, AGENT_ID)
            )
            await asyncio.sleep(0.05)
            # /new lands while recovery is mid-resume. It must wait.
            new_cmd = asyncio.create_task(
                commands.handle(graph, "/new", "conv-a", conn)
            )

            assert await recovery == 1
            await new_cmd

            assert adapter.sent == ["answer to OLD"]
            assert await outbox.list_owed(conn) == []


async def test_one_bad_row_does_not_stop_the_others(tmp_path, crashing) -> None:
    """Otherwise a single corrupt row blocks recovery on every future boot."""
    from gojo.logs import new_turn_id

    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        turn = new_turn_id()
        await crash_a_turn(graph)

        async with aiosqlite.connect(db) as conn:
            await outbox.create_table(conn)
            # Sorts first, and its reference will not deserialise.
            await outbox.record(conn, "aaa-bad", "conv-a", "not json at all")
            await owe(conn, turn)

            delivered = await recover_owed_replies(conn, graph, adapter, AGENT_ID)

            assert delivered == 1
            assert adapter.sent == ["answer to OLD"]
            assert await outbox.list_owed(conn) == []


async def test_a_stale_reply_is_abandoned(tmp_path, crashing, monkeypatch) -> None:
    """An answer to a question asked hours ago is noise, not service."""
    import gojo.recovery as recovery_module
    from gojo.config import Settings
    from gojo.logs import new_turn_id

    monkeypatch.setattr(
        recovery_module,
        "get_settings",
        lambda: Settings(_env_file=None, owed_reply_max_age_seconds=0.0),
    )

    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        turn = new_turn_id()
        await crash_a_turn(graph)

        async with aiosqlite.connect(db) as conn:
            await owe(conn, turn)

            assert await recover_owed_replies(conn, graph, adapter, AGENT_ID) == 0
            assert adapter.sent == []
            assert await outbox.list_owed(conn) == []


async def test_nothing_owed_delivers_nothing(tmp_path) -> None:
    adapter = RecordingAdapter()

    async with aiosqlite.connect(str(tmp_path / "cp.sqlite")) as conn:
        await outbox.create_table(conn)

        assert await recover_owed_replies(conn, None, adapter, AGENT_ID) == 0
        assert adapter.sent == []


async def test_failed_delivery_is_retried_on_a_later_pass(tmp_path, crashing) -> None:
    """Two passes, because one pass proves nothing about retry."""
    from gojo.logs import new_turn_id

    db = str(tmp_path / "cp.sqlite")

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        turn = new_turn_id()
        await crash_a_turn(graph)

        async with aiosqlite.connect(db) as conn:
            await owe(conn, turn)

            assert await recover_owed_replies(conn, graph, BrokenAdapter(), AGENT_ID) == 0
            assert (await outbox.list_owed(conn))[0].attempts == 1

            working = RecordingAdapter()
            assert await recover_owed_replies(conn, graph, working, AGENT_ID) == 1
            assert working.sent == ["answer to OLD"]
            assert await outbox.list_owed(conn) == []


async def test_an_exhausted_reply_is_abandoned(tmp_path, crashing) -> None:
    from gojo.logs import new_turn_id

    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        turn = new_turn_id()
        await crash_a_turn(graph)

        async with aiosqlite.connect(db) as conn:
            await owe(conn, turn)
            for _ in range(outbox.MAX_ATTEMPTS):
                await outbox.bump_attempts(conn, turn)

            assert await recover_owed_replies(conn, graph, adapter, AGENT_ID) == 0
            assert await outbox.list_owed(conn) == []
            assert adapter.sent == []


async def test_a_turn_that_cannot_resume_still_gets_an_answer(
    tmp_path, crashing
) -> None:
    """The promise was a reply, not a correct one (ADR 0008)."""
    from gojo.logs import new_turn_id

    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    class ExplodingGraph:
        def __init__(self, real):
            self._real = real

        async def aget_state(self, config):
            return await self._real.aget_state(config)

        async def ainvoke(self, _input, _config):
            raise RuntimeError("checkpoint unreadable")

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        turn = new_turn_id()
        await crash_a_turn(graph)

        async with aiosqlite.connect(db) as conn:
            await owe(conn, turn)

            assert (
                await recover_owed_replies(conn, ExplodingGraph(graph), adapter, AGENT_ID)
                == 1
            )
            assert adapter.sent == [FAILED]
            assert await outbox.list_owed(conn) == []


async def test_recovery_delivers_an_activity_not_a_reference(
    tmp_path, crashing
) -> None:
    """Regression cover for the bug test_teams_delivery.py documents."""
    from microsoft_agents.activity import ConversationReference

    from gojo.logs import new_turn_id

    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        turn = new_turn_id()
        await crash_a_turn(graph)

        async with aiosqlite.connect(db) as conn:
            await owe(conn, turn)
            await recover_owed_replies(conn, graph, adapter, AGENT_ID)

    assert isinstance(adapter.continuations[0], Activity)
    assert not isinstance(adapter.continuations[0], ConversationReference)


async def test_recovery_restores_the_original_turn_id(tmp_path, crashing) -> None:
    """`grep turn=<id>` must span the crash and the recovery."""
    from gojo.logs import new_turn_id
    from gojo.logs import turn_id as turn_id_var

    db = str(tmp_path / "cp.sqlite")
    seen: list[str] = []

    class Watcher(RecordingAdapter):
        async def continue_conversation(self, agent_id, continuation, callback):
            seen.append(turn_id_var.get())
            await super().continue_conversation(agent_id, continuation, callback)

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        turn = new_turn_id()
        await crash_a_turn(graph)

        async with aiosqlite.connect(db) as conn:
            await owe(conn, turn)
            await recover_owed_replies(conn, graph, Watcher(), AGENT_ID)

    assert seen == [turn]


async def test_recovery_never_raises_on_a_broken_table(tmp_path) -> None:
    """It runs unattended at boot in a task nothing awaits (10.4)."""
    adapter = RecordingAdapter()

    async with aiosqlite.connect(str(tmp_path / "cp.sqlite")) as conn:
        # No create_table: every query against it fails.
        assert await recover_owed_replies(conn, None, adapter, AGENT_ID) == 0


# --- Step 5: recovery on a gate-paused thread (ADR 0011) ---

VALID_PROPOSAL_JSON = (
    '{"op": "draft", "kind": "new", "to": ["amy@example.org"], '
    '"subject": "Setup session", "body": "Hi Amy."}'
)


def _text_of(sent_item) -> str:
    """RecordingAdapter receives str for plain replies, an Activity when a
    card rides along - the prompt text is present either way."""
    return getattr(sent_item, "text", sent_item)


@pytest.fixture
def composing(monkeypatch: pytest.MonkeyPatch):
    """A compose stub that pauses turns at the gate."""

    async def fake(message, resume=None, summary=""):
        return AgentResult(text=VALID_PROPOSAL_JSON, session_id="s-sukuna")

    monkeypatch.setattr(orchestrator, "compose", fake)


async def test_a_gate_paused_thread_gets_its_prompt_redelivered_not_failed(
    tmp_path, composing
) -> None:
    """The T9 hole: resume_turn on a paused gate re-raises the interrupt,
    reads an empty reply, delivers FAILED and destroys the debt. The right
    move is re-delivering the approval prompt - the promise is re-issued
    as the question it always was."""
    from gojo import actions
    from gojo.logs import new_turn_id

    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)

        async with aiosqlite.connect(db) as conn:
            await actions.create_table(conn)
            actions.use_connection(conn)

            turn = new_turn_id()
            result = await orchestrator.run_locked(
                graph, "draft an email to amy about setup", "conv-a"
            )
            assert "__interrupt__" in result  # the gate is genuinely paused

            await owe(conn, turn)
            assert await recover_owed_replies(conn, graph, adapter, AGENT_ID) == 1

            text = _text_of(adapter.sent[0])
            assert "Setup session" in text  # the prompt, verbatim payload
            assert text != FAILED
            assert await outbox.list_owed(conn) == []

            # The gate itself is untouched - the next message decides it.
            snapshot = await graph.aget_state(
                {"configurable": {"thread_id": "conv-a"}}
            )
            assert orchestrator.gate_pending(snapshot)


class CrashOnceCompose:
    """Crashes the first compose call - the likeliest real crash point."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, message, resume=None, summary=""):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("process died mid-compose")
        return AgentResult(text=VALID_PROPOSAL_JSON, session_id="s-sukuna")


async def test_a_resume_that_runs_into_the_gate_delivers_the_prompt(
    tmp_path, monkeypatch
) -> None:
    """Review M2: the crash was mid-sukuna, so at boot there is no pending
    interrupt - the RESUME ITSELF runs into the gate. The result carries
    __interrupt__ and must become the approval prompt, never FAILED."""
    from gojo import actions
    from gojo.logs import new_turn_id

    crashing = CrashOnceCompose()
    monkeypatch.setattr(orchestrator, "compose", crashing)

    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)

        async with aiosqlite.connect(db) as conn:
            await actions.create_table(conn)
            actions.use_connection(conn)

            turn = new_turn_id()
            with pytest.raises(RuntimeError):
                await orchestrator.run_locked(
                    graph, "draft an email to amy about setup", "conv-a"
                )

            await owe(conn, turn)
            assert await recover_owed_replies(conn, graph, adapter, AGENT_ID) == 1

            text = _text_of(adapter.sent[0])
            assert "Setup session" in text
            assert text != FAILED
            assert crashing.calls == 2  # resumed through sukuna into the gate

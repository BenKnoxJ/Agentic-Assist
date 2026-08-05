"""Tests for resuming an interrupted turn and delivering what was owed.

ADR 0008. The properties: a turn that died mid-graph is finished from its
checkpoint rather than replayed; the turn id that identifies it is stable
across that turn's own progress and changes on the next.
"""

import asyncio

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from gojo import orchestrator
from gojo.agents.runner import AgentResult


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


async def test_resume_applies_the_wall_clock_guard(tmp_path, monkeypatch) -> None:
    """9.3's timeout must cover the recovery path, not just live turns.

    Patches orchestrator.get_settings the way test_guards.py does, rather than
    mutating the lru_cached singleton built from the developer's real .env.
    """
    from gojo.config import Settings

    async def hangs(message: str, resume: str | None = None, summary: str = ""):
        await asyncio.sleep(60)

    monkeypatch.setattr(orchestrator, "gather", hangs)
    monkeypatch.setattr(
        orchestrator,
        "get_settings",
        lambda: Settings(_env_file=None, graph_timeout_seconds=0.2),
    )

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        config = {"configurable": {"thread_id": "conv-a"}}
        task = asyncio.create_task(
            graph.ainvoke({"message": "q", "steps": [], "findings": []}, config)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

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

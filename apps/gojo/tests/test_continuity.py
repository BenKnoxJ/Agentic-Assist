"""Tests for conversation continuity — build step 3.

Exercises the real AsyncSqliteSaver against a temp file rather than an
in-memory saver, because persistence across a restart is the property being
bought and an in-memory checkpointer cannot demonstrate it.
"""

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from gojo import orchestrator
from gojo.agents.runner import AgentResult


class RecordingGather:
    """Stands in for the Agent SDK and records what resume it was handed."""

    def __init__(self) -> None:
        self.resumes: list[str | None] = []
        self.turn = 0

    async def __call__(
        self, message: str, resume: str | None = None, summary: str = ""
    ) -> AgentResult:
        self.resumes.append(resume)
        self.turn += 1
        return AgentResult(text=f"answer {self.turn}", session_id=f"session-{self.turn}")


@pytest.fixture
def gather(monkeypatch: pytest.MonkeyPatch) -> RecordingGather:
    recorder = RecordingGather()
    monkeypatch.setattr(orchestrator, "gather", recorder)
    return recorder


def thread(name: str) -> dict:
    return {"configurable": {"thread_id": name}}


async def send(graph, text: str, thread_id: str) -> dict:
    return await graph.ainvoke(
        {"message": text, "steps": [], "findings": []}, thread(thread_id)
    )


async def test_second_turn_resumes_the_first_session(tmp_path, gather) -> None:
    """The point of step 3: turn two continues turn one's conversation."""
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await send(graph, "first question", "conv-a")
        await send(graph, "second question", "conv-a")

    # Turn one starts fresh; turn two resumes what turn one produced.
    assert gather.resumes == [None, "session-1"]


async def test_separate_conversations_do_not_share_a_session(tmp_path, gather) -> None:
    """Thread ids are the Teams conversation id - they must not leak."""
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await send(graph, "hello", "conv-a")
        await send(graph, "hello", "conv-b")

    assert gather.resumes == [None, None]


async def test_state_survives_rebuilding_the_graph(tmp_path, gather) -> None:
    """Standing in for a process restart: same file, new graph object."""
    path = str(tmp_path / "cp.sqlite")

    async with AsyncSqliteSaver.from_conn_string(path) as cp:
        await send(orchestrator.build_graph(checkpointer=cp), "before", "conv-a")

    async with AsyncSqliteSaver.from_conn_string(path) as cp:
        await send(orchestrator.build_graph(checkpointer=cp), "after", "conv-a")

    assert gather.resumes == [None, "session-1"]


async def test_steps_do_not_accumulate_across_turns(tmp_path, gather) -> None:
    """Without the new_turn reset these lists grow for the whole conversation.

    That is 6.3 rule 3's unbounded state growth arriving quietly, so it gets
    an explicit test rather than trust in a reducer.
    """
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        first = await send(graph, "one", "conv-a")
        second = await send(graph, "two", "conv-a")
        third = await send(graph, "three", "conv-a")

    assert first["steps"] == ["classify", "megumi", "respond"]
    assert second["steps"] == ["classify", "megumi", "respond"]
    assert third["steps"] == ["classify", "megumi", "respond"]
    assert third["findings"] == ["answer 3"]


async def test_reply_reflects_only_the_current_turn(tmp_path, gather) -> None:
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await send(graph, "one", "conv-a")
        second = await send(graph, "two", "conv-a")

    assert second["reply"] == "answer 2"

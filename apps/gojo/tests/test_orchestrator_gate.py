"""Tests for the approval gate - the step-5 core.

Real graph, real AsyncSqliteSaver, real ledger; only the compose agent and
the Graph write client are faked. These are the tests the plan marks
never-sacrifice: the interrupt shape, the byte-equality of what executes
versus what was approved, and the guards' immunity to the verified
langgraph footguns (state updates blinding snapshot.interrupts; double
resume silently returning old state).
"""

import json

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from gojo import actions, orchestrator
from gojo.agents.runner import AgentResult
from gojo.orchestrator import (
    gate_pending,
    resume_gate_locked,
    run_locked,
)

CONFIG = {"configurable": {"thread_id": "conv"}}

VALID_DRAFT_JSON = json.dumps(
    {
        "op": "draft",
        "kind": "new",
        "to": ["amy@example.org"],
        "subject": "Setup session",
        "body": "Hi Amy — Thursday works.",
    }
)

REPLY_DRAFT_JSON = json.dumps(
    {
        "op": "draft",
        "kind": "reply",
        "reply_to_message_id": "AAMkAGI2TAAA=",
        "body": "Thursday works.",
    }
)


class FakeWriteClient:
    def __init__(self):
        self.create_draft_calls: list[dict] = []
        self.reply_draft_calls: list[dict] = []
        self.get_message_calls: list[str] = []

    async def get_message(self, message_id):
        self.get_message_calls.append(message_id)
        return {
            "id": message_id,
            "from": "Amy Whalen <amy@jrht.example>",
            "subject": "Onboarding go-ahead",
            "receivedDateTime": "2026-08-06T10:00:00Z",
            "bodyPreview": "Happy to progress...",
            "isRead": True,
            "importance": "normal",
            "hasAttachments": False,
        }

    async def create_draft(self, to, subject, body):
        self.create_draft_calls.append({"to": to, "subject": subject, "body": body})
        return "draft-1"

    async def create_reply_draft(self, message_id, body):
        self.reply_draft_calls.append({"message_id": message_id, "body": body})
        return "reply-draft-1"

    async def send_draft(self, message_id):
        raise AssertionError("draft ops must never send")


@pytest.fixture
def write_client(monkeypatch) -> FakeWriteClient:
    client = FakeWriteClient()
    monkeypatch.setattr(actions, "write_client", lambda: client)
    return client


@pytest.fixture
async def ledger(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "actions.sqlite")
    await actions.create_table(conn)
    actions.use_connection(conn)
    yield conn
    await conn.close()


@pytest.fixture
def compose(monkeypatch):
    """Set what the fake compose agent outputs."""
    output = {"text": VALID_DRAFT_JSON}

    async def fake(message, resume=None, summary=""):
        return AgentResult(text=output["text"], session_id="sukuna-session")

    monkeypatch.setattr(orchestrator, "compose", fake)

    def set_output(text: str) -> None:
        output["text"] = text

    return set_output


async def pause_at_gate(graph, message="draft an email to amy about setup"):
    result = await run_locked(graph, message, "conv")
    return result


async def approve_in_ledger(result) -> str:
    """What the teams handler does before resuming: mark approved."""
    action_id = result["__interrupt__"][0].value["action_id"]
    await actions.mark(actions.connection(), action_id, "approved")
    return action_id


async def test_act_turn_pauses_at_the_gate(tmp_path, ledger, write_client, compose):
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        result = await pause_at_gate(graph)

        interrupts = result["__interrupt__"]  # a list in ainvoke results
        assert isinstance(interrupts, list) and interrupts
        value = interrupts[0].value
        assert value["payload"]["subject"] == "Setup session"
        assert value["action_id"]

        snapshot = await graph.aget_state(CONFIG)
        assert "gate" in snapshot.next
        row = await actions.get(ledger, value["action_id"])
        assert row.status == "proposed"


async def test_reply_target_is_verified_deterministically(
    tmp_path, ledger, write_client, compose
):
    """M3: the human is shown what get_message returned for the id, never
    what the agent claimed about it."""
    compose(REPLY_DRAFT_JSON)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        result = await pause_at_gate(graph, "draft a reply to amy")

        assert write_client.get_message_calls == ["AAMkAGI2TAAA="]
        target = result["__interrupt__"][0].value["verified_target"]
        assert target == {
            "from": "Amy Whalen <amy@jrht.example>",
            "subject": "Onboarding go-ahead",
        }


async def test_approve_executes_the_approved_bytes(
    tmp_path, ledger, write_client, compose
):
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        paused = await pause_at_gate(graph)
        action_id = await approve_in_ledger(paused)

        result = await resume_gate_locked(graph, "approve", "conv")

        assert result is not None
        assert "Draft" in result["reply"]
        assert result["steps"][-3:] == ["gate", "execute", "respond"]
        # Byte equality: what executes is what was approved.
        assert write_client.create_draft_calls == [
            {
                "to": ["amy@example.org"],
                "subject": "Setup session",
                "body": "Hi Amy — Thursday works.",
            }
        ]
        row = await actions.get(ledger, action_id)
        assert row.status == "executed" and row.result_id == "draft-1"


async def test_reject_discards_without_touching_the_client(
    tmp_path, ledger, write_client, compose
):
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        await pause_at_gate(graph)

        result = await resume_gate_locked(graph, "reject", "conv")

        assert result is not None
        assert "discarded" in result["reply"].lower()
        assert write_client.create_draft_calls == []


async def test_malformed_proposal_fails_safe(tmp_path, ledger, write_client, compose):
    compose("ABSTAIN")
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        result = await pause_at_gate(graph)

        assert "__interrupt__" not in result
        assert "couldn't put together" in result["reply"]
        assert await actions.stale_open_rows(ledger) == []


async def test_gate_survives_process_restart(tmp_path, ledger, write_client, compose):
    """The ADR 0008 story extended: the approval can arrive at a different
    process than the one that asked."""
    db = str(tmp_path / "c.sqlite")
    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        paused = await pause_at_gate(graph)
        action_id = await approve_in_ledger(paused)

    # New saver + new graph object = new process.
    async with AsyncSqliteSaver.from_conn_string(db) as cp2:
        graph2 = orchestrator.build_graph(checkpointer=cp2)
        result = await resume_gate_locked(graph2, "approve", "conv")

        assert result is not None and "Draft" in result["reply"]
        assert (await actions.get(ledger, action_id)).status == "executed"


async def test_guard_survives_state_updates_that_blind_interrupts(
    tmp_path, ledger, write_client, compose
):
    """M1, verified against langgraph 1.2.10: aupdate_state with values
    empties snapshot.interrupts while the gate stays resumable. The guard
    must key on snapshot.next as well."""
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        paused = await pause_at_gate(graph)

        await graph.aupdate_state(CONFIG, {"summary": "unrelated"})
        snapshot = await graph.aget_state(CONFIG)

        assert gate_pending(snapshot) is True  # even though...
        assert not snapshot.interrupts  # ...this went blind

        await approve_in_ledger(paused)
        result = await resume_gate_locked(graph, "approve", "conv")
        assert result is not None and "Draft" in result["reply"]


async def test_double_resume_is_refused_not_replayed(
    tmp_path, ledger, write_client, compose
):
    """B1: Command(resume=...) on a thread with no pending interrupt silently
    returns the old final state - a double card tap must not re-deliver it."""
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        paused = await pause_at_gate(graph)
        await approve_in_ledger(paused)

        first = await resume_gate_locked(graph, "approve", "conv")
        second = await resume_gate_locked(graph, "approve", "conv")

        assert first is not None
        assert second is None  # not pending -> refused
        assert len(write_client.create_draft_calls) == 1


async def test_gather_path_is_unchanged(tmp_path, ledger, write_client, monkeypatch):
    async def fake_gather(message, resume=None, summary="", use_tools=True):
        return AgentResult(text="all quiet", session_id="s1")

    monkeypatch.setattr(orchestrator, "gather", fake_gather)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        result = await run_locked(graph, "what needs my attention today", "conv")

        assert result["reply"] == "all quiet"
        assert result["steps"] == ["classify", "megumi", "respond"]
        assert "__interrupt__" not in result


async def test_sukuna_respects_the_agent_budget(ledger, write_client, compose) -> None:
    """9.3: the compose call counts against the same per-turn allowance."""
    state = {"message": "draft an email to x", "agent_calls": 99, "summary": ""}
    update = await orchestrator.sukuna(state, CONFIG)

    assert update["findings"] == [orchestrator.BUDGET_EXHAUSTED]
    assert update["steps"] == ["sukuna:over-budget"]

"""Tests for the Teams side of the approval gate.

assess_gate_traffic is the lock-critical section extracted to module level:
detect a paused gate, parse the decision, mark the ledger, cancel in-lock.
The card fixture is the REAL payload recorded from the live spike on
7 Aug 2026 (schema 1.5): {'gojo_action': 'approve', 'action_id': ...}.
"""

from types import SimpleNamespace

import aiosqlite
import pytest
from langgraph.graph import END
from microsoft_agents.activity import Activity, ConversationReference

from gojo import actions
from gojo.actions import ActionProposal
from gojo.teams import (
    CANCELLED_NOTICE,
    NO_LONGER_PENDING,
    STALE_CARD,
    Outcome,
    assess_gate_traffic,
    deliver_reply,
    outcome_from_result,
)

PROPOSAL_STATE = {
    "action_id": "act-123",
    "payload": {
        "op": "draft",
        "kind": "new",
        "to": ["amy@example.org"],
        "subject": "Setup session",
        "body": "Hi Amy — Thursday works.",
        "reply_to_message_id": None,
    },
    "verified_target": None,
}


class FakeGraph:
    def __init__(self, pending: bool):
        self.updates: list[tuple] = []
        nxt = ("gate",) if pending else ()
        values = {"proposal": PROPOSAL_STATE} if pending else {}
        self.snapshot = SimpleNamespace(next=nxt, interrupts=(), values=values)

    async def aget_state(self, config):
        return self.snapshot

    async def aupdate_state(self, config, values, as_node=None):
        self.updates.append((values, as_node))


@pytest.fixture
async def ledger(tmp_path):
    conn = await aiosqlite.connect(tmp_path / "actions.sqlite")
    await actions.create_table(conn)
    await actions.record_proposed(
        conn,
        "act-123",
        "conv",
        "turn-1",
        ActionProposal.model_validate(
            {k: v for k, v in PROPOSAL_STATE["payload"].items() if v is not None}
        ),
    )
    actions.use_connection(conn)
    yield conn
    await conn.close()


# The live spike recording, verbatim shape.
CARD_APPROVE = {"gojo_action": "approve", "action_id": "act-123"}
CARD_REJECT = {"gojo_action": "reject", "action_id": "act-123"}
CARD_STALE = {"gojo_action": "approve", "action_id": "spike-1.5"}


async def test_card_tap_with_no_gate_is_a_polite_notice(ledger) -> None:
    """A stale card on a thread with nothing pending must never become an
    empty-text graph turn."""
    traffic = await assess_gate_traffic(FakeGraph(pending=False), CARD_APPROVE, "", "conv")

    assert traffic.kind == "notice"
    assert traffic.text == NO_LONGER_PENDING


async def test_card_approve_marks_ledger_and_asks_for_resume(ledger) -> None:
    traffic = await assess_gate_traffic(FakeGraph(pending=True), CARD_APPROVE, "", "conv")

    assert traffic.kind == "resume" and traffic.decision == "approve"
    assert (await actions.get(ledger, "act-123")).status == "approved"


async def test_card_reject_marks_declined(ledger) -> None:
    traffic = await assess_gate_traffic(FakeGraph(pending=True), CARD_REJECT, "", "conv")

    assert traffic.kind == "resume" and traffic.decision == "reject"
    assert (await actions.get(ledger, "act-123")).status == "declined"


async def test_stale_card_id_is_refused_with_a_notice(ledger) -> None:
    graph = FakeGraph(pending=True)
    traffic = await assess_gate_traffic(graph, CARD_STALE, "", "conv")

    assert traffic.kind == "notice" and traffic.text == STALE_CARD
    assert graph.updates == []  # the pending gate is untouched
    assert (await actions.get(ledger, "act-123")).status == "proposed"


async def test_text_yes_resumes(ledger) -> None:
    traffic = await assess_gate_traffic(FakeGraph(pending=True), None, "yes", "conv")

    assert traffic.kind == "resume" and traffic.decision == "approve"


async def test_free_text_cancels_loudly_and_falls_through(ledger) -> None:
    """B1/M1: cancellation happens entirely inside the lock section - the
    gate is abandoned via the END escape hatch, the ledger row cancelled,
    and the message then continues as a normal turn."""
    graph = FakeGraph(pending=True)
    traffic = await assess_gate_traffic(graph, None, "what time is my first call?", "conv")

    assert traffic.kind == "cancelled"
    assert traffic.text == CANCELLED_NOTICE
    assert graph.updates == [(None, END)]
    assert (await actions.get(ledger, "act-123")).status == "cancelled"


async def test_no_gate_no_value_is_normal_traffic(ledger) -> None:
    traffic = await assess_gate_traffic(FakeGraph(pending=False), None, "hello", "conv")
    assert traffic.kind == "none"


class TestOutcomeFromResult:
    def test_interrupt_result_becomes_prompt_plus_card(self) -> None:
        result = {
            "reply": "",
            "__interrupt__": [SimpleNamespace(value=PROPOSAL_STATE)],
        }
        outcome = outcome_from_result(result)

        assert "Setup session" in outcome.text  # text always present (fallback)
        assert outcome.card is not None
        assert outcome.card["actions"][0]["data"]["action_id"] == "act-123"

    def test_normal_result_is_text_only(self) -> None:
        outcome = outcome_from_result({"reply": "all done"})
        assert outcome == Outcome(text="all done", card=None)

    def test_empty_reply_still_never_empty(self) -> None:
        outcome = outcome_from_result({"reply": ""})
        assert outcome.text  # FAILED fallback


class RecordingAdapter:
    def __init__(self) -> None:
        self.received = None
        self.sent: list = []

    async def continue_conversation(self, agent_id, continuation, callback):
        self.received = continuation
        context = SimpleNamespace(send_activity=self._capture)
        await callback(context)

    async def _capture(self, activity):
        self.sent.append(activity)


def a_reference() -> ConversationReference:
    from microsoft_agents.activity import ActivityTypes, ChannelAccount, ConversationAccount

    activity = Activity(
        type=ActivityTypes.message,
        text="hello",
        channel_id="msteams",
        service_url="https://smba.trafficmanager.net/uk/",
        from_property=ChannelAccount(id="29:user", name="Ben"),
        recipient=ChannelAccount(id="28:bot", name="Gojo"),
        conversation=ConversationAccount(id="a:conv", tenant_id="tenant"),
    )
    return activity.get_conversation_reference()


async def test_proactive_delivery_can_carry_a_card() -> None:
    adapter = RecordingAdapter()
    card = {"type": "AdaptiveCard", "version": "1.5", "body": [], "actions": []}

    ok = await deliver_reply(adapter, "agent-id", a_reference(), "approve?", card=card)

    assert ok is True
    sent = adapter.sent[0]
    assert isinstance(sent, Activity)
    assert sent.text == "approve?"  # text fallback always present
    assert sent.attachments[0].content == card


async def test_proactive_delivery_without_a_card_stays_text() -> None:
    adapter = RecordingAdapter()
    await deliver_reply(adapter, "agent-id", a_reference(), "plain answer")
    assert adapter.sent == ["plain answer"]

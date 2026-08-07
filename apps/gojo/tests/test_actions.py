"""Tests for the action ledger and proposal model - the step-5 write path.

Everything here runs against a temporary sqlite file and a fake connector
client. The ledger is the idempotency mechanism AND the audit trail: these
tests are the evidence THREAT-MODEL.md's control table points at.
"""

import json

import aiosqlite
import pytest

from gojo import actions
from gojo.actions import ActionError, ActionProposal, execute, parse_proposal


@pytest.fixture
async def conn(tmp_path):
    connection = await aiosqlite.connect(tmp_path / "actions.sqlite")
    await actions.create_table(connection)
    yield connection
    await connection.close()


class FakeWriteClient:
    def __init__(self):
        self.create_draft_calls: list[dict] = []
        self.reply_draft_calls: list[dict] = []
        self.send_calls: list[str] = []

    async def create_draft(self, to, subject, body):
        self.create_draft_calls.append({"to": to, "subject": subject, "body": body})
        return f"draft-{len(self.create_draft_calls)}"

    async def create_reply_draft(self, message_id, body):
        self.reply_draft_calls.append({"message_id": message_id, "body": body})
        return f"reply-draft-{len(self.reply_draft_calls)}"

    async def send_draft(self, message_id):
        self.send_calls.append(message_id)


NEW_DRAFT = {
    "op": "draft",
    "kind": "new",
    "to": ["amy@example.org"],
    "subject": "Setup session",
    "body": "Hi Amy — Thursday works.",
}

REPLY_SEND = {
    "op": "send",
    "kind": "reply",
    "reply_to_message_id": "AAMkAGI2TAAA=",
    "body": "Thursday works — invite to follow.",
}


class TestProposalModel:
    def test_valid_new_draft_parses(self) -> None:
        proposal = ActionProposal.model_validate(NEW_DRAFT)
        assert proposal.op == "draft" and proposal.kind == "new"

    def test_reply_requires_target_id(self) -> None:
        with pytest.raises(ValueError):
            ActionProposal.model_validate({"op": "draft", "kind": "reply", "body": "x"})

    def test_new_requires_recipient_and_subject(self) -> None:
        with pytest.raises(ValueError):
            ActionProposal.model_validate({"op": "draft", "kind": "new", "body": "x"})

    def test_unknown_keys_rejected(self) -> None:
        """Strict: an agent smuggling extra fields is a parse failure, not
        a silent passenger."""
        with pytest.raises(ValueError):
            ActionProposal.model_validate({**NEW_DRAFT, "bcc": ["evil@example.net"]})

    def test_sha256_is_stable_across_key_order(self) -> None:
        a = ActionProposal.model_validate(NEW_DRAFT)
        reordered = json.loads(json.dumps(NEW_DRAFT, sort_keys=True))
        b = ActionProposal.model_validate(reordered)
        assert a.payload_sha256() == b.payload_sha256()


class TestParseProposal:
    def test_fenced_json(self) -> None:
        text = "Here you go:\n```json\n" + json.dumps(NEW_DRAFT) + "\n```"
        assert parse_proposal(text) is not None

    def test_bare_json_with_prose(self) -> None:
        text = "Proposal follows " + json.dumps(NEW_DRAFT) + " — let me know."
        assert parse_proposal(text) is not None

    def test_malformed_returns_none_never_raises(self) -> None:
        assert parse_proposal("no json here") is None
        assert parse_proposal("{broken") is None
        assert parse_proposal("") is None

    def test_valid_json_invalid_schema_returns_none(self) -> None:
        assert parse_proposal(json.dumps({"op": "detonate"})) is None


async def record(conn, action_id="act-1", payload=NEW_DRAFT, thread="conv", turn="t1"):
    proposal = ActionProposal.model_validate(payload)
    await actions.record_proposed(conn, action_id, thread, turn, proposal)
    return proposal


class TestLedger:
    async def test_proposed_roundtrip(self, conn) -> None:
        await record(conn)
        row = await actions.get(conn, "act-1")
        assert row.status == "proposed"
        assert json.loads(row.payload_json)["subject"] == "Setup session"
        assert row.payload_sha256

    async def test_mark_approved_sets_decided_at(self, conn) -> None:
        await record(conn)
        await actions.mark(conn, "act-1", "approved")
        row = await actions.get(conn, "act-1")
        assert row.status == "approved" and row.decided_at

    async def test_cancel_thread_hits_only_open_rows(self, conn) -> None:
        await record(conn, "open-1")
        await record(conn, "open-2")
        await actions.mark(conn, "open-2", "approved")
        await record(conn, "done-1")
        await actions.mark(conn, "done-1", "executed", result_id="d-1")

        cancelled = await actions.cancel_thread(conn, "conv")

        assert cancelled == 2
        assert (await actions.get(conn, "open-1")).status == "cancelled"
        assert (await actions.get(conn, "open-2")).status == "cancelled"
        assert (await actions.get(conn, "done-1")).status == "executed"

    async def test_stale_open_rows_lists_approved_but_unfinished(self, conn) -> None:
        await record(conn, "stuck")
        await actions.mark(conn, "stuck", "approved")
        await record(conn, "fine")

        stale = await actions.stale_open_rows(conn)

        assert [row.action_id for row in stale] == ["stuck"]


class TestExecute:
    async def test_draft_executes_once_and_is_idempotent(self, conn) -> None:
        await record(conn)
        await actions.mark(conn, "act-1", "approved")
        client = FakeWriteClient()

        first = await execute(conn, client, "act-1")
        second = await execute(conn, client, "act-1")

        assert first == "draft-1" and second == "draft-1"
        assert len(client.create_draft_calls) == 1  # replay never re-executes
        row = await actions.get(conn, "act-1")
        assert row.status == "executed" and row.result_id == "draft-1"

    async def test_draft_payload_is_replayed_verbatim(self, conn) -> None:
        """The byte-equality control: what was approved is what is executed."""
        await record(conn)
        await actions.mark(conn, "act-1", "approved")
        client = FakeWriteClient()

        await execute(conn, client, "act-1")

        assert client.create_draft_calls == [
            {
                "to": ["amy@example.org"],
                "subject": "Setup session",
                "body": "Hi Amy — Thursday works.",
            }
        ]

    async def test_reply_draft_targets_the_recorded_id(self, conn) -> None:
        await record(conn, payload={**REPLY_SEND, "op": "draft"})
        await actions.mark(conn, "act-1", "approved")
        client = FakeWriteClient()

        await execute(conn, client, "act-1")

        assert client.reply_draft_calls[0]["message_id"] == "AAMkAGI2TAAA="

    async def test_send_is_two_phase_and_replay_reuses_the_draft_id(self, conn) -> None:
        """The double-send defence (ADR 0011): the draft id is persisted
        BEFORE the send POST, so a crash-replay sends the SAME draft."""
        await record(conn, payload=REPLY_SEND)
        await actions.mark(conn, "act-1", "approved")
        client = FakeWriteClient()

        await execute(conn, client, "act-1")

        assert client.send_calls == ["reply-draft-1"]

        # Simulate a crash between the send POST and the executed mark:
        # roll status back to draft_created and replay.
        await actions.mark(conn, "act-1", "draft_created", result_id="reply-draft-1")
        await execute(conn, client, "act-1")

        assert len(client.reply_draft_calls) == 1  # no second draft minted
        assert client.send_calls == ["reply-draft-1", "reply-draft-1"]  # same id

    async def test_hash_mismatch_fails_safe(self, conn) -> None:
        await record(conn)
        await actions.mark(conn, "act-1", "approved")
        await conn.execute(
            "UPDATE actions SET payload_json = ? WHERE action_id = ?",
            (json.dumps({**NEW_DRAFT, "to": ["evil@example.net"]}), "act-1"),
        )
        await conn.commit()
        client = FakeWriteClient()

        with pytest.raises(ActionError, match="hash"):
            await execute(conn, client, "act-1")

        assert client.create_draft_calls == []
        assert (await actions.get(conn, "act-1")).status == "failed"

    async def test_unapproved_action_refuses_to_execute(self, conn) -> None:
        await record(conn)  # still proposed
        client = FakeWriteClient()

        with pytest.raises(ActionError, match="approved"):
            await execute(conn, client, "act-1")

        assert client.create_draft_calls == []

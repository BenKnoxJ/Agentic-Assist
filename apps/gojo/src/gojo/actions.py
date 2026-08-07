"""The action ledger - every write Gojo ever proposes, decides, or performs.

Step 5's write path in one sentence: an agent may *propose*, only the owner
may *approve*, and only this module's deterministic `execute` may *perform* -
replaying the approved bytes verbatim, exactly once.

The ledger is two things at once (ADR 0011):

- **The idempotency mechanism.** The row, not the HTTP call, is the unit of
  exactly-once. A replay after any crash consults the row: already executed
  means return the recorded result; a send whose draft already exists reuses
  the persisted draft id, so a crash between the two phases can never mint a
  second copy.
- **The audit trail.** Who-approved-what-when, with a sha256 of the payload,
  which is what THREAT-MODEL.md's control table points at.

Lives in the checkpointer's sqlite file for the same reason the outbox does:
the two are lost or kept together (ADR 0008).

Status machine:
    proposed -> approved -> (draft_created ->) executed
    proposed -> declined
    proposed/approved -> cancelled        (/new, free-text cancel, stale card)
    approved/draft_created -> failed      (execution error, never retried)
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, model_validator

from gojo.config import get_settings
from gojo_graph import GraphMailClient

logger = logging.getLogger(__name__)


class ActionError(Exception):
    """An action could not be executed safely. Never retried."""


class ActionProposal(BaseModel):
    """One proposed mail action - the only thing an agent may output.

    Strict on purpose: unknown fields are a parse failure, not a silent
    passenger. What this model accepts is the entire write vocabulary.
    """

    model_config = ConfigDict(extra="forbid")

    op: Literal["draft", "send"]
    kind: Literal["new", "reply"]
    to: list[str] = []
    subject: str = ""
    body: str
    reply_to_message_id: str | None = None

    @model_validator(mode="after")
    def _kind_requirements(self) -> "ActionProposal":
        if self.kind == "reply" and not self.reply_to_message_id:
            raise ValueError("a reply needs reply_to_message_id")
        if self.kind == "new" and (not self.to or not self.subject):
            raise ValueError("a new mail needs recipients and a subject")
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(), sort_keys=True, ensure_ascii=False)

    def payload_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def parse_proposal(text: str) -> ActionProposal | None:
    """Extract the single JSON proposal from agent output. Never raises.

    Malformed output is a fail-safe path (the turn answers "couldn't compose"),
    never an execution path.
    """
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return ActionProposal.model_validate(json.loads(text[start : end + 1]))
    except (ValueError, TypeError):
        return None


def new_action_id() -> str:
    return uuid.uuid4().hex


SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    action_id      TEXT PRIMARY KEY,
    thread_id      TEXT NOT NULL,
    turn_id        TEXT NOT NULL,
    op             TEXT NOT NULL,
    kind           TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    decided_at     TEXT,
    executed_at    TEXT,
    result_id      TEXT
)
"""

_COLUMNS = (
    "action_id, thread_id, turn_id, op, kind, payload_json, payload_sha256, "
    "status, created_at, decided_at, executed_at, result_id"
)

# Terminal states never change again; cancel_thread must not touch them.
_OPEN_STATUSES = ("proposed", "approved", "draft_created")


@dataclass(frozen=True)
class ActionRow:
    action_id: str
    thread_id: str
    turn_id: str
    op: str
    kind: str
    payload_json: str
    payload_sha256: str
    status: str
    created_at: str
    decided_at: str | None
    executed_at: str | None
    result_id: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def create_table(conn: aiosqlite.Connection) -> None:
    """Ensure the table exists. Runs on every boot."""
    await conn.execute(SCHEMA)
    await conn.commit()


async def record_proposed(
    conn: aiosqlite.Connection,
    action_id: str,
    thread_id: str,
    turn_id: str,
    proposal: ActionProposal,
) -> None:
    """Write the proposal down before the human ever sees it."""
    await conn.execute(
        f"INSERT OR REPLACE INTO actions ({_COLUMNS}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?, NULL, NULL, NULL)",
        (
            action_id,
            thread_id,
            turn_id,
            proposal.op,
            proposal.kind,
            proposal.canonical_json(),
            proposal.payload_sha256(),
            _now(),
        ),
    )
    await conn.commit()


async def get(conn: aiosqlite.Connection, action_id: str) -> ActionRow | None:
    async with conn.execute(
        f"SELECT {_COLUMNS} FROM actions WHERE action_id = ?", (action_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return ActionRow(*row) if row else None


async def mark(
    conn: aiosqlite.Connection,
    action_id: str,
    status: str,
    *,
    result_id: str | None = None,
) -> None:
    """Advance an action's status, stamping the matching timestamp."""
    stamp = "decided_at" if status in ("approved", "declined", "cancelled") else "executed_at"
    await conn.execute(
        f"UPDATE actions SET status = ?, {stamp} = ?, "
        "result_id = COALESCE(?, result_id) WHERE action_id = ?",
        (status, _now(), result_id, action_id),
    )
    await conn.commit()


async def cancel_thread(conn: aiosqlite.Connection, thread_id: str) -> int:
    """Cancel every open action on one conversation. Returns how many."""
    placeholders = ",".join("?" for _ in _OPEN_STATUSES)
    cursor = await conn.execute(
        f"UPDATE actions SET status = 'cancelled', decided_at = ? "
        f"WHERE thread_id = ? AND status IN ({placeholders})",
        (_now(), thread_id, *_OPEN_STATUSES),
    )
    await conn.commit()
    return cursor.rowcount


async def stale_open_rows(conn: aiosqlite.Connection) -> list[ActionRow]:
    """Approved-but-unfinished actions - the startup WARNING's input.

    An approved action that never reached a terminal state means a crash ate
    it after the human said yes. That must be visible, not inferred (9.1).
    """
    async with conn.execute(
        f"SELECT {_COLUMNS} FROM actions "
        "WHERE status IN ('approved', 'draft_created') ORDER BY created_at"
    ) as cursor:
        rows = await cursor.fetchall()
    return [ActionRow(*row) for row in rows]


async def execute(
    conn: aiosqlite.Connection, client, action_id: str
) -> str:
    """Perform an approved action. Deterministic; no model involved.

    The ledger row is canonical - the payload executed is the payload whose
    hash the row recorded at proposal time, re-verified here. Two-phase for
    send: the draft id is persisted BEFORE the send POST, so a crash-replay
    re-sends the SAME draft (which Graph refuses once sent) rather than
    minting a second copy.
    """
    row = await get(conn, action_id)
    if row is None:
        raise ActionError(f"action {action_id} is not in the ledger")
    if row.status == "executed":
        return row.result_id or ""
    if row.status not in ("approved", "draft_created"):
        raise ActionError(f"action {action_id} is {row.status}, not approved")

    payload = json.loads(row.payload_json)
    actual = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    if actual != row.payload_sha256:
        await mark(conn, action_id, "failed")
        raise ActionError(
            f"action {action_id} payload hash mismatch - refusing to execute"
        )

    try:
        # Phase 1: the draft. Reused verbatim if a replay already made it.
        if row.status == "draft_created" and row.result_id:
            draft_id = row.result_id
        else:
            if payload["kind"] == "reply":
                draft_id = await client.create_reply_draft(
                    payload["reply_to_message_id"], body=payload["body"]
                )
            else:
                draft_id = await client.create_draft(
                    to=payload["to"],
                    subject=payload["subject"],
                    body=payload["body"],
                )
            if payload["op"] == "send":
                # Persisted before the send POST - the whole point.
                await mark(conn, action_id, "draft_created", result_id=draft_id)

        # Phase 2: only send leaves the mailbox.
        if payload["op"] == "send":
            await client.send_draft(draft_id)
    except ActionError:
        raise
    except Exception as exc:
        await mark(conn, action_id, "failed")
        raise ActionError(f"execution failed: {exc}") from exc

    await mark(conn, action_id, "executed", result_id=draft_id)
    return draft_id


# Node-facing injectable state. The graph's nodes cannot see app.state, so
# the lifespan hands the ledger connection and settings-built client in here;
# tests inject fakes and conftest resets between tests (same hazard class as
# tools._cache).

_conn: aiosqlite.Connection | None = None
_client_cache: dict[str, GraphMailClient | None] = {}


def use_connection(conn: aiosqlite.Connection | None) -> None:
    global _conn
    _conn = conn


def connection() -> aiosqlite.Connection | None:
    return _conn


def write_client() -> GraphMailClient | None:
    """The connector used by execute. None when mail is unconfigured."""
    if "client" not in _client_cache:
        settings = get_settings()
        _client_cache["client"] = (
            GraphMailClient(
                tenant_id=settings.graph_tenant_id,
                client_id=settings.graph_client_id,
                client_secret=settings.graph_client_secret.get_secret_value(),
                owner_upn=settings.graph_owner_upn,
            )
            if settings.graph_configured
            else None
        )
    return _client_cache["client"]


def reset_clients() -> None:
    _client_cache.clear()

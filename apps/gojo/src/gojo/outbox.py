"""Owed replies - the durable record that a turn was acknowledged, not answered.

ADR 0006 answers a slow Teams turn in two parts: an acknowledgement inside the
channel's timeout, then the answer delivered proactively. Between those two
messages, everything needed to finish the turn lives in process memory. A
restart loses it and the user is left holding a promise.

This table is that promise written down. It lives in the checkpointer's own
SQLite file because the two are lost or kept together, and VPS.md then tracks
one path rather than two. LangGraph owns its schema; we own this one.

⚠ The row is matched to a turn by turn_id, never by checkpoint id. The
checkpoint advances while the owed turn is still running, so a checkpoint-based
match abandons exactly the replies it exists to deliver. ADR 0008 Evidence.

Retention is structural rather than a routine: rows are deleted on delivery,
abandoned after MAX_ATTEMPTS, abandoned once stale, and deleted by /new - so
the table only ever holds outstanding work (9.1's ceiling-at-creation).

ADR 0008.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

# Delivery is retried on a later boot. Without a ceiling, a conversation that
# can never be reached again leaves a row that outlives the project.
MAX_ATTEMPTS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS owed_replies (
    turn_id    TEXT PRIMARY KEY,
    thread_id  TEXT NOT NULL,
    reference  TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
"""

_COLUMNS = "turn_id, thread_id, reference, attempts, created_at"


@dataclass(frozen=True)
class OwedReply:
    """One acknowledged turn that has not been answered."""

    turn_id: str
    thread_id: str
    reference: str
    attempts: int
    created_at: str

    def age_seconds(self, now: datetime | None = None) -> float:
        """How long this reply has been owed."""
        now = now or datetime.now(UTC)
        return (now - datetime.fromisoformat(self.created_at)).total_seconds()


async def create_table(conn: aiosqlite.Connection) -> None:
    """Ensure the table exists. Runs on every boot."""
    await conn.execute(SCHEMA)
    await conn.commit()


async def record(
    conn: aiosqlite.Connection, turn_id: str, thread_id: str, reference: str
) -> None:
    """Note that a reply is owed.

    Called immediately before the acknowledgement, never after: the
    acknowledgement is what creates the debt, and a crash between the two
    would otherwise lose it.

    `reference` is a serialised ConversationReference - the only route back
    into the conversation once this turn's context is gone.
    """
    await conn.execute(
        f"INSERT OR REPLACE INTO owed_replies ({_COLUMNS}) VALUES (?, ?, ?, 0, ?)",
        (turn_id, thread_id, reference, datetime.now(UTC).isoformat()),
    )
    await conn.commit()


async def list_owed(conn: aiosqlite.Connection) -> list[OwedReply]:
    """Every outstanding reply, oldest first."""
    async with conn.execute(
        f"SELECT {_COLUMNS} FROM owed_replies ORDER BY created_at, turn_id"
    ) as cursor:
        rows = await cursor.fetchall()
    return [OwedReply(*row) for row in rows]


async def clear(conn: aiosqlite.Connection, turn_id: str) -> None:
    """The reply was delivered, or never will be. Forget it."""
    await conn.execute("DELETE FROM owed_replies WHERE turn_id = ?", (turn_id,))
    await conn.commit()


async def clear_thread(conn: aiosqlite.Connection, thread_id: str) -> int:
    """Forget every debt on one conversation. Returns how many were dropped.

    This is /new: the user has discarded the conversation, so an answer from
    it must not arrive afterwards. A turn id survives aupdate_state, so
    recovery's guard cannot detect this on its own.
    """
    cursor = await conn.execute(
        "DELETE FROM owed_replies WHERE thread_id = ?", (thread_id,)
    )
    await conn.commit()
    return cursor.rowcount


async def bump_attempts(conn: aiosqlite.Connection, turn_id: str) -> int:
    """Record a failed delivery. Returns the new attempt count.

    A missing row reports MAX_ATTEMPTS rather than 0, so a row deleted
    underneath us reads as exhausted rather than as a fresh start.
    """
    await conn.execute(
        "UPDATE owed_replies SET attempts = attempts + 1 WHERE turn_id = ?",
        (turn_id,),
    )
    await conn.commit()
    async with conn.execute(
        "SELECT attempts FROM owed_replies WHERE turn_id = ?", (turn_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else MAX_ATTEMPTS

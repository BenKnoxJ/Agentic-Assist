"""Tests for the owed-replies table — ADR 0008.

Uses a real file rather than :memory: because the property being bought is
survival across a process restart, which an in-memory database cannot show.
"""

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from gojo import outbox


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "cp.sqlite")


async def test_a_recorded_reply_is_listed(db) -> None:
    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", '{"ref": 1}')

        owed = await outbox.list_owed(conn)

    assert len(owed) == 1
    assert owed[0].turn_id == "turn1"
    assert owed[0].thread_id == "conv-a"
    assert owed[0].reference == '{"ref": 1}'
    assert owed[0].attempts == 0


async def test_an_owed_reply_survives_reconnecting(db) -> None:
    """Standing in for a restart: same file, new connection."""
    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", "{}")

    async with aiosqlite.connect(db) as conn:
        assert [r.turn_id for r in await outbox.list_owed(conn)] == ["turn1"]


async def test_clearing_removes_the_row(db) -> None:
    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", "{}")
        await outbox.clear(conn, "turn1")

        assert await outbox.list_owed(conn) == []


async def test_clear_thread_removes_only_that_thread(db) -> None:
    """/new must not settle debts on other conversations."""
    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", "{}")
        await outbox.record(conn, "turn2", "conv-b", "{}")

        assert await outbox.clear_thread(conn, "conv-a") == 1
        assert [r.turn_id for r in await outbox.list_owed(conn)] == ["turn2"]


async def test_bump_attempts_counts_up_and_returns_the_new_total(db) -> None:
    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", "{}")

        assert await outbox.bump_attempts(conn, "turn1") == 1
        assert await outbox.bump_attempts(conn, "turn1") == 2
        assert (await outbox.list_owed(conn))[0].attempts == 2


async def test_bumping_a_missing_row_reports_the_ceiling(db) -> None:
    """A row deleted underneath us must not read as 'attempts left'."""
    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)

        assert await outbox.bump_attempts(conn, "gone") == outbox.MAX_ATTEMPTS


async def test_create_table_is_idempotent(db) -> None:
    """It runs on every boot."""
    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", "{}")
        await outbox.create_table(conn)

        assert len(await outbox.list_owed(conn)) == 1


async def test_a_fresh_row_has_almost_no_age(db) -> None:
    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", "{}")

        assert (await outbox.list_owed(conn))[0].age_seconds() < 5


async def test_age_is_measured_from_created_at() -> None:
    """The expiry rule in recovery depends on this arithmetic."""
    long_ago = (datetime.now(UTC) - timedelta(hours=7)).isoformat()

    assert outbox.OwedReply("t", "c", "{}", 0, long_ago).age_seconds() > 6 * 3600

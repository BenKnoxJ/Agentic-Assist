# In-Flight Turn Resumption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An acknowledged Teams turn that is interrupted by a process restart is resumed from its checkpoint and delivered, instead of being silently lost.

**Architecture:** A second table in the checkpointer's existing SQLite file records that a turn was acknowledged but not answered. The row is written immediately before the acknowledgement and deleted after successful delivery. On startup a recovery pass resumes each owed thread with `graph.ainvoke(None, config)` — which continues from the last completed node rather than replaying the turn — and delivers the result through the same `deliver_reply` the live path uses.

**Tech Stack:** Python 3.12, aiosqlite 0.22.1 (already installed transitively via `langgraph-checkpoint-sqlite`), langgraph 1.2.10, Microsoft 365 Agents SDK 1.3.0, pytest.

**Decision record:** `docs/decisions/0008-in-flight-turn-resumption.md`. Read it before starting — it carries the reasoning this plan only implements.

## Global Constraints

- **Never set or reference `ANTHROPIC_API_KEY`.** `config.py` raises on it at boot. GOJO-MASTER §6.2.
- **No conversation transcript in `GojoState`.** The Agent SDK owns the transcript; state holds `session_id` only. §6.3 rule 3.
- **Every Agent SDK call goes through `agents/runner.py`.** §6.3 rule 2. This plan adds no SDK calls.
- **Do not use LangGraph's prebuilt agent** (`create_react_agent`, `ToolNode`, `bind_tools`). §6.2.
- **The §9.3 guards live at one surface.** Any new graph invocation applies both `graph_timeout_seconds` and `recursion_limit`, via a function in `orchestrator.py` — never inline at a call site.
- **Version floors are mandatory (security):** `langgraph>=1.0.10`, `langgraph-checkpoint-sqlite>=3.0.1`, `langchain-core>=1.2.22`. Do not add dependencies to `pyproject.toml` in this plan — `aiosqlite` is already resolved in `uv.lock`.
- **Tests run with `uv run pytest`** from the repo root. The suite is 46 tests and must stay green.
- **`asyncio_mode` is auto** for most tests; `test_teams_delivery.py` marks explicitly with `@pytest.mark.asyncio`. Follow the file you are editing.

### Verified facts this plan depends on

Measured against the installed versions on 5 August 2026. Do not re-derive; do not assume the opposite:

- `graph.ainvoke(None, config)` on a thread that crashed mid-graph resumes from the failed node. `aget_state(config).next` was `('megumi',)` after the crash, and the resumed call completed the turn.
- `graph.ainvoke(None, config)` on a thread that **already completed** returns its final state and re-runs nothing. `next` is `()` and the agent-call count did not change. Recovery therefore needs no branch — one call handles both cases.
- `ConversationReference.model_dump_json()` → `ConversationReference.model_validate_json()` round-trips, and the rehydrated reference still yields a valid continuation activity with `recipient` populated. ~530 bytes.

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/gojo/src/gojo/outbox.py` (new) | The owed-replies table and nothing else. Storage only — no graph, no adapter, no delivery. |
| `apps/gojo/src/gojo/recovery.py` (new) | The startup pass: resume each owed thread, deliver, settle the row. |
| `apps/gojo/src/gojo/orchestrator.py` (modify) | Gains `resume_turn`, sibling to `run_turn`. |
| `apps/gojo/src/gojo/teams.py` (modify) | Records the debt before the ACK; clears it after delivery. |
| `apps/gojo/src/gojo/api.py` (modify) | Opens the outbox connection, creates the table, launches recovery. |
| `apps/gojo/tests/test_outbox.py` (new) | Storage behaviour, including survival across a reconnect. |
| `apps/gojo/tests/test_recovery.py` (new) | The property being bought: a crashed turn is resumed and delivered. |

`outbox.py` and `recovery.py` are separate because one is a table and the other is a policy. The table is testable with no graph and no adapter; keeping them together would force every storage test to build both.

---

### Task 1: The owed-replies table

**Files:**
- Create: `apps/gojo/src/gojo/outbox.py`
- Test: `apps/gojo/tests/test_outbox.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `MAX_ATTEMPTS: int` (= 3)
  - `OwedReply` frozen dataclass with fields `turn_id: str`, `thread_id: str`, `reference: str`, `attempts: int`, `created_at: str`
  - `async create_table(conn: aiosqlite.Connection) -> None`
  - `async record(conn, turn_id: str, thread_id: str, reference: str) -> None`
  - `async list_owed(conn) -> list[OwedReply]`
  - `async clear(conn, turn_id: str) -> None`
  - `async bump_attempts(conn, turn_id: str) -> int`

- [ ] **Step 1: Write the failing tests**

Create `apps/gojo/tests/test_outbox.py`:

```python
"""Tests for the owed-replies table — ADR 0008.

Uses a real file rather than :memory: because the property being bought is
survival across a process restart, which an in-memory database cannot show.
"""

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
        await outbox.record(conn, "turn1", "conv-a", '{"ref": 1}')

    async with aiosqlite.connect(db) as conn:
        owed = await outbox.list_owed(conn)

    assert [row.turn_id for row in owed] == ["turn1"]


async def test_clearing_removes_the_row(db) -> None:
    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", "{}")
        await outbox.clear(conn, "turn1")

        assert await outbox.list_owed(conn) == []


async def test_bump_attempts_counts_up_and_returns_the_new_total(db) -> None:
    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", "{}")

        assert await outbox.bump_attempts(conn, "turn1") == 1
        assert await outbox.bump_attempts(conn, "turn1") == 2
        assert (await outbox.list_owed(conn))[0].attempts == 2


async def test_bumping_a_missing_row_reports_the_ceiling(db) -> None:
    """A row deleted underneath us must not read as 'plenty of attempts left'."""
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


async def test_owed_replies_come_back_oldest_first(db) -> None:
    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "old", "conv-a", "{}")
        await outbox.record(conn, "new", "conv-b", "{}")

        assert [row.turn_id for row in await outbox.list_owed(conn)] == ["old", "new"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest apps/gojo/tests/test_outbox.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gojo.outbox'`

- [ ] **Step 3: Write the implementation**

Create `apps/gojo/src/gojo/outbox.py`:

```python
"""Owed replies - the durable record that a turn was acknowledged, not answered.

ADR 0006 answers a slow Teams turn in two parts: an acknowledgement inside the
channel's timeout, then the answer delivered proactively. Between those two
messages, everything needed to finish the turn lives in process memory. A
restart loses it and the user is left holding a promise.

This table is that promise written down. It lives in the checkpointer's own
SQLite file because the two are lost or kept together, and VPS.md then tracks
one path rather than two. LangGraph owns its schema; we own this one.

Retention is structural rather than a routine. A row is deleted on delivery
and abandoned after MAX_ATTEMPTS, so the table only ever holds outstanding
work - 9.1's ceiling-at-creation, with nothing to forget to run later.

ADR 0008.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

# Delivery is retried on the next start. Without a ceiling, a conversation
# that can never be reached again leaves a row that outlives the project.
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest apps/gojo/tests/test_outbox.py -v`
Expected: 7 passed

- [ ] **Step 5: Lint**

Run: `uv run ruff check apps/gojo/src/gojo/outbox.py apps/gojo/tests/test_outbox.py`
Expected: no findings

- [ ] **Step 6: Commit**

```bash
git add apps/gojo/src/gojo/outbox.py apps/gojo/tests/test_outbox.py
git commit -m "feat: owed-replies table — the record that a turn was acknowledged"
```

---

### Task 2: `resume_turn` in the orchestrator

**Files:**
- Modify: `apps/gojo/src/gojo/orchestrator.py` (add after `run_turn`, which ends at line 133)
- Test: `apps/gojo/tests/test_recovery.py` (created here, extended in Task 4)

**Interfaces:**
- Consumes: `get_settings()`, `GraphTimeout` — both already in `orchestrator.py`.
- Produces: `async resume_turn(graph, thread_id: str) -> dict`. Returns the graph's final state dict, same shape as `run_turn`. Raises `GraphTimeout`.

- [ ] **Step 1: Write the failing tests**

Create `apps/gojo/tests/test_recovery.py`:

```python
"""Tests for resuming an interrupted turn — ADR 0008.

The property: a turn that died mid-graph is finished from its checkpoint
rather than replayed from the start, so the agent call is not paid for twice.
"""

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
        return AgentResult(text="the answer", session_id="s1")


@pytest.fixture
def crashing(monkeypatch: pytest.MonkeyPatch) -> CrashOnceGather:
    recorder = CrashOnceGather()
    monkeypatch.setattr(orchestrator, "gather", recorder)
    return recorder


async def test_resume_finishes_a_turn_that_died_mid_graph(tmp_path, crashing) -> None:
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        config = {"configurable": {"thread_id": "conv-a"}}

        with pytest.raises(RuntimeError):
            await graph.ainvoke(
                {"message": "q", "steps": [], "findings": []}, config
            )

        result = await orchestrator.resume_turn(graph, "conv-a")

    assert result["reply"] == "the answer"


async def test_resume_does_not_replay_a_completed_turn(tmp_path, crashing) -> None:
    """A crash between finishing and delivering must not re-pay for the agent."""
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "cp.sqlite")) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        config = {"configurable": {"thread_id": "conv-a"}}

        with pytest.raises(RuntimeError):
            await graph.ainvoke(
                {"message": "q", "steps": [], "findings": []}, config
            )
        await orchestrator.resume_turn(graph, "conv-a")
        calls_after_first_resume = crashing.calls

        second = await orchestrator.resume_turn(graph, "conv-a")

    assert second["reply"] == "the answer"
    assert crashing.calls == calls_after_first_resume


async def test_resume_applies_the_wall_clock_guard(tmp_path, monkeypatch) -> None:
    """9.3's timeout must cover the recovery path too, not just live turns."""
    import asyncio

    async def hangs(message: str, resume: str | None = None, summary: str = ""):
        await asyncio.sleep(60)

    monkeypatch.setattr(orchestrator, "gather", hangs)
    settings = orchestrator.get_settings()
    monkeypatch.setattr(settings, "graph_timeout_seconds", 0.2)

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest apps/gojo/tests/test_recovery.py -v`
Expected: FAIL — `AttributeError: module 'gojo.orchestrator' has no attribute 'resume_turn'`

- [ ] **Step 3: Write the implementation**

In `apps/gojo/src/gojo/orchestrator.py`, add directly below `run_turn`:

```python
async def resume_turn(graph, thread_id: str) -> dict:
    """Finish a turn that was interrupted, from its last checkpoint.

    The sibling of run_turn: same guards, no initial state. Passing None tells
    LangGraph to continue the thread rather than start a turn, so a crash
    after megumi returned re-runs only respond and the agent call is not paid
    for twice.

    Verified against langgraph 1.2.10: a thread that had already completed
    returns its final state and re-runs nothing, so the caller needs no branch
    for "crashed before finishing" versus "crashed before delivering".

    This is a separate function rather than a flag on run_turn for the reason
    run_turn's own docstring gives - the 9.3 guards live at one surface, and a
    flag is how a call site ends up with one of the two.

    Raises:
        GraphTimeout: the resumed turn exceeded graph_timeout_seconds.
    """
    settings = get_settings()
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings.recursion_limit,
    }

    try:
        return await asyncio.wait_for(
            graph.ainvoke(None, config), timeout=settings.graph_timeout_seconds
        )
    except TimeoutError as exc:
        logger.error(
            "resumed turn timed out after %ss on thread %s",
            settings.graph_timeout_seconds,
            thread_id,
        )
        raise GraphTimeout(
            f"resumed turn exceeded {settings.graph_timeout_seconds}s"
        ) from exc
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest apps/gojo/tests/test_recovery.py -v`
Expected: 3 passed

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: 56 passed (46 existing + 7 outbox + 3 resume)

- [ ] **Step 6: Commit**

```bash
git add apps/gojo/src/gojo/orchestrator.py apps/gojo/tests/test_recovery.py
git commit -m "feat: resume_turn — finish an interrupted turn from its checkpoint"
```

---

### Task 3: Record the debt at the acknowledgement

**Files:**
- Modify: `apps/gojo/src/gojo/teams.py` — `build_agent_app` signature, `_deliver_when_done` (line 177), `on_message` (lines 185, 242-243)
- Test: `apps/gojo/tests/test_teams_delivery.py` (extend)

**Interfaces:**
- Consumes: `outbox.record`, `outbox.clear` from Task 1.
- Produces: `build_agent_app(...)` gains a final parameter `outbox_conn: aiosqlite.Connection | None = None`. When `None`, no debt is recorded — which is what keeps the existing tests and an unconfigured deployment working.

- [ ] **Step 1: Write the failing tests**

Append to `apps/gojo/tests/test_teams_delivery.py`:

```python
@pytest.mark.asyncio
async def test_a_delivered_reply_settles_its_debt(tmp_path) -> None:
    """The row exists while the answer is outstanding, and not after."""
    import aiosqlite

    from gojo import outbox
    from gojo.teams import settle_debt

    async with aiosqlite.connect(str(tmp_path / "cp.sqlite")) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", "{}")

        await settle_debt(conn, "turn1", delivered=True)

        assert await outbox.list_owed(conn) == []


@pytest.mark.asyncio
async def test_an_undelivered_reply_stays_owed(tmp_path) -> None:
    """A failed send must leave the debt for the next startup to retry."""
    import aiosqlite

    from gojo import outbox
    from gojo.teams import settle_debt

    async with aiosqlite.connect(str(tmp_path / "cp.sqlite")) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", "{}")

        await settle_debt(conn, "turn1", delivered=False)

        assert [row.turn_id for row in await outbox.list_owed(conn)] == ["turn1"]


@pytest.mark.asyncio
async def test_settling_without_an_outbox_is_a_no_op() -> None:
    """/chat and the tests build the app without one."""
    from gojo.teams import settle_debt

    await settle_debt(None, "turn1", delivered=True)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest apps/gojo/tests/test_teams_delivery.py -v`
Expected: FAIL — `ImportError: cannot import name 'settle_debt' from 'gojo.teams'`

- [ ] **Step 3: Write the implementation**

In `apps/gojo/src/gojo/teams.py`, add the import at the top with the other `gojo` imports:

```python
from gojo import outbox
```

Add this function at module level, directly after `deliver_reply`:

```python
async def settle_debt(outbox_conn, turn_id: str, *, delivered: bool) -> None:
    """Close out an owed reply, or leave it for the next startup to retry.

    A failed delivery deliberately leaves the row: the answer is still owed,
    and recovery is what tries again. ADR 0008.
    """
    if outbox_conn is None or not delivered:
        return
    await outbox.clear(outbox_conn, turn_id)
```

Change the `build_agent_app` signature — add a final parameter:

```python
    fast_reply_seconds: float,
    outbox_conn=None,
) -> AgentApplication:
```

and document it in the docstring's `Args:` block:

```
        outbox_conn: an aiosqlite connection holding the owed-replies table,
            or None to record nothing. None is correct for /chat and for
            tests, where nothing is acknowledged and so nothing is owed.
```

Replace `_deliver_when_done` (currently lines 177-179) with:

```python
    async def _deliver_when_done(
        task: asyncio.Task, reference: ConversationReference, turn_id: str
    ) -> None:
        """Wait for a slow turn, send its answer, and settle the debt."""
        delivered = await deliver_reply(adapter, agent_id, reference, await task)
        await settle_debt(outbox_conn, turn_id, delivered=delivered)
```

In `on_message`, capture the turn id — change line 185 from `new_turn_id()` to:

```python
        turn = new_turn_id()
```

Then replace the slow-path block (currently lines 242-243) with:

```python
        # The acknowledgement is what creates the debt (ADR 0008), so the row
        # goes down *before* it. A crash between the two would otherwise
        # promise an answer with nothing recording that it is owed.
        if outbox_conn is not None:
            await outbox.record(
                outbox_conn, turn, thread_id, reference.model_dump_json()
            )
        await context.send_activity(ACK)
        _track(asyncio.create_task(_deliver_when_done(task, reference, turn)))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest apps/gojo/tests/test_teams_delivery.py -v`
Expected: 6 passed

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: 59 passed

- [ ] **Step 6: Lint**

Run: `uv run ruff check apps/gojo/src/gojo/teams.py apps/gojo/tests/test_teams_delivery.py`
Expected: no findings

- [ ] **Step 7: Commit**

```bash
git add apps/gojo/src/gojo/teams.py apps/gojo/tests/test_teams_delivery.py
git commit -m "feat: record an owed reply before the acknowledgement"
```

---

### Task 4: The startup recovery pass

**Files:**
- Create: `apps/gojo/src/gojo/recovery.py`
- Test: `apps/gojo/tests/test_recovery.py` (extend from Task 2)

**Interfaces:**
- Consumes: `outbox.list_owed/clear/bump_attempts/MAX_ATTEMPTS` (Task 1), `orchestrator.resume_turn` (Task 2), `teams.deliver_reply` (existing), `logs.turn_id` (existing ContextVar).
- Produces: `async recover_owed_replies(conn, graph, adapter, agent_id: str) -> int`, returning the number of replies successfully delivered.

- [ ] **Step 1: Write the failing tests**

Append to `apps/gojo/tests/test_recovery.py`:

```python
import aiosqlite
from microsoft_agents.activity import (
    Activity,
    ActivityTypes,
    ChannelAccount,
    ConversationAccount,
)

from gojo import outbox
from gojo.recovery import FAILED, recover_owed_replies

AGENT_ID = "2b6bad70-0000-0000-0000-000000000000"


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

    async def continue_conversation(self, agent_id, continuation, callback):
        class Ctx:
            def __init__(self, sink):
                self._sink = sink

            async def send_activity(self, text):
                self._sink.append(text)

        await callback(Ctx(self.sent))


class BrokenAdapter:
    async def continue_conversation(self, agent_id, continuation, callback):
        raise RuntimeError("service url unreachable")


async def test_a_crashed_turn_is_resumed_and_delivered(tmp_path, crashing) -> None:
    """The whole point of ADR 0008, end to end."""
    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        with pytest.raises(RuntimeError):
            await graph.ainvoke(
                {"message": "q", "steps": [], "findings": []},
                {"configurable": {"thread_id": "conv-a"}},
            )

        async with aiosqlite.connect(db) as conn:
            await outbox.create_table(conn)
            await outbox.record(
                conn, "turn1", "conv-a", a_reference().model_dump_json()
            )

            delivered = await recover_owed_replies(conn, graph, adapter, AGENT_ID)

            assert delivered == 1
            assert adapter.sent == ["the answer"]
            assert await outbox.list_owed(conn) == []


async def test_nothing_owed_delivers_nothing(tmp_path) -> None:
    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)

        assert await recover_owed_replies(conn, None, adapter, AGENT_ID) == 0
        assert adapter.sent == []


async def test_failed_delivery_bumps_attempts_and_keeps_the_row(
    tmp_path, crashing
) -> None:
    db = str(tmp_path / "cp.sqlite")

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        with pytest.raises(RuntimeError):
            await graph.ainvoke(
                {"message": "q", "steps": [], "findings": []},
                {"configurable": {"thread_id": "conv-a"}},
            )

        async with aiosqlite.connect(db) as conn:
            await outbox.create_table(conn)
            await outbox.record(
                conn, "turn1", "conv-a", a_reference().model_dump_json()
            )

            assert await recover_owed_replies(conn, graph, BrokenAdapter(), AGENT_ID) == 0

            owed = await outbox.list_owed(conn)

    assert len(owed) == 1
    assert owed[0].attempts == 1


async def test_an_exhausted_reply_is_abandoned_not_retried(tmp_path) -> None:
    """Three failures and the row goes, or it outlives the project."""
    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", a_reference().model_dump_json())
        for _ in range(outbox.MAX_ATTEMPTS):
            await outbox.bump_attempts(conn, "turn1")

        assert await recover_owed_replies(conn, None, adapter, AGENT_ID) == 0
        assert await outbox.list_owed(conn) == []
        assert adapter.sent == []


async def test_a_turn_that_cannot_resume_still_gets_an_answer(tmp_path) -> None:
    """The promise was a reply, not a correct one (ADR 0008)."""
    db = str(tmp_path / "cp.sqlite")
    adapter = RecordingAdapter()

    class ExplodingGraph:
        async def ainvoke(self, _input, _config):
            raise RuntimeError("checkpoint unreadable")

    async with aiosqlite.connect(db) as conn:
        await outbox.create_table(conn)
        await outbox.record(conn, "turn1", "conv-a", a_reference().model_dump_json())

        delivered = await recover_owed_replies(
            conn, ExplodingGraph(), adapter, AGENT_ID
        )

        assert delivered == 1
        assert adapter.sent == [FAILED]
        assert await outbox.list_owed(conn) == []


async def test_recovery_restores_the_original_turn_id(tmp_path, crashing) -> None:
    """`grep turn=<id>` must span the crash and the recovery."""
    from gojo.logs import turn_id as turn_id_var

    db = str(tmp_path / "cp.sqlite")
    seen = []

    class Watcher(RecordingAdapter):
        async def continue_conversation(self, agent_id, continuation, callback):
            seen.append(turn_id_var.get())
            await super().continue_conversation(agent_id, continuation, callback)

    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = orchestrator.build_graph(checkpointer=cp)
        with pytest.raises(RuntimeError):
            await graph.ainvoke(
                {"message": "q", "steps": [], "findings": []},
                {"configurable": {"thread_id": "conv-a"}},
            )

        async with aiosqlite.connect(db) as conn:
            await outbox.create_table(conn)
            await outbox.record(
                conn, "abc12345", "conv-a", a_reference().model_dump_json()
            )
            await recover_owed_replies(conn, graph, Watcher(), AGENT_ID)

    assert seen == ["abc12345"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest apps/gojo/tests/test_recovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gojo.recovery'`

- [ ] **Step 3: Write the implementation**

Create `apps/gojo/src/gojo/recovery.py`:

```python
"""Startup recovery - deliver the replies a restart interrupted.

ADR 0006 acknowledges a slow turn and answers it later. If the process dies
in between, the user holds an acknowledgement and gets nothing. The
checkpointer knows how far the graph got; the outbox knows a human is owed an
answer and where to send it. This is the pass that puts the two together.

It runs once per boot, after the app starts serving rather than before. The
alternative - blocking the boot - is strictly safer but can hold the service
down for graph_timeout_seconds per owed turn against Restart=always. The race
it accepts is that a user who sends a new message on that thread first will
advance the checkpoint past the answer being rescued, which is the behaviour
they get today anyway.

ADR 0008.
"""

import logging

from microsoft_agents.activity import ConversationReference

from gojo import outbox
from gojo.logs import turn_id as turn_id_var
from gojo.orchestrator import resume_turn
from gojo.teams import deliver_reply

logger = logging.getLogger(__name__)

# Deliberately the same words _run_graph uses for a live failure. A recovered
# turn that cannot be finished should read like any other failed turn, not
# like a new category of thing for the user to interpret.
FAILED = "Something went wrong on my side. Nothing was changed."


async def recover_owed_replies(conn, graph, adapter, agent_id: str) -> int:
    """Resume and deliver every owed reply. Returns how many were delivered.

    Never raises: this runs unattended at boot, and a failure here must not
    take down a process that is otherwise healthy (10.4).
    """
    owed = await outbox.list_owed(conn)
    if not owed:
        return 0

    logger.info("recovering %d owed repl(ies)", len(owed))
    delivered = 0

    for row in owed:
        # Same id the crashed turn logged under, so one grep spans both halves
        # instead of splitting into two unrelated traces.
        turn_id_var.set(row.turn_id)

        if row.attempts >= outbox.MAX_ATTEMPTS:
            logger.warning(
                "abandoning owed reply on thread %s after %d attempts",
                row.thread_id,
                row.attempts,
            )
            await outbox.clear(conn, row.turn_id)
            continue

        try:
            result = await resume_turn(graph, row.thread_id)
            text = result.get("reply") or FAILED
        except Exception:
            # A turn that cannot be completed is still answered. The promise
            # was a reply, not a correct one - and a turn that can never
            # resume would otherwise stay owed forever.
            logger.exception("could not resume thread %s", row.thread_id)
            text = FAILED

        reference = ConversationReference.model_validate_json(row.reference)
        if await deliver_reply(adapter, agent_id, reference, text):
            await outbox.clear(conn, row.turn_id)
            delivered += 1
        else:
            attempts = await outbox.bump_attempts(conn, row.turn_id)
            logger.warning(
                "delivery failed on thread %s, attempt %d of %d",
                row.thread_id,
                attempts,
                outbox.MAX_ATTEMPTS,
            )

    logger.info("recovery delivered %d of %d owed repl(ies)", delivered, len(owed))
    return delivered
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest apps/gojo/tests/test_recovery.py -v`
Expected: 9 passed

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check apps/gojo/`
Expected: 65 passed, no lint findings

- [ ] **Step 6: Commit**

```bash
git add apps/gojo/src/gojo/recovery.py apps/gojo/tests/test_recovery.py
git commit -m "feat: startup recovery for owed replies"
```

---

### Task 5: Wire recovery into the lifespan

**Files:**
- Modify: `apps/gojo/src/gojo/api.py` — imports, `lifespan` (lines 69-127), `health` (line 133)
- Test: `apps/gojo/tests/test_api.py` (extend)

**Interfaces:**
- Consumes: `outbox.create_table` (Task 1), `recovery.recover_owed_replies` (Task 4), `build_agent_app`'s new parameter (Task 3).
- Produces: `app.state.outbox` (an `aiosqlite.Connection`) and `app.state.recovery_task` (an `asyncio.Task` or `None`).

- [ ] **Step 1: Write the failing test**

Append to `apps/gojo/tests/test_api.py`:

```python
def test_health_reports_owed_replies(client) -> None:
    """A restart that lost work should be visible without opening the file."""
    body = client.get("/health").json()

    assert "owed_replies" in body
    assert body["owed_replies"] == 0
```

If `test_api.py` has no `client` fixture under that name, use whatever fixture the existing tests in that file use to reach `/health` — match the file, do not introduce a second pattern.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest apps/gojo/tests/test_api.py -v -k owed`
Expected: FAIL — `KeyError: 'owed_replies'` or `assert 'owed_replies' in {...}`

- [ ] **Step 3: Write the implementation**

In `apps/gojo/src/gojo/api.py`, add to the imports:

```python
import asyncio

import aiosqlite

from gojo import outbox
from gojo.recovery import recover_owed_replies
```

In `lifespan`, directly after the `logger.info("checkpointer at %s", checkpoint_file)` line (line 76), add:

```python
        # A second connection to the same file. LangGraph owns its schema and
        # we own ours; at one user's write volume, contention is not a
        # concern (ADR 0008).
        outbox_conn = await stack.enter_async_context(
            aiosqlite.connect(str(checkpoint_file))
        )
        await outbox.create_table(outbox_conn)
        app.state.outbox = outbox_conn
        app.state.recovery_task = None
```

Pass the connection into `build_agent_app` — the call at line 105 gains a final argument:

```python
            app.state.agent_app = build_agent_app(
                adapter,
                app.state.graph,
                settings.teams_client_id,
                connections,
                settings.allowed_users,
                settings.teams_tenant_id,
                settings.fast_reply_seconds,
                outbox_conn,
            )
```

Then, immediately before `yield` (line 127), add:

```python
        if app.state.agent_app is not None:
            # Started here rather than awaited: recovery must not hold the
            # boot open for graph_timeout_seconds per owed turn while systemd
            # is watching (ADR 0008). Held on app.state for the same reason
            # teams.py holds its tasks - asyncio keeps only weak references.
            app.state.recovery_task = asyncio.create_task(
                recover_owed_replies(
                    outbox_conn,
                    app.state.graph,
                    adapter,
                    settings.teams_client_id,
                )
            )

        yield
```

Update `health` to report the backlog:

```python
@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness probe. Used by systemd and by you, from a phone, at 07:00.

    Reports the Teams surface separately: a process that is up but not
    listening to Teams looks identical from the outside otherwise.

    owed_replies is the ADR 0008 backlog - answers promised and not yet
    delivered. Steady state is 0; a number that stays above 0 across restarts
    means delivery is failing, not that turns are slow.
    """
    owed = await outbox.list_owed(app.state.outbox) if getattr(app.state, "outbox", None) else []
    return {
        "status": "ok",
        "teams": "enabled" if app.state.agent_app else "disabled",
        "turns_in_flight": in_flight_count(),
        "owed_replies": len(owed),
    }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest apps/gojo/tests/test_api.py -v`
Expected: all pass, including the new one

- [ ] **Step 5: Run the whole suite and lint**

Run: `uv run pytest -q && uv run ruff check apps/gojo/`
Expected: 66 passed, no lint findings

- [ ] **Step 6: Commit**

```bash
git add apps/gojo/src/gojo/api.py apps/gojo/tests/test_api.py
git commit -m "feat: run owed-reply recovery at startup, report the backlog on /health"
```

---

### Task 6: Prove it on the box, then record it

**Files:**
- Modify: `docs/build-log.md` (append a session entry)
- Modify: `docs/GOJO-MASTER.md` §11.0 — the `In-flight turn resumption` row and the "Open — carry into step 4" list
- Modify: `docs/decisions/0006-async-teams-replies.md` — mark the consequence closed
- Modify: `docs/VPS.md` — note the second table in the state inventory

- [ ] **Step 1: Deploy and restart**

```bash
sudo systemctl restart gojo
journalctl -u gojo -n 30 --no-pager
```

Expected: the service starts, and with no owed replies the recovery log line does not appear (`recover_owed_replies` returns early before logging).

- [ ] **Step 2: Prove the gap is actually closed, from Teams**

This is the acceptance test and it cannot be done from the test suite. Ask Gojo something slow enough to cross `fast_reply_seconds` (8s) — a long question on an established conversation, since a resumed session measures 5.9–7.1s. The moment the acknowledgement arrives:

```bash
sudo systemctl restart gojo
```

Expected: the answer still arrives in Teams after the service comes back. Before this change it never did.

Capture the evidence:

```bash
journalctl -u gojo -n 60 --no-pager | grep -E "recovering|recovery delivered|turn="
```

- [ ] **Step 3: Record it in the build log**

Append to `docs/build-log.md`, following the existing session format — what was done, what broke, chronological. Include the measured restart-to-delivery time from Step 2 and anything that surprised you. Do not record decisions here; ADR 0008 owns those (§18).

- [ ] **Step 4: Flip the master doc**

In `docs/GOJO-MASTER.md` §11.0, change:

```
| In-flight turn resumption | ❌ An acknowledged turn is still lost on restart — ADR 0008 |
```

to a ✅ row naming what was verified, and delete item 1 from the "Open — carry into step 4" list, leaving the backup item as item 1.

- [ ] **Step 5: Close the loop on ADR 0006**

In `docs/decisions/0006-async-teams-replies.md`, the consequence beginning "A turn in flight does not survive a restart" gains a closing line:

```markdown
  **Closed 2026-08-05 by ADR 0008** — the outbox records the debt at the
  acknowledgement and startup recovery resumes it from the checkpoint.
```

Leave the original text intact. §16 counts recorded reversals as evidence of judgement; rewriting history removes the evidence.

- [ ] **Step 6: Note the new table in VPS.md**

In the State table, `checkpoints/gojo.sqlite` now holds two things. Update its Contents cell to say so — conversation state plus owed replies — so the operations doc matches the file.

- [ ] **Step 7: Commit**

```bash
git add docs/
git commit -m "docs: in-flight resumption verified on the box; close ADR 0006's gap"
```

---

## Self-Review

**Spec coverage.** Every decision in ADR 0008 maps to a task: the outbox table and its schema (Task 1); resumption from the checkpoint with the §9.3 guards (Task 2); the ACK creating the debt, written before the acknowledgement (Task 3); silent delivery, the original turn id, failure-still-answered, and the attempts ceiling (Task 4); background recovery after the app serves, and visibility (Task 5); the ADR 0006 closure and doc updates (Task 6). At-least-once delivery is a documented consequence with no code, correctly.

**Placeholders.** None. Every code step carries the code; the one instruction that defers to the codebase (Task 5 Step 1's fixture name) does so because matching the existing file's pattern beats inventing a second one.

**Type consistency.** `settle_debt` is keyword-only on `delivered` in both its definition and both call sites. `recover_owed_replies(conn, graph, adapter, agent_id)` has the same argument order in `recovery.py`, all six tests, and `api.py`. `outbox.record(conn, turn_id, thread_id, reference)` is consistent across Tasks 1, 3 and 4. `OwedReply`'s field order matches `_COLUMNS`, which is what `OwedReply(*row)` depends on.

**Known soft spot.** Task 5's test-count expectations assume `test_api.py` gains exactly one test. If that file's fixtures differ from the assumption, the count moves — treat `uv run pytest -q` passing as the gate, not the arithmetic.

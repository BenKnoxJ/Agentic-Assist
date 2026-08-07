"""Slash commands — the session controls Claude Code has.

Handled before the graph runs. A command is not a question: it manipulates
the conversation rather than asking anything, so it never reaches an agent
and never spends a turn.

Kept out of teams.py so it can be tested against a graph and a checkpointer
with no adapter, no Teams, and no subprocess.
"""

import logging

from langgraph.graph import END

from gojo import actions, outbox
from gojo.agents.megumi import SUMMARISE, gather
from gojo.orchestrator import gate_pending, lock_for

logger = logging.getLogger(__name__)

HELP = """**Commands**
- `/new` — start a fresh conversation. I forget everything before this.
- `/compact` — summarise what we've covered and carry on from the summary.
- `/help` — this list.

Anything else is treated as a question."""

NEW_DONE = "Fresh start — I've forgotten everything before this."
COMPACT_EMPTY = "Nothing to compact yet — this conversation hasn't started."
COMPACT_FAILED = "I couldn't summarise the conversation, so I've left it as it was."
COMPACT_BLOCKED = (
    "There's an action waiting for your decision — approve it, reject it, or "
    "say anything else to discard it. Then /compact."
)


def is_command(text: str) -> bool:
    """Whether a message is a command rather than a question."""
    return text.strip().startswith("/")


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


async def handle(graph, text: str, thread_id: str, outbox_conn=None) -> str:
    """Run a command and return what to say back.

    Unknown commands get the help text rather than being passed to an agent:
    a typo'd command should not silently become a prompt.

    outbox_conn is optional because /chat and the tests have no outbox. Only
    /new uses it.

    The whole command runs under the conversation's lock (ADR 0009): a /new
    that races an in-flight turn would otherwise be silently reverted by the
    turn's final state writes - measured on the live system, not
    hypothetical. Under the lock the ordering is defined: answer, then
    forget, and forget sticks.
    """
    command = text.strip().split()[0].lower()

    async with lock_for(thread_id):
        if command == "/new":
            return await _new(graph, thread_id, outbox_conn)
        if command == "/compact":
            return await _compact(graph, thread_id)
    return HELP


async def _new(graph, thread_id: str, outbox_conn=None) -> str:
    """Drop the session, any carried summary, and any answer still owed.

    The checkpoint history is left alone - clearing the pointers is what
    makes the next turn start fresh, and keeping the history means a
    mistaken /new is still recoverable from the checkpointer.

    Owed replies are not left alone. A turn id survives aupdate_state, so
    recovery's guard cannot tell that this conversation was discarded;
    without this, /new is followed by an answer to the question it discarded
    (ADR 0008).

    A paused gate is not left alone either - and its check runs FIRST,
    because the values-update below empties snapshot.interrupts while the
    gate stays resumable (verified 1.2.10, ADR 0011): checked after, the
    gate would look gone while its task still holds the thread.
    """
    snapshot = await graph.aget_state(_config(thread_id))
    if gate_pending(snapshot):
        await graph.aupdate_state(_config(thread_id), None, as_node=END)
        conn = actions.connection()
        if conn is not None:
            cancelled = await actions.cancel_thread(conn, thread_id)
            logger.info(
                "/new cancelled %d pending action(s) on thread %s", cancelled, thread_id
            )
    await graph.aupdate_state(_config(thread_id), {"session_id": None, "summary": ""})
    if outbox_conn is not None:
        dropped = await outbox.clear_thread(outbox_conn, thread_id)
        if dropped:
            logger.info(
                "/new dropped %d owed repl(ies) on thread %s", dropped, thread_id
            )
    logger.info("/new on thread %s", thread_id)
    return NEW_DONE


async def _compact(graph, thread_id: str) -> str:
    """Summarise the session, then start a new one seeded with the summary.

    This is the fix for resume latency, not just a tidiness feature: the SDK
    replays the transcript on every turn, so cost grows with conversation
    length. Compacting resets that to near zero while keeping the gist.
    """
    snapshot = await graph.aget_state(_config(thread_id))

    # A paused gate blocks compaction: the aupdate_state below would blind
    # the gate guards (snapshot.interrupts empties, ADR 0011/M1), so the
    # pending action must be decided or discarded first.
    if gate_pending(snapshot):
        return COMPACT_BLOCKED

    session_id = (snapshot.values or {}).get("session_id")

    if not session_id:
        return COMPACT_EMPTY

    # Tool-free on purpose: a summary must not spend tool calls or hand the
    # summariser a way to fetch more untrusted content mid-summary (step 4).
    result = await gather(SUMMARISE, resume=session_id, use_tools=False)
    if not result.text:
        # Leave state untouched: a failed summary must not silently discard
        # the conversation it was meant to preserve.
        logger.warning("/compact produced nothing on thread %s", thread_id)
        return COMPACT_FAILED

    await graph.aupdate_state(
        _config(thread_id), {"session_id": None, "summary": result.text}
    )
    logger.info("/compact on thread %s, summary %d chars", thread_id, len(result.text))
    return f"Compacted. Carrying forward:\n\n{result.text}"

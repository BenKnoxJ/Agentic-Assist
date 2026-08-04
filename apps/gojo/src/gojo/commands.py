"""Slash commands — the session controls Claude Code has.

Handled before the graph runs. A command is not a question: it manipulates
the conversation rather than asking anything, so it never reaches an agent
and never spends a turn.

Kept out of teams.py so it can be tested against a graph and a checkpointer
with no adapter, no Teams, and no subprocess.
"""

import logging

from gojo.agents.megumi import SUMMARISE, gather

logger = logging.getLogger(__name__)

HELP = """**Commands**
- `/new` — start a fresh conversation. I forget everything before this.
- `/compact` — summarise what we've covered and carry on from the summary.
- `/help` — this list.

Anything else is treated as a question."""

NEW_DONE = "Fresh start — I've forgotten everything before this."
COMPACT_EMPTY = "Nothing to compact yet — this conversation hasn't started."
COMPACT_FAILED = "I couldn't summarise the conversation, so I've left it as it was."


def is_command(text: str) -> bool:
    """Whether a message is a command rather than a question."""
    return text.strip().startswith("/")


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


async def handle(graph, text: str, thread_id: str) -> str:
    """Run a command and return what to say back.

    Unknown commands get the help text rather than being passed to an agent:
    a typo'd command should not silently become a prompt.
    """
    command = text.strip().split()[0].lower()

    if command == "/new":
        return await _new(graph, thread_id)
    if command == "/compact":
        return await _compact(graph, thread_id)
    return HELP


async def _new(graph, thread_id: str) -> str:
    """Drop the session and any carried summary.

    The checkpoint history is left alone - clearing the pointers is what
    makes the next turn start fresh, and keeping the history means a
    mistaken /new is still recoverable from the checkpointer.
    """
    await graph.aupdate_state(_config(thread_id), {"session_id": None, "summary": ""})
    logger.info("/new on thread %s", thread_id)
    return NEW_DONE


async def _compact(graph, thread_id: str) -> str:
    """Summarise the session, then start a new one seeded with the summary.

    This is the fix for resume latency, not just a tidiness feature: the SDK
    replays the transcript on every turn, so cost grows with conversation
    length. Compacting resets that to near zero while keeping the gist.
    """
    snapshot = await graph.aget_state(_config(thread_id))
    session_id = (snapshot.values or {}).get("session_id")

    if not session_id:
        return COMPACT_EMPTY

    result = await gather(SUMMARISE, resume=session_id)
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

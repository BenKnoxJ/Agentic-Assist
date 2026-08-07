"""Startup recovery - deliver the replies a restart interrupted.

ADR 0006 acknowledges a slow turn and answers it later. If the process dies in
between, the user holds an acknowledgement and gets nothing. The checkpointer
knows how far the graph got; the outbox knows a human is owed an answer and
where to send it. This is the pass that puts them together.

⚠ The identity check is not optional, and it must be the turn id. ainvoke(None,
config) returns whatever the thread's latest turn produced, so resuming a
thread the user has moved on from delivers the answer to their newest question
a second time. Matching on checkpoint id instead looks equivalent and is not:
the checkpoint advances while the owed turn is still running, so that guard
abandons the replies it exists to deliver. ADR 0008 Evidence.

Runs once per boot, after the app starts serving - which is safe because each
row is processed under its conversation's ADR 0009 lock, so live traffic on
that thread queues rather than racing the resume. Blocking the boot instead
would hold the service down for graph_timeout_seconds per owed row against
Restart=always.

ADR 0008.
"""

import logging

from microsoft_agents.activity import ConversationReference

from gojo import outbox
from gojo.approval import build_card, render_prompt
from gojo.config import get_settings
from gojo.logs import turn_id as turn_id_var
from gojo.orchestrator import GraphTimeout, gate_pending, lock_for, resume_turn
from gojo.teams import FAILED, deliver_reply, outcome_from_result

logger = logging.getLogger(__name__)

TIMED_OUT = (
    "That took too long and I stopped it. Nothing was changed - "
    "try a narrower question."
)


async def recover_owed_replies(conn, graph, adapter, agent_id: str) -> int:
    """Resume and deliver every owed reply. Returns how many were delivered.

    Never raises. This runs unattended at boot inside a task nothing awaits, so
    an exception escaping here would surface only as "Task exception was never
    retrieved" at garbage-collection time (10.4).
    """
    try:
        owed = await outbox.list_owed(conn)
    except Exception:
        logger.exception("could not read the owed-replies table")
        return 0

    if not owed:
        return 0

    logger.info("recovering %d owed repl(ies)", len(owed))
    delivered = 0

    for row in owed:
        # Same id the crashed turn logged under, so one grep spans both halves
        # rather than splitting into two unrelated traces.
        turn_id_var.set(row.turn_id)
        try:
            if await _deliver_one(conn, graph, adapter, agent_id, row):
                delivered += 1
        except Exception:
            # One unusable row must not cost every other row its answer - and
            # left in place it would fail again on every future boot.
            logger.exception(
                "abandoning unrecoverable owed reply on thread %s", row.thread_id
            )
            await _forget(conn, row.turn_id)

    logger.info("recovery delivered %d of %d owed repl(ies)", delivered, len(owed))
    return delivered


async def _forget(conn, turn_id: str) -> None:
    """Drop a row, tolerating a database that is itself the problem."""
    try:
        await outbox.clear(conn, turn_id)
    except Exception:
        logger.exception("could not clear owed reply %s", turn_id)


async def _deliver_one(conn, graph, adapter, agent_id: str, row) -> bool:
    """Handle one owed reply. Returns whether it was delivered."""
    max_age = get_settings().owed_reply_max_age_seconds

    if row.attempts >= outbox.MAX_ATTEMPTS:
        logger.warning(
            "abandoning owed reply on thread %s after %d attempts",
            row.thread_id,
            row.attempts,
        )
        await _forget(conn, row.turn_id)
        return False

    if row.age_seconds() > max_age:
        logger.warning(
            "abandoning owed reply on thread %s, %.0fs old (ceiling %.0fs)",
            row.thread_id,
            row.age_seconds(),
            max_age,
        )
        await _forget(conn, row.turn_id)
        return False

    # Everything from the guard to the row's settlement happens under the
    # conversation's lock (ADR 0009). /new and new messages queue behind it,
    # so nothing can invalidate the guard between check and delivery - the
    # window an earlier revision tried to shrink with re-checks does not
    # exist.
    async with lock_for(row.thread_id):
        # The guard. A different turn id means a newer turn owns this thread,
        # so the answer we would resume is not the one that was promised. The
        # lock cannot cover this case: the user moving on happened between
        # boots, sequentially.
        snapshot = await graph.aget_state(
            {"configurable": {"thread_id": row.thread_id}}
        )
        current = (snapshot.values or {}).get("turn_id")
        if current != row.turn_id:
            # WARNING, not INFO: this is a promise that will not be kept.
            logger.warning(
                "abandoning owed reply on thread %s - turn %s superseded by %s",
                row.thread_id,
                row.turn_id,
                current,
            )
            await _forget(conn, row.turn_id)
            return False

        proposal = (snapshot.values or {}).get("proposal") or {}
        if gate_pending(snapshot) and proposal:
            # A paused gate must NOT be resumed - ainvoke(None) would re-hit
            # the interrupt, read an empty reply and deliver FAILED, eating
            # the debt while the gate silently persists (ADR 0011). The
            # promise is re-issued as the question it always was; the gate
            # itself stays pending, and the user's next message decides it -
            # so clearing the row on delivery loses nothing.
            logger.info(
                "re-delivering approval prompt for action %s on thread %s",
                proposal.get("action_id"),
                row.thread_id,
            )
            text = render_prompt(proposal)
            card = build_card(proposal)
        else:
            card = None
            try:
                result = await resume_turn(graph, row.thread_id)
                # outcome_from_result, not result["reply"]: if the crash was
                # mid-compose, this resume itself runs INTO the gate and the
                # result carries __interrupt__ - that is the approval prompt,
                # never a failure (ADR 0011, review M2).
                outcome = outcome_from_result(result)
                text, card = outcome.text, outcome.card
            except GraphTimeout:
                # The same words a live turn uses when it overruns, rather
                # than the generic failure - the cause is different and the
                # user can act on it.
                logger.error("resumed turn timed out on thread %s", row.thread_id)
                text = TIMED_OUT
            except Exception:
                # A turn that cannot be completed is still answered. The
                # promise was a reply, not a correct one.
                logger.exception("could not resume thread %s", row.thread_id)
                text = FAILED

        reference = ConversationReference.model_validate_json(row.reference)
        if await deliver_reply(adapter, agent_id, reference, text, card):
            await _forget(conn, row.turn_id)
            return True

        attempts = await outbox.bump_attempts(conn, row.turn_id)
        logger.warning(
            "delivery failed on thread %s, attempt %d of %d",
            row.thread_id,
            attempts,
            outbox.MAX_ATTEMPTS,
        )
        return False

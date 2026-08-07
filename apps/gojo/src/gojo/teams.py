"""The Teams surface — build step 2.

Teams does not talk to this process. Azure Bot Service does: it POSTs a JSON
Activity to /api/messages and waits for a reply.

⚠ It waits for 10-15 seconds, depending on channel, then returns 504 to the
user (Microsoft, "Manage a long-running operation"). A Megumi turn is ~5s
today with no tools at max_turns=1; with connectors at step 3 and
max_turns=8 it will exceed that routinely.

So a turn takes one of two paths. Fast: a typing indicator, then the answer
as a single message on the same turn. Slow: the answer misses the budget, so
Gojo says so, returns before the channel times out, and delivers the answer
proactively when it arrives. ADR 0006.

The typing indicator is deliberate. An unconditional "on it" for answers
that arrive three seconds later is noise, and noise in a chat you use daily
is a real cost.

JWT validation is the SDK's, not ours. Hand-rolled validators routinely skip
the iss and aud checks, which is the difference between validating a token
and validating the right one (5.2).
"""

import asyncio
import logging
from dataclasses import dataclass

from langgraph.graph import END
from microsoft_agents.activity import (
    Activity,
    ActivityTypes,
    Attachment,
    ConversationReference,
)
from microsoft_agents.hosting.core import (
    AgentApplication,
    ApplicationOptions,
    MemoryStorage,
    TurnContext,
    TurnState,
)

from gojo import actions, outbox
from gojo.approval import build_card, parse_decision, render_prompt
from gojo.commands import handle as handle_command
from gojo.commands import is_command
from gojo.logs import new_turn_id
from gojo.orchestrator import (
    GraphTimeout,
    gate_pending,
    lock_for,
    resume_gate_locked,
    run_locked,
)

logger = logging.getLogger(__name__)

ACK = "On it — give me a moment."
REFUSAL = "This assistant is private and isn't available to you."

NO_LONGER_PENDING = "That action is no longer pending — nothing was done."
STALE_CARD = (
    "That approval button belongs to an earlier action — nothing was done. "
    "The current pending action is above."
)
CANCELLED_NOTICE = "Discarded the pending action — nothing was done."

CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"

# Module-level so recovery.py imports it rather than keeping a second copy. A
# recovered turn that cannot be finished should read like any other failed
# turn, and two identical strings with a comment asking future editors to keep
# them in step is not a mechanism.
FAILED = "Something went wrong on my side. Nothing was changed."


def is_authorised(
    aad_object_id: str | None,
    tenant_id: str | None,
    allowed_users: frozenset[str],
    expected_tenant: str,
) -> bool:
    """Whether this sender may use Gojo.

    Two independent checks, both of which must pass:

    1. The sender's Entra object ID is on the allow-list. A valid Bot Service
       token proves the message came from our bot, not that an authorised
       person sent it.
    2. The conversation's tenant matches ours. Redundant with single-tenant
       token validation, and kept because the cost is one comparison and the
       failure it guards against is silent.

    Fails closed on every missing value: no ID, no tenant, or an empty
    allow-list all deny.
    """
    if not allowed_users or not aad_object_id:
        return False
    if expected_tenant and tenant_id != expected_tenant:
        return False
    return aad_object_id in allowed_users

# asyncio holds only weak references to tasks, so a task nobody keeps can be
# garbage-collected mid-flight. Holding them here is what stops a reply
# vanishing silently.
_in_flight: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> None:
    """Hold a strong reference until the task finishes."""
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)


@dataclass(frozen=True)
class Outcome:
    """What a turn produced: text always (Teams needs a fallback and an
    empty message 400s), a card only for approval prompts."""

    text: str
    card: dict | None = None


@dataclass(frozen=True)
class GateTraffic:
    """What a message arriving on a possibly-paused thread turned out to be.

    kind: "none" (no gate involved - handle normally), "notice" (send text,
    stop), "cancelled" (send text, then handle the message normally),
    "resume" (resume the gate with .decision).
    """

    kind: str
    text: str = ""
    decision: str = ""


async def assess_gate_traffic(
    graph, activity_value: dict | None, message: str, thread_id: str
) -> GateTraffic:
    """The lock-critical approval section (ADR 0011, review B1).

    Everything that must be atomic happens inside the conversation's lock:
    detecting the paused gate (keyed on snapshot.next - interrupts go blind
    after any state update, M1), parsing the decision against the pending
    action_id, marking the ledger, and cancellation (END escape hatch +
    ledger row). The resume itself deliberately happens AFTER the lock is
    released - resume_gate_locked re-checks pending under its own
    acquisition, which is what makes a double tap safe.
    """
    async with lock_for(thread_id):
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        if not gate_pending(snapshot):
            if activity_value:
                # A tap on a card whose action is finished. Never let it
                # fall through - empty text would become a graph turn.
                return GateTraffic("notice", NO_LONGER_PENDING)
            return GateTraffic("none")

        proposal = (snapshot.values or {}).get("proposal") or {}
        action_id = proposal.get("action_id", "")
        decision = parse_decision(activity_value, message, action_id)
        conn = actions.connection()

        if decision is None:
            return GateTraffic("notice", STALE_CARD)

        if decision == "cancel":
            # Fully in-lock: abandon the pending task cleanly, cancel the
            # ledger row, and only then let the message continue as a
            # normal turn. Loud on purpose.
            await graph.aupdate_state(
                {"configurable": {"thread_id": thread_id}}, None, as_node=END
            )
            if conn is not None:
                await actions.cancel_thread(conn, thread_id)
            logger.info("gate cancelled on thread %s action_id=%s", thread_id, action_id)
            return GateTraffic("cancelled" if message else "notice", CANCELLED_NOTICE)

        if conn is not None and action_id:
            await actions.mark(
                conn, action_id, "approved" if decision == "approve" else "declined"
            )
        logger.info("gate decision=%s action_id=%s recorded", decision, action_id)
        return GateTraffic("resume", decision=decision)


def outcome_from_result(result: dict) -> Outcome:
    """Translate a graph result into what Teams should show.

    A result carrying "__interrupt__" (a list - langgraph 1.2.10) is a
    paused gate: the reply is the approval prompt, exact payload verbatim,
    with the card as decoration and the text as the always-wired fallback.
    """
    interrupts = result.get("__interrupt__")
    if interrupts:
        value = interrupts[0].value
        return Outcome(text=render_prompt(value), card=build_card(value))
    return Outcome(text=result.get("reply") or FAILED)


def _as_sendable(outcome: Outcome):
    if outcome.card is None:
        return outcome.text
    return Activity(
        type=ActivityTypes.message,
        text=outcome.text,
        attachments=[
            Attachment(content_type=CARD_CONTENT_TYPE, content=outcome.card)
        ],
    )


async def deliver_reply(
    adapter,
    agent_id: str,
    reference: ConversationReference,
    text: str,
    card: dict | None = None,
) -> bool:
    """Send a reply back into an existing conversation. Returns True on success.

    ⚠ Pass a continuation **Activity**, not the ConversationReference itself.
    ChannelAdapter.continue_conversation takes a reference and converts it, but
    ChannelServiceAdapter - which CloudAdapter actually inherits from -
    overrides the method to take an Activity. Reading the base class signature
    is not enough; passing a reference fails deep inside the SDK's telemetry
    with "'ConversationReference' object has no attribute 'recipient'", which
    names neither the argument nor the method at fault.
    """

    async def _send(context: TurnContext) -> None:
        await context.send_activity(_as_sendable(Outcome(text=text, card=card)))

    try:
        await adapter.continue_conversation(
            agent_id, reference.get_continuation_activity(), _send
        )
        return True
    except Exception:
        logger.exception("could not deliver proactive reply")
        return False


async def note_debt(outbox_conn, turn_id: str, thread_id: str, reference: str) -> bool:
    """Record that a reply is owed. Returns whether it was recorded.

    ⚠ Never raises. This runs immediately before the acknowledgement, so an
    exception here would cost the user their answer *and* the acknowledgement
    that one is coming - turning a database problem into a dead Teams turn.
    Losing the debt is strictly better (ADR 0008).
    """
    if outbox_conn is None:
        return False
    try:
        await outbox.record(outbox_conn, turn_id, thread_id, reference)
        return True
    except Exception:
        logger.exception("could not record an owed reply on thread %s", thread_id)
        return False


async def settle_debt(outbox_conn, turn_id: str, *, delivered: bool) -> None:
    """Close out an owed reply, or leave it for a later boot to retry.

    A failed delivery deliberately leaves the row: the answer is still owed.
    Note the outbox is only drained at startup, so an in-process failure waits
    for the next restart (ADR 0008).
    """
    if outbox_conn is None or not delivered:
        return
    await outbox.clear(outbox_conn, turn_id)


def build_agent_app(
    adapter,
    graph,
    agent_id: str,
    connections,
    allowed_users: frozenset[str],
    expected_tenant: str,
    fast_reply_seconds: float,
    outbox_conn=None,
) -> AgentApplication:
    """Wire the Teams surface onto an already-compiled graph.

    Args:
        adapter: the CloudAdapter that received the activity. Needed again to
            send the proactive reply, which is a separate outbound call.
        graph: the compiled orchestrator graph.
        agent_id: the Entra client ID. continue_conversation needs it to mint
            a token for the outbound call.
        connections: the MsalConnectionManager. The app builds its own
            Authorization from this and refuses to start without it.
        allowed_users: Entra object IDs permitted to use Gojo. Empty denies all.
        expected_tenant: the tenant every conversation must belong to.
        fast_reply_seconds: how long to wait for an answer before sending
            an acknowledgement instead. Must stay under the channel's
            response timeout.
        outbox_conn: an aiosqlite connection holding the owed-replies table,
            or None to record nothing. None is correct for /chat and for
            tests, where nothing is acknowledged and so nothing is owed.
    """
    # MemoryStorage holds the SDK's own conversation state, which is separate
    # from LangGraph's. In-process, so it does not survive a restart - matching
    # ADR 0006's accepted gap. Step 4 is where persistence arrives for both.
    app = AgentApplication(
        ApplicationOptions(
            adapter=adapter,
            storage=MemoryStorage(),
            # ⚠ Defaults to True. The SDK then runs its own typing indicator
            # on a background loop that re-sends via the reply-to-activity
            # endpoint, which Teams rejects with 400 Bad Request - surfacing
            # to the user as "Exception caught" while the turn itself
            # succeeds. One explicit typing activity per turn instead: sent
            # below, once, with no timer behind it.
            start_typing_timer=False,
        ),
        connection_manager=connections,
    )

    async def _run_graph(message: str, thread_id: str) -> Outcome:
        """Run one turn. Never raises - a failure becomes a reply.

        thread_id is the Teams conversation id, so state and the agent's
        session follow the chat rather than the process.

        run_locked, not run_turn: turns on one conversation queue rather than
        interleave (ADR 0009) - two overlapping invocations of one thread
        fork its checkpoint history, and the losing line's writes vanish.
        """
        try:
            return outcome_from_result(await run_locked(graph, message, thread_id))
        except GraphTimeout:
            logger.error("turn timed out on thread %s", thread_id)
            return Outcome(
                "That took too long and I stopped it. Nothing was changed - "
                "try a narrower question."
            )
        except Exception:
            # 10.4: a dead upstream degrades the answer, it does not kill the
            # process. The user gets told rather than left waiting forever.
            logger.exception("graph failed for a Teams turn")
            return Outcome(FAILED)

    async def _run_resume(decision: str, thread_id: str) -> Outcome:
        """Resume a paused gate. Never raises.

        resume_gate_locked returning None means the gate vanished between
        our lock section and this one (a cancellation or double tap won the
        race) - refused, not replayed (B1).
        """
        try:
            result = await resume_gate_locked(graph, decision, thread_id)
        except GraphTimeout:
            logger.error("gate resume timed out on thread %s", thread_id)
            return Outcome(
                "That took too long and I stopped it. Check the action "
                "ledger before approving again."
            )
        except Exception:
            logger.exception("gate resume failed on thread %s", thread_id)
            return Outcome(FAILED)
        if result is None:
            return Outcome(NO_LONGER_PENDING)
        return outcome_from_result(result)

    async def _deliver_when_done(
        task: asyncio.Task, reference: ConversationReference, turn_id: str
    ) -> None:
        """Wait for a slow turn, send its answer, and settle the debt."""
        outcome = await task
        delivered = await deliver_reply(
            adapter, agent_id, reference, outcome.text, outcome.card
        )
        await settle_debt(outbox_conn, turn_id, delivered=delivered)

    @app.activity(ActivityTypes.message)
    async def on_message(context: TurnContext, state: TurnState) -> bool:
        """Authorise, acknowledge inside the timeout, then hand off."""
        # Stamped before anything else so even a refusal is traceable. Kept:
        # it is also the identity an owed reply is recorded under (ADR 0008).
        turn = new_turn_id()
        sender = context.activity.from_property
        aad_id = sender.aad_object_id if sender else None
        tenant = context.activity.conversation.tenant_id if context.activity.conversation else None

        if not is_authorised(aad_id, tenant, allowed_users, expected_tenant):
            # Logged at WARNING with the object ID so an authorised user can be
            # added without hunting through the portal - and so unauthorised
            # attempts leave a trace rather than vanishing.
            logger.warning(
                "refused message from aad_object_id=%s name=%s tenant=%s",
                aad_id,
                sender.name if sender else None,
                tenant,
            )
            await context.send_activity(REFUSAL)
            return True

        message = (context.activity.text or "").strip()
        thread_id = context.activity.conversation.id
        value = context.activity.value

        # A card tap is never a command and never a normal turn - it routes
        # purely through the gate logic. Text goes to commands FIRST so /new
        # and /compact keep their own gate-aware handling rather than being
        # read as a cancellation.
        if not value and is_command(message):
            await context.send_activity(
                await handle_command(graph, message, thread_id, outbox_conn)
            )
            return True

        traffic = await assess_gate_traffic(graph, value, message, thread_id)
        if traffic.kind == "notice":
            await context.send_activity(traffic.text)
            return True
        if traffic.kind == "cancelled":
            # Loud, then the message continues as a normal turn below.
            await context.send_activity(traffic.text)

        # Captured before returning: once this turn ends the context is gone,
        # and the reference is the only way back to this conversation.
        reference = context.activity.get_conversation_reference()

        # A typing indicator rather than a message. Most turns finish inside
        # the budget below, and a chat littered with "on it" for answers that
        # arrive three seconds later is worse than no acknowledgement at all.
        await context.send_activity(Activity(type=ActivityTypes.typing))

        started = asyncio.get_running_loop().time()
        if traffic.kind == "resume":
            task = asyncio.create_task(_run_resume(traffic.decision, thread_id))
            # B2: an approved action must never vanish silently. The debt row
            # goes down BEFORE the resume attempt - even though execution is
            # one HTTP call and will usually beat the fast-reply budget - so
            # a crash mid-execute leaves recovery something to find.
            await note_debt(outbox_conn, turn, thread_id, reference.model_dump_json())
        else:
            task = asyncio.create_task(_run_graph(message, thread_id))
        _track(task)

        # Spend part of Azure Bot Service's response window waiting. If the
        # answer lands in time it goes back as a single message on this turn -
        # no acknowledgement, no proactive call.
        done, _ = await asyncio.wait({task}, timeout=fast_reply_seconds)
        elapsed = asyncio.get_running_loop().time() - started
        if task in done:
            logger.info("turn fast path: %.1fs (budget %.1fs)", elapsed, fast_reply_seconds)
            await context.send_activity(_as_sendable(task.result()))
            if traffic.kind == "resume":
                # The resume path always records debt (above); a delivered
                # fast-path answer settles it immediately.
                await settle_debt(outbox_conn, turn, delivered=True)
            return True

        # Slow turn: say so, return before the channel times out, and deliver
        # the answer proactively when it arrives (ADR 0006).
        logger.info(
            "turn slow path: still running at %.1fs (budget %.1fs) - sending ack",
            elapsed,
            fast_reply_seconds,
        )
        # The acknowledgement creates the debt (ADR 0008), so the row goes down
        # before it. `turn` is the whole identity: recovery matches it against
        # the turn id the graph stamped into state, which is stable while this
        # turn runs and changes when the next one starts. (Resume turns
        # recorded theirs already, before the attempt - B2.)
        if traffic.kind != "resume":
            await note_debt(outbox_conn, turn, thread_id, reference.model_dump_json())
        await context.send_activity(ACK)
        _track(asyncio.create_task(_deliver_when_done(task, reference, turn)))
        return True

    return app


def in_flight_count() -> int:
    """How many turns are mid-flight. For the health check."""
    return len(_in_flight)

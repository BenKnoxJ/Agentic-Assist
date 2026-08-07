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

from gojo import outbox
from gojo.commands import handle as handle_command
from gojo.commands import is_command
from gojo.logs import new_turn_id
from gojo.orchestrator import GraphTimeout, run_locked

logger = logging.getLogger(__name__)

ACK = "On it — give me a moment."
REFUSAL = "This assistant is private and isn't available to you."

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


async def deliver_reply(
    adapter, agent_id: str, reference: ConversationReference, text: str
) -> bool:
    """Send `text` back into an existing conversation. Returns True on success.

    ⚠ Pass a continuation **Activity**, not the ConversationReference itself.
    ChannelAdapter.continue_conversation takes a reference and converts it, but
    ChannelServiceAdapter - which CloudAdapter actually inherits from -
    overrides the method to take an Activity. Reading the base class signature
    is not enough; passing a reference fails deep inside the SDK's telemetry
    with "'ConversationReference' object has no attribute 'recipient'", which
    names neither the argument nor the method at fault.
    """

    async def _send(context: TurnContext) -> None:
        await context.send_activity(text)

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

    async def _run_graph(message: str, thread_id: str) -> str:
        """Run one turn. Never raises - a failure becomes a reply.

        thread_id is the Teams conversation id, so state and the agent's
        session follow the chat rather than the process.

        run_locked, not run_turn: turns on one conversation queue rather than
        interleave (ADR 0009) - two overlapping invocations of one thread
        fork its checkpoint history, and the losing line's writes vanish.
        """
        try:
            result = await run_locked(graph, message, thread_id)
            # Belt to respond's braces: an empty message 400s at the channel.
            return result["reply"] or FAILED
        except GraphTimeout:
            logger.error("turn timed out on thread %s", thread_id)
            return (
                "That took too long and I stopped it. Nothing was changed - "
                "try a narrower question."
            )
        except Exception:
            # 10.4: a dead upstream degrades the answer, it does not kill the
            # process. The user gets told rather than left waiting forever.
            logger.exception("graph failed for a Teams turn")
            return FAILED

    async def _deliver_when_done(
        task: asyncio.Task, reference: ConversationReference, turn_id: str
    ) -> None:
        """Wait for a slow turn, send its answer, and settle the debt."""
        delivered = await deliver_reply(adapter, agent_id, reference, await task)
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

        # --- TEMPORARY: step-5 T1 card spike. Removed once the submit payload
        # shape is recorded as the T7 test fixture (build-log Session 6).
        if context.activity.value:
            logger.info(
                "SPIKE submit: value=%r text=%r channel_data=%r",
                context.activity.value,
                context.activity.text,
                context.activity.channel_data,
            )
            await context.send_activity(
                f"Card submit received. value={context.activity.value!r}"
            )
            return True
        if message.startswith("/cardspike"):
            parts = message.split()
            version = parts[1] if len(parts) > 1 else "1.5"
            card = {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": version,
                "body": [
                    {
                        "type": "TextBlock",
                        "text": f"Card spike — schema {version}",
                        "weight": "Bolder",
                        "wrap": True,
                    },
                    {
                        "type": "TextBlock",
                        "text": "If this renders and the buttons work, tap one.",
                        "wrap": True,
                    },
                ],
                "actions": [
                    {
                        "type": "Action.Submit",
                        "title": "Approve",
                        "data": {"gojo_action": "approve", "action_id": f"spike-{version}"},
                    },
                    {
                        "type": "Action.Submit",
                        "title": "Reject",
                        "data": {"gojo_action": "reject", "action_id": f"spike-{version}"},
                    },
                ],
            }
            await context.send_activity(
                Activity(
                    type=ActivityTypes.message,
                    attachments=[
                        Attachment(
                            content_type="application/vnd.microsoft.card.adaptive",
                            content=card,
                        )
                    ],
                )
            )
            return True
        # --- end TEMPORARY spike ---

        # Commands are answered on this turn and never reach an agent. They
        # are fast, so there is no acknowledgement and no proactive delivery.
        if is_command(message):
            await context.send_activity(
                await handle_command(graph, message, thread_id, outbox_conn)
            )
            return True

        # Captured before returning: once this turn ends the context is gone,
        # and the reference is the only way back to this conversation.
        reference = context.activity.get_conversation_reference()

        # A typing indicator rather than a message. Most turns finish inside
        # the budget below, and a chat littered with "on it" for answers that
        # arrive three seconds later is worse than no acknowledgement at all.
        await context.send_activity(Activity(type=ActivityTypes.typing))

        started = asyncio.get_running_loop().time()
        task = asyncio.create_task(_run_graph(message, thread_id))
        _track(task)

        # Spend part of Azure Bot Service's response window waiting. If the
        # answer lands in time it goes back as a single message on this turn -
        # no acknowledgement, no proactive call.
        done, _ = await asyncio.wait({task}, timeout=fast_reply_seconds)
        elapsed = asyncio.get_running_loop().time() - started
        if task in done:
            logger.info("turn fast path: %.1fs (budget %.1fs)", elapsed, fast_reply_seconds)
            await context.send_activity(task.result())
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
        # turn runs and changes when the next one starts.
        await note_debt(outbox_conn, turn, thread_id, reference.model_dump_json())
        await context.send_activity(ACK)
        _track(asyncio.create_task(_deliver_when_done(task, reference, turn)))
        return True

    return app


def in_flight_count() -> int:
    """How many turns are mid-flight. For the health check."""
    return len(_in_flight)

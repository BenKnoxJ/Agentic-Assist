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

from microsoft_agents.activity import Activity, ActivityTypes, ConversationReference
from microsoft_agents.hosting.core import (
    AgentApplication,
    ApplicationOptions,
    MemoryStorage,
    TurnContext,
    TurnState,
)

logger = logging.getLogger(__name__)

ACK = "On it — give me a moment."
REFUSAL = "This assistant is private and isn't available to you."


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


def build_agent_app(
    adapter,
    graph,
    agent_id: str,
    connections,
    allowed_users: frozenset[str],
    expected_tenant: str,
    fast_reply_seconds: float,
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
    """
    # MemoryStorage holds the SDK's own conversation state, which is separate
    # from LangGraph's. In-process, so it does not survive a restart - matching
    # ADR 0006's accepted gap. Step 4 is where persistence arrives for both.
    app = AgentApplication(
        ApplicationOptions(adapter=adapter, storage=MemoryStorage()),
        connection_manager=connections,
    )

    async def _run_graph(message: str) -> str:
        """Run one turn. Never raises - a failure becomes a reply."""
        try:
            result = await graph.ainvoke({"message": message, "steps": [], "findings": []})
            return result["reply"]
        except Exception:
            # 10.4: a dead upstream degrades the answer, it does not kill the
            # process. The user gets told rather than left waiting forever.
            logger.exception("graph failed for a Teams turn")
            return "Something went wrong on my side. Nothing was changed."

    async def _deliver_when_done(task: asyncio.Task, reference: ConversationReference) -> None:
        """Wait for a slow turn and send its answer as a new message."""
        await deliver_reply(adapter, agent_id, reference, await task)

    @app.activity(ActivityTypes.message)
    async def on_message(context: TurnContext, state: TurnState) -> bool:
        """Authorise, acknowledge inside the timeout, then hand off."""
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

        # Captured before returning: once this turn ends the context is gone,
        # and the reference is the only way back to this conversation.
        reference = context.activity.get_conversation_reference()
        message = context.activity.text or ""

        # A typing indicator rather than a message. Most turns finish inside
        # the budget below, and a chat littered with "on it" for answers that
        # arrive three seconds later is worse than no acknowledgement at all.
        await context.send_activity(Activity(type=ActivityTypes.typing))

        task = asyncio.create_task(_run_graph(message))
        _track(task)

        # Spend part of Azure Bot Service's response window waiting. If the
        # answer lands in time it goes back as a single message on this turn -
        # no acknowledgement, no proactive call.
        done, _ = await asyncio.wait({task}, timeout=fast_reply_seconds)
        if task in done:
            await context.send_activity(task.result())
            return True

        # Slow turn: say so, return before the channel times out, and deliver
        # the answer proactively when it arrives (ADR 0006).
        await context.send_activity(ACK)
        _track(asyncio.create_task(_deliver_when_done(task, reference)))
        return True

    return app


def in_flight_count() -> int:
    """How many turns are mid-flight. For the health check."""
    return len(_in_flight)

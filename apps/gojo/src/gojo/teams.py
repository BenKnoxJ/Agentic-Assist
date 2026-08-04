"""The Teams surface — build step 2.

Teams does not talk to this process. Azure Bot Service does: it POSTs a JSON
Activity to /api/messages and waits for a reply.

⚠ It waits for 10-15 seconds, depending on channel, then returns 504 to the
user (Microsoft, "Manage a long-running operation"). A Megumi turn is ~5s
today with no tools at max_turns=1; with connectors at step 3 and
max_turns=8 it will exceed that routinely. So every turn is answered in two
parts: an immediate acknowledgement inside the timeout, then the real reply
sent proactively when the graph finishes. ADR 0006.

JWT validation is the SDK's, not ours. Hand-rolled validators routinely skip
the iss and aud checks, which is the difference between validating a token
and validating the right one (5.2).
"""

import asyncio
import logging

from microsoft_agents.activity import ActivityTypes, ConversationReference
from microsoft_agents.hosting.core import AgentApplication, TurnContext, TurnState

logger = logging.getLogger(__name__)

ACK = "On it — give me a moment."

# asyncio holds only weak references to tasks, so a task nobody keeps can be
# garbage-collected mid-flight. Holding them here is what stops a reply
# vanishing silently.
_in_flight: set[asyncio.Task] = set()


def build_agent_app(adapter, graph, agent_id: str) -> AgentApplication:
    """Wire the Teams surface onto an already-compiled graph.

    Args:
        adapter: the CloudAdapter that received the activity. Needed again to
            send the proactive reply, which is a separate outbound call.
        graph: the compiled orchestrator graph.
        agent_id: the Entra client ID. continue_conversation needs it to mint
            a token for the outbound call.
    """
    app = AgentApplication()

    async def _reply_later(reference: ConversationReference, message: str) -> None:
        """Run the graph, then deliver the answer as a new outbound message."""
        try:
            result = await graph.ainvoke({"message": message, "steps": [], "findings": []})
            reply = result["reply"]
        except Exception:
            # 10.4: a dead upstream degrades the answer, it does not kill the
            # process. The user gets told rather than left waiting forever.
            logger.exception("graph failed for a Teams turn")
            reply = "Something went wrong on my side. Nothing was changed."

        async def _send(context: TurnContext) -> None:
            await context.send_activity(reply)

        try:
            await adapter.continue_conversation(agent_id, reference, _send)
        except Exception:
            logger.exception("could not deliver proactive reply")

    @app.activity(ActivityTypes.message)
    async def on_message(context: TurnContext, state: TurnState) -> bool:
        """Acknowledge inside the timeout, then hand off to the graph."""
        # Captured before returning: once this turn ends the context is gone,
        # and the reference is the only way back to this conversation.
        reference = context.activity.get_conversation_reference()
        message = context.activity.text or ""

        await context.send_activity(ACK)

        task = asyncio.create_task(_reply_later(reference, message))
        _in_flight.add(task)
        task.add_done_callback(_in_flight.discard)
        return True

    return app


def in_flight_count() -> int:
    """How many turns are mid-flight. For the health check."""
    return len(_in_flight)

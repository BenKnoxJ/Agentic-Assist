"""Tests for proactive reply delivery.

Regression cover for a live failure: continue_conversation was called with a
ConversationReference because that is what ChannelAdapter's signature takes.
CloudAdapter inherits ChannelServiceAdapter, which overrides the method to
take a continuation Activity instead. The acknowledgement arrived, the answer
never did, and the traceback surfaced inside the SDK's telemetry as
"'ConversationReference' object has no attribute 'recipient'".
"""

import pytest
from microsoft_agents.activity import (
    Activity,
    ActivityTypes,
    ChannelAccount,
    ConversationAccount,
    ConversationReference,
)

from gojo.teams import deliver_reply

AGENT_ID = "2b6bad70-0000-0000-0000-000000000000"


def a_reference() -> ConversationReference:
    """A reference of the shape Teams produces."""
    activity = Activity(
        type=ActivityTypes.message,
        text="hello",
        channel_id="msteams",
        service_url="https://smba.trafficmanager.net/uk/",
        from_property=ChannelAccount(id="29:user", name="Ben Knox"),
        recipient=ChannelAccount(id="28:bot", name="Gojo"),
        conversation=ConversationAccount(id="a:conv", tenant_id="tenant"),
    )
    return activity.get_conversation_reference()


class RecordingAdapter:
    """Captures what continue_conversation was handed."""

    def __init__(self) -> None:
        self.received = None

    async def continue_conversation(self, agent_id, continuation, callback):
        self.received = continuation


@pytest.mark.asyncio
async def test_delivery_passes_an_activity_not_a_reference() -> None:
    """The whole point. A ConversationReference here breaks delivery."""
    adapter = RecordingAdapter()
    assert await deliver_reply(adapter, AGENT_ID, a_reference(), "the answer") is True

    assert not isinstance(adapter.received, ConversationReference)
    assert isinstance(adapter.received, Activity)


@pytest.mark.asyncio
async def test_delivered_activity_has_the_attributes_the_sdk_reads() -> None:
    """`recipient` is what the telemetry span touched when this failed."""
    adapter = RecordingAdapter()
    await deliver_reply(adapter, AGENT_ID, a_reference(), "the answer")

    assert adapter.received.recipient is not None
    assert adapter.received.conversation is not None


@pytest.mark.asyncio
async def test_delivery_failure_is_contained() -> None:
    """A failed send must not escape into the background task (10.4)."""

    class BrokenAdapter:
        async def continue_conversation(self, agent_id, continuation, callback):
            raise RuntimeError("service url unreachable")

    assert await deliver_reply(BrokenAdapter(), AGENT_ID, a_reference(), "x") is False

"""Graph state for the Gojo orchestrator."""

from typing import Annotated, Literal, TypedDict

Intent = Literal["gather", "act", "unknown"]


def accumulate(left: list[str], right: list[str] | None) -> list[str]:
    """Append within a turn; clear when handed None.

    Plain operator.add was correct while every invocation started from an
    empty state. With a checkpointer the state survives between messages, so
    an appending reducer would grow the step and finding lists for the life
    of the conversation - 6.3 rule 3's unbounded growth, arriving quietly.

    The new_turn node clears these by returning None, so each turn reports
    only its own path.
    """
    if right is None:
        return []
    return left + right


class GojoState(TypedDict):
    """State flowing through the orchestrator graph.

    Fields without a reducer are overwritten on each update.

    ⚠ No conversation transcript lives here. The Agent SDK owns the
    conversation and `session_id` is the handle to it (6.3 rule 3). Persisting
    the id rather than the messages is what keeps this state small no matter
    how long a conversation runs.
    """

    message: str
    intent: Intent
    reply: str
    steps: Annotated[list[str], accumulate]
    findings: Annotated[list[str], accumulate]

    # Survives between turns via the checkpointer. None means "start fresh",
    # which is also what /new restores.
    session_id: str | None

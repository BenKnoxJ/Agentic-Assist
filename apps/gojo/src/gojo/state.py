"""Graph state for the Gojo orchestrator."""

import operator
from typing import Annotated, Literal, TypedDict

Intent = Literal["gather", "act", "unknown"]


class GojoState(TypedDict):
    """State flowing through the orchestrator graph.

    Fields without a reducer are overwritten on each update.
    Fields annotated with operator.add accumulate across nodes.
    """

    message: str
    intent: Intent
    reply: str
    steps: Annotated[list[str], operator.add]
    findings: Annotated[list[str], operator.add]

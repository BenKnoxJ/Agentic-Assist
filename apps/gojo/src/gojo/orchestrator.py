"""Gojo - the orchestrator. Routes, holds state, enforces the gate.

Not an agent: no reasoning happens here. It decides which agent runs
and what happens with the result.
"""

import asyncio

from langgraph.graph import END, START, StateGraph

from gojo.agents.megumi import gather
from gojo.state import GojoState

ACTION_WORDS = ("send", "reply", "create", "update", "close", "delete", "assign")


def classify(state: GojoState) -> dict:
    """Deterministic intent classification. Kept deterministic on purpose."""
    text = state["message"].lower()
    intent = "act" if any(w in text for w in ACTION_WORDS) else "gather"
    print(f"[classify] intent={intent}")
    return {"intent": intent, "steps": ["classify"]}


def route_by_intent(state: GojoState) -> str:
    """Router: reads state, returns a label. Does no work, changes no state."""
    return state["intent"]


async def megumi(state: GojoState) -> dict:
    """Gather agent - read-only. Delegates to the Agent SDK."""
    print("[megumi] gathering")
    findings = await gather(state["message"])
    return {"findings": [findings], "steps": ["megumi"]}


def sukuna(state: GojoState) -> dict:
    """Act agent - writes. Stub. Only ever runs behind interrupt()."""
    print("[sukuna] acting (stub)")
    return {"steps": ["sukuna"]}


def respond(state: GojoState) -> dict:
    """Format the reply. A node, not an agent - one call, no tools."""
    print("[respond] composing")
    findings = state["findings"]
    body = findings[0] if findings else "(no findings)"
    return {"reply": body, "steps": ["respond"]}


def build_graph():
    """Assemble and compile the orchestrator graph."""
    builder = StateGraph(GojoState)

    builder.add_node("classify", classify)
    builder.add_node("megumi", megumi)
    builder.add_node("sukuna", sukuna)
    builder.add_node("respond", respond)

    builder.add_edge(START, "classify")
    builder.add_conditional_edges(
        "classify",
        route_by_intent,
        {"gather": "megumi", "act": "sukuna", "unknown": "respond"},
    )
    builder.add_edge("megumi", "respond")
    builder.add_edge("sukuna", "respond")
    builder.add_edge("respond", END)

    return builder.compile()


async def main() -> None:
    graph = build_graph()
    for msg in ["what needs my attention today", "send a reply to Dave"]:
        print(f"\n--- {msg} ---")
        result = await graph.ainvoke({"message": msg, "steps": [], "findings": []})
        print("REPLY:", result["reply"])
        print("PATH:", result["steps"])


if __name__ == "__main__":
    asyncio.run(main())

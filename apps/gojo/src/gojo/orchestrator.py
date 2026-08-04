"""Gojo - the orchestrator. Routes, holds state, enforces the gate.

Not an agent: no reasoning happens here. It decides which agent runs
and what happens with the result.
"""

import asyncio

from langgraph.graph import END, START, StateGraph

from gojo.agents.megumi import gather
from gojo.state import GojoState

ACTION_WORDS = ("send", "reply", "create", "update", "close", "delete", "assign")


def new_turn(state: GojoState) -> dict:
    """Clear per-turn state before anything runs.

    With a checkpointer the previous turn's state is still here. Steps and
    findings describe one turn and must not carry over; session_id must.
    """
    return {"steps": None, "findings": None, "reply": ""}


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
    """Gather agent - read-only. Delegates to the Agent SDK.

    Carries session_id in and back out, which is what makes a conversation
    continue rather than restart on every message.
    """
    print("[megumi] gathering")
    result = await gather(state["message"], resume=state.get("session_id"))
    return {
        "findings": [result.text],
        "steps": ["megumi"],
        "session_id": result.session_id,
    }


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


def build_graph(checkpointer=None):
    """Assemble and compile the orchestrator graph.

    Args:
        checkpointer: a LangGraph checkpointer. Without one the graph is
            stateless and every invocation starts fresh - fine for tests and
            for /chat, not for a conversation.
    """
    builder = StateGraph(GojoState)

    builder.add_node("new_turn", new_turn)
    builder.add_node("classify", classify)
    builder.add_node("megumi", megumi)
    builder.add_node("sukuna", sukuna)
    builder.add_node("respond", respond)

    builder.add_edge(START, "new_turn")
    builder.add_edge("new_turn", "classify")
    builder.add_conditional_edges(
        "classify",
        route_by_intent,
        {"gather": "megumi", "act": "sukuna", "unknown": "respond"},
    )
    builder.add_edge("megumi", "respond")
    builder.add_edge("sukuna", "respond")
    builder.add_edge("respond", END)

    return builder.compile(checkpointer=checkpointer)


async def main() -> None:
    graph = build_graph()
    for msg in ["what needs my attention today", "send a reply to Dave"]:
        print(f"\n--- {msg} ---")
        result = await graph.ainvoke({"message": msg, "steps": [], "findings": []})
        print("REPLY:", result["reply"])
        print("PATH:", result["steps"])


if __name__ == "__main__":
    asyncio.run(main())

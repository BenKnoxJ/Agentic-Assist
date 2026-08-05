"""Gojo - the orchestrator. Routes, holds state, enforces the gate.

Not an agent: no reasoning happens here. It decides which agent runs
and what happens with the result.
"""

import asyncio
import logging

from langgraph.graph import END, START, StateGraph

from gojo.agents.megumi import gather
from gojo.config import get_settings
from gojo.logs import turn_id as turn_id_var
from gojo.state import GojoState

logger = logging.getLogger(__name__)

BUDGET_EXHAUSTED = (
    "I stopped before finishing - this turn used its whole allowance of "
    "agent steps. Nothing was changed. Try narrowing the question."
)

ACTION_WORDS = ("send", "reply", "create", "update", "close", "delete", "assign")


def new_turn(state: GojoState) -> dict:
    """Clear per-turn state before anything runs.

    With a checkpointer the previous turn's state is still here. Steps and
    findings describe one turn and must not carry over; session_id must.

    The turn id is stamped here rather than anywhere else because this node
    runs exactly once per turn, at the start - which is what makes it a
    reliable identity for recovery to match against (ADR 0008).
    """
    return {
        "steps": None,
        "findings": None,
        "reply": "",
        "agent_calls": 0,
        "turn_id": turn_id_var.get(),
    }


def classify(state: GojoState) -> dict:
    """Deterministic intent classification. Kept deterministic on purpose."""
    text = state["message"].lower()
    intent = "act" if any(w in text for w in ACTION_WORDS) else "gather"
    logger.info("classify intent=%s", intent)
    return {"intent": intent, "steps": ["classify"]}


def route_by_intent(state: GojoState) -> str:
    """Router: reads state, returns a label. Does no work, changes no state."""
    return state["intent"]


async def megumi(state: GojoState) -> dict:
    """Gather agent - read-only. Delegates to the Agent SDK.

    Carries session_id in and back out, which is what makes a conversation
    continue rather than restart on every message.
    """
    used = state.get("agent_calls", 0)
    budget = get_settings().max_agent_calls_per_turn
    if used >= budget:
        # 9.3: route to a graceful exit rather than being killed mid-work.
        # The user is told, and nothing is left half-done.
        logger.warning("agent budget exhausted: %d calls used this turn", used)
        return {"findings": [BUDGET_EXHAUSTED], "steps": ["megumi:over-budget"]}

    logger.info("megumi gathering")
    result = await gather(
        state["message"],
        resume=state.get("session_id"),
        summary=state.get("summary", ""),
    )

    # The only spend signal that crosses the subprocess boundary - LangSmith
    # cannot see inside it (9.2), so it is logged here or nowhere.
    logger.info(
        "megumi turn: cost_usd=%s sdk_turns=%s", result.cost_usd, result.num_turns
    )

    return {
        "findings": [result.text],
        "steps": ["megumi"],
        "session_id": result.session_id,
        "agent_calls": used + 1,
    }


def sukuna(state: GojoState) -> dict:
    """Act agent - writes. Stub. Only ever runs behind interrupt()."""
    logger.info("sukuna acting (stub)")
    return {"steps": ["sukuna"]}


def respond(state: GojoState) -> dict:
    """Format the reply. A node, not an agent - one call, no tools.

    The reply must never be empty: Teams rejects an empty message with 400
    BadSyntax, and the SDK surfaces that into the chat as "Exception caught".
    Found live 5 Aug 2026 - a split message made megumi return empty text,
    and [""] is truthy while "" is not, so the old `if findings` check let
    the empty string straight through.
    """
    logger.info("respond composing")
    findings = state["findings"]
    body = (findings[0] if findings else "") or "(no findings)"
    return {"reply": body, "steps": ["respond"]}


class GraphTimeout(Exception):
    """The graph exceeded its wall-clock budget."""


async def run_turn(graph, message: str, thread_id: str) -> dict:
    """Invoke the graph for one turn, with both 9.3 guards applied.

    Every caller goes through here. Applying the timeout and recursion limit
    at each call site instead would mean applying them in one surface and
    forgetting them in the next - which is how a guard ends up protecting
    /chat but not the thing that actually faces Teams.

    Raises:
        GraphTimeout: the turn exceeded graph_timeout_seconds. The Agent SDK
            subprocess is abandoned rather than awaited; the alternative is a
            request that never returns.
    """
    settings = get_settings()
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings.recursion_limit,
    }
    initial = {"message": message, "steps": [], "findings": []}

    try:
        return await asyncio.wait_for(
            graph.ainvoke(initial, config), timeout=settings.graph_timeout_seconds
        )
    except TimeoutError as exc:
        logger.error(
            "graph timed out after %ss on thread %s",
            settings.graph_timeout_seconds,
            thread_id,
        )
        raise GraphTimeout(
            f"turn exceeded {settings.graph_timeout_seconds}s"
        ) from exc


async def resume_turn(graph, thread_id: str) -> dict:
    """Finish a turn that was interrupted, from its last checkpoint.

    The sibling of run_turn: same guards, no initial state. Passing None tells
    LangGraph to continue the thread rather than start a turn.

    Verified against langgraph 1.2.10: a thread that had already completed
    returns its final state and re-runs nothing, so the caller needs no branch
    for "crashed before finishing" versus "crashed before delivering".

    ⚠ It does not check *which* turn it resumes. On a thread that has since run
    a new turn this returns the new turn's state, so the caller must establish
    that the thread still belongs to the turn it cares about. recovery.py does
    that by matching turn ids.

    Separate from run_turn rather than a flag on it for the reason run_turn's
    own docstring gives - the 9.3 guards live at one surface.

    Raises:
        GraphTimeout: the resumed turn exceeded graph_timeout_seconds.
    """
    settings = get_settings()
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings.recursion_limit,
    }

    try:
        return await asyncio.wait_for(
            graph.ainvoke(None, config), timeout=settings.graph_timeout_seconds
        )
    except TimeoutError as exc:
        logger.error(
            "resumed turn timed out after %ss on thread %s",
            settings.graph_timeout_seconds,
            thread_id,
        )
        raise GraphTimeout(
            f"resumed turn exceeded {settings.graph_timeout_seconds}s"
        ) from exc


# One lock per conversation, process-wide. A LangGraph thread is a
# single-writer structure, and this registry is what makes that true in
# practice: live turns, /chat turns, commands and recovery all take the
# conversation's lock for their whole critical section. Grows with distinct
# thread ids; bounded in practice by the conversations one user opens.
# ADR 0009.
_thread_locks: dict[str, asyncio.Lock] = {}


def lock_for(thread_id: str) -> asyncio.Lock:
    """The serialisation lock for one conversation (ADR 0009).

    ⚠ Not re-entrant, and deliberately NOT taken inside run_turn or
    resume_turn: recovery must hold it across a wider span (guard, resume,
    deliver, clear), and a lock inside the turn functions would deadlock it.
    Callers wrap their own critical sections. ADR 0009 records why this is
    an exception to 9.3's one-surface pattern.
    """
    return _thread_locks.setdefault(thread_id, asyncio.Lock())


async def run_locked(graph, message: str, thread_id: str) -> dict:
    """run_turn under the conversation's lock - the live-path entry point."""
    async with lock_for(thread_id):
        return await run_turn(graph, message, thread_id)


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

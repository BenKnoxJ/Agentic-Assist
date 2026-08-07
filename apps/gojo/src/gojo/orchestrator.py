"""Gojo - the orchestrator. Routes, holds state, enforces the gate.

Not an agent: no reasoning happens here. It decides which agent runs
and what happens with the result.
"""

import asyncio
import logging

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from gojo import actions
from gojo.actions import ActionError, new_action_id, parse_proposal
from gojo.agents.megumi import gather
from gojo.agents.sukuna import compose
from gojo.config import get_settings
from gojo.logs import turn_id as turn_id_var
from gojo.state import GojoState
from gojo_graph import GraphError

logger = logging.getLogger(__name__)

BUDGET_EXHAUSTED = (
    "I stopped before finishing - this turn used its whole allowance of "
    "agent steps. Nothing was changed. Try narrowing the question."
)

# Substring-matched, so phrases are chosen against noun collisions: bare
# "draft"/"email" would misroute "the draft agreement" and "any email from
# Amy?". Misroute is a UX error, never a safety one - the gather path cannot
# write and the act path cannot act without approval (ADR 0011) - but the
# read questions are the product, so they get the benefit of the doubt.
ACTION_WORDS = (
    "send",
    "reply",
    "create",
    "update",
    "close",
    "delete",
    "assign",
    "compose",
    "draft an email",
    "draft a mail",
    "draft a message",
    "write an email",
    "write a mail",
)


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
        # A fresh message never inherits a pending action: by the time a new
        # input reaches the graph, teams.py has already resolved or loudly
        # cancelled any paused gate (a new input silently discards a pending
        # interrupt - verified langgraph 1.2.10 behaviour, ADR 0011).
        "proposal": None,
        "decision": "",
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


COMPOSE_FAILED = (
    "I couldn't put together a safe action from that. Nothing was proposed "
    "or changed - try being more specific."
)

DISCARDED = "Okay — discarded. Nothing was done."


async def sukuna(state: GojoState, config: RunnableConfig | None = None) -> dict:
    """Compose agent - proposes writes, never performs them (ADR 0011).

    Holds the same read-only tools as megumi; its output is one JSON
    proposal, parsed strictly. Anything that doesn't parse is a fail-safe
    reply, never an execution. The one deterministic step here: for a
    reply, the target message is fetched by id via the connector so the
    human is shown real sender/subject, never agent prose about them.
    """
    used = state.get("agent_calls", 0)
    budget = get_settings().max_agent_calls_per_turn
    if used >= budget:
        logger.warning("agent budget exhausted: %d calls used this turn", used)
        return {"findings": [BUDGET_EXHAUSTED], "steps": ["sukuna:over-budget"]}

    logger.info("sukuna composing")
    result = await compose(
        state["message"],
        resume=state.get("session_id"),
        summary=state.get("summary", ""),
    )
    logger.info(
        "sukuna turn: cost_usd=%s sdk_turns=%s", result.cost_usd, result.num_turns
    )

    # ⚠ Deliberately NOT writing result.session_id back: megumi's thread
    # stays canonical, compose only borrows its context (ADR 0011, M4).
    base = {"steps": ["sukuna"], "agent_calls": used + 1}

    proposal = parse_proposal(result.text)
    if proposal is None:
        return {**base, "findings": [COMPOSE_FAILED]}

    verified_target = None
    if proposal.kind == "reply":
        client = actions.write_client()
        if client is None:
            return {
                **base,
                "findings": [
                    "The mail connector isn't configured, so I can't verify "
                    "the reply target. Nothing was proposed."
                ],
            }
        try:
            target = await client.get_message(proposal.reply_to_message_id)
        except GraphError as exc:
            return {
                **base,
                "findings": [
                    f"I couldn't verify the reply target: {exc} Nothing was proposed."
                ],
            }
        verified_target = {"from": target["from"], "subject": target["subject"]}

    conn = actions.connection()
    if conn is None:
        return {
            **base,
            "findings": [
                "The action ledger isn't available, so I can't propose writes "
                "right now. Nothing was changed."
            ],
        }

    if config is None:
        # Inside a graph run the config comes from the runtime context; the
        # explicit parameter exists so node-level tests can pass one.
        try:
            config = get_config()
        except Exception:
            config = {}
    action_id = new_action_id()
    thread_id = config.get("configurable", {}).get("thread_id", "-")
    await actions.record_proposed(
        conn, action_id, thread_id, state.get("turn_id", "-"), proposal
    )
    logger.info(
        "sukuna proposed action_id=%s op=%s kind=%s", action_id, proposal.op, proposal.kind
    )
    return {
        **base,
        "proposal": {
            "action_id": action_id,
            "payload": proposal.model_dump(),
            "verified_target": verified_target,
        },
    }


def route_after_sukuna(state: GojoState) -> str:
    """Gate only when there is something to gate."""
    return "gate" if state.get("proposal") else "respond"


def gate(state: GojoState) -> dict:
    """The seal. Pauses the graph until the owner decides (ADR 0011).

    interrupt() is the FIRST statement on purpose: the node re-executes from
    its start when resumed (verified langgraph 1.2.10 behaviour), so nothing
    with a side effect may precede it - the proposal was recorded upstream
    in sukuna. The fresh turn_id stamp keeps the outbox/recovery machinery
    coherent for the approval turn, which never runs new_turn.
    """
    decision = str(interrupt(state["proposal"]))
    logger.info(
        "gate decision=%s action_id=%s", decision, state["proposal"]["action_id"]
    )
    update = {"decision": decision, "turn_id": turn_id_var.get(), "steps": ["gate"]}
    if decision != "approve":
        update["findings"] = [DISCARDED]
    return update


def route_after_gate(state: GojoState) -> str:
    return "execute" if state.get("decision") == "approve" else "respond"


async def execute_action(state: GojoState) -> dict:
    """Perform the approved action. Deterministic - no model runs here.

    Everything that executes comes from the ledger row (sha-verified
    approved bytes); this node only translates the outcome into a reply.
    """
    proposal = state["proposal"]
    conn = actions.connection()
    client = actions.write_client()
    if conn is None or client is None:
        return {
            "steps": ["execute"],
            "findings": ["The write path isn't configured. Nothing was done."],
        }
    try:
        result_id = await actions.execute(conn, client, proposal["action_id"])
    except ActionError as exc:
        logger.warning("execute failed action_id=%s: %s", proposal["action_id"], exc)
        return {
            "steps": ["execute"],
            "findings": [f"I couldn't complete that: {exc} Nothing further was changed."],
        }
    op = proposal["payload"]["op"]
    logger.info("executed action_id=%s result_id=%s", proposal["action_id"], result_id)
    text = (
        "Sent."
        if op == "send"
        else "Draft created — it's in your Drafts folder for you to review and send."
    )
    return {"steps": ["execute"], "findings": [text]}


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


def gate_pending(snapshot) -> bool:
    """Whether this thread is paused at the approval gate.

    ⚠ Keyed on snapshot.next AND snapshot.interrupts, never interrupts
    alone: any aupdate_state with values (e.g. /compact) empties
    snapshot.interrupts while the gate stays fully resumable - verified
    against langgraph 1.2.10 (ADR 0011, review M1).
    """
    return "gate" in (snapshot.next or ()) or bool(snapshot.interrupts)


async def resume_gate(graph, decision: str, thread_id: str) -> dict:
    """Resume a paused gate with the owner's decision. Same 9.3 guards.

    Command(resume=...) re-enters the gate node from its start; interrupt()
    then returns `decision` instead of raising.

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
            graph.ainvoke(Command(resume=decision), config),
            timeout=settings.graph_timeout_seconds,
        )
    except TimeoutError as exc:
        logger.error(
            "gate resume timed out after %ss on thread %s",
            settings.graph_timeout_seconds,
            thread_id,
        )
        raise GraphTimeout(
            f"gate resume exceeded {settings.graph_timeout_seconds}s"
        ) from exc


async def resume_gate_locked(graph, decision: str, thread_id: str) -> dict | None:
    """resume_gate under the conversation's lock, with the pending re-check.

    Returns None when no gate is pending - the B1 defence: a double card
    tap, or a resume racing a cancellation, must be refused rather than
    silently replaying the thread's previous final state (which is what
    Command(resume=...) does on a non-paused thread - verified 1.2.10).
    """
    async with lock_for(thread_id):
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        if not gate_pending(snapshot):
            logger.info("gate resume refused - nothing pending on thread %s", thread_id)
            return None
        return await resume_gate(graph, decision, thread_id)


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
    builder.add_node("gate", gate)
    builder.add_node("execute", execute_action)
    builder.add_node("respond", respond)

    builder.add_edge(START, "new_turn")
    builder.add_edge("new_turn", "classify")
    builder.add_conditional_edges(
        "classify",
        route_by_intent,
        {"gather": "megumi", "act": "sukuna", "unknown": "respond"},
    )
    builder.add_edge("megumi", "respond")
    builder.add_conditional_edges(
        "sukuna", route_after_sukuna, {"gate": "gate", "respond": "respond"}
    )
    builder.add_conditional_edges(
        "gate", route_after_gate, {"execute": "execute", "respond": "respond"}
    )
    builder.add_edge("execute", "respond")
    builder.add_edge("respond", END)

    return builder.compile(checkpointer=checkpointer)


async def main() -> None:
    # Gather-path demo only: the act path now pauses at the gate, which
    # needs a checkpointer to hold the interrupt - use /chat or Teams.
    graph = build_graph()
    msg = "what needs my attention today"
    print(f"\n--- {msg} ---")
    result = await graph.ainvoke({"message": msg, "steps": [], "findings": []})
    print("REPLY:", result["reply"])
    print("PATH:", result["steps"])


if __name__ == "__main__":
    asyncio.run(main())

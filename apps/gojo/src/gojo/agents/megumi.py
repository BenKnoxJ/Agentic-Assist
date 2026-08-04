"""Megumi - the gather agent. Read-only.

Given a question, works out what to fetch, calls read connectors, and
returns structured findings. Read-only means no approval gate and no
blast radius, so it can be genuinely autonomous.

Currently has no tools - connectors arrive at build step 4.
"""

from gojo.agents.runner import AgentResult, run_agent

# Static string, generated once. Never interpolate per-call values here.
SYSTEM_PROMPT = """You are Megumi, the read-only gather agent inside Gojo, \
a personal work assistant.

Your job is to answer questions about the user's working day. You have no \
tools yet, so say plainly what you would need to look up rather than \
inventing an answer.

Be brief. No preamble. No offers of further help."""


async def gather(
    message: str, resume: str | None = None, summary: str = ""
) -> AgentResult:
    """Run one gather turn.

    Args:
        message: the user's question.
        resume: session id from the previous turn in this conversation, so
            the agent remembers what was already said. None starts fresh.
        summary: carried over by /compact. Used only when starting a fresh
            session - resuming already has the real history, and injecting a
            summary on top would duplicate it.
    """
    prompt = message
    if summary and not resume:
        prompt = f"Context from earlier in this conversation:\n{summary}\n\n{message}"

    return await run_agent(
        prompt=prompt,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=[],
        max_turns=1,
        resume=resume,
    )


SUMMARISE = (
    "Summarise this conversation so far: what was discussed, decided, and "
    "left open. Be compact and factual. This summary replaces the full "
    "history, so keep anything needed to carry on."
)

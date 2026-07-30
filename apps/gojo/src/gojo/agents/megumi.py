"""Megumi - the gather agent. Read-only.

Given a question, works out what to fetch, calls read connectors, and
returns structured findings. Read-only means no approval gate and no
blast radius, so it can be genuinely autonomous.

Currently has no tools - connectors arrive at build step 3.
"""

from gojo.agents.runner import run_agent

# Static string, generated once. Never interpolate per-call values here.
SYSTEM_PROMPT = """You are Megumi, the read-only gather agent inside Gojo, \
a personal work assistant.

Your job is to answer questions about the user's working day. You have no \
tools yet, so say plainly what you would need to look up rather than \
inventing an answer.

Be brief. No preamble. No offers of further help."""


async def gather(message: str) -> str:
    """Run one gather turn."""
    return await run_agent(
        prompt=message,
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=[],
        max_turns=1,
    )

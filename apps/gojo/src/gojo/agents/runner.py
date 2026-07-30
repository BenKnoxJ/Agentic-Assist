"""Single point of contact with the Claude Agent SDK.

Every agent call in Gojo goes through run_agent(). Nothing else imports
claude_agent_sdk directly. This is what makes nodes testable: tests inject
a fake runner instead of spawning a real subprocess.
"""

from collections.abc import Callable, Sequence

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

from gojo.config import get_settings

AgentRunner = Callable[..., "object"]


async def run_agent(
    prompt: str,
    system_prompt: str = "",
    allowed_tools: Sequence[str] | None = None,
    max_turns: int | None = None,
) -> str:
    """Run one agent turn and return its text output.

    Args:
        prompt: the task for the agent.
        system_prompt: static instructions. Must be a constant string -
            per-call values bust the prompt cache (GOJO-MASTER.md 6.3 rule 1).
        allowed_tools: tool names the agent may use. Empty means none.
        max_turns: cap on agentic loop iterations. Budget guard.
    """
    settings = get_settings()

    options = ClaudeAgentOptions(
        system_prompt=system_prompt or None,
        allowed_tools=list(allowed_tools or []),
        max_turns=max_turns or settings.max_turns_per_agent,
    )

    parts: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)

    return "\n".join(parts).strip()

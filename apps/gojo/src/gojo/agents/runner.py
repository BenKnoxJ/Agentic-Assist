"""Single point of contact with the Claude Agent SDK.

Every agent call in Gojo goes through run_agent(). Nothing else imports
claude_agent_sdk directly. This is what makes nodes testable: tests inject
a fake runner instead of spawning a real subprocess.

Uses ClaudeSDKClient rather than query(): 6.2 specifies the client "for the
full agentic loop with tools, sessions and manual control", and query() has
no session support at all. Sessions are what make a conversation continue
rather than restart on every message.

⚠ The SDK owns the transcript, not LangGraph. run_agent returns a session id
and the graph stores only that - never the messages themselves (6.3 rule 3,
unbounded state growth is the named failure mode of this pattern).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from gojo.config import get_settings

AgentRunner = Callable[..., "object"]


@dataclass(frozen=True)
class AgentResult:
    """What one agent turn produced.

    session_id is the handle for continuing this conversation next time.
    cost_usd and num_turns come back from the SDK and are the only spend
    signal that crosses the subprocess boundary - LangSmith does not see
    inside it (9.2), so this is worth carrying even before it is used.
    """

    text: str
    session_id: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None


async def run_agent(
    prompt: str,
    system_prompt: str = "",
    allowed_tools: Sequence[str] | None = None,
    max_turns: int | None = None,
    resume: str | None = None,
) -> AgentResult:
    """Run one agent turn.

    Args:
        prompt: the task for the agent.
        system_prompt: static instructions. Must be a constant string -
            per-call values bust the prompt cache (GOJO-MASTER.md 6.3 rule 1).
        allowed_tools: tool names the agent may use. Empty means none.
        max_turns: cap on agentic loop iterations. Budget guard.
        resume: a session id from a previous AgentResult. When given, the
            agent continues that conversation instead of starting fresh.
    """
    settings = get_settings()

    options = ClaudeAgentOptions(
        system_prompt=system_prompt or None,
        allowed_tools=list(allowed_tools or []),
        max_turns=max_turns or settings.max_turns_per_agent,
        # Agents get only the context passed in above. No CLAUDE.md, settings,
        # or slash commands are inherited from the host Claude Code environment
        # - not the user's, not the project's, not the local ones.
        setting_sources=[],
        resume=resume,
    )

    parts: list[str] = []
    result: ResultMessage | None = None

    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(message, ResultMessage):
                result = message
    finally:
        # Always disconnect: the client owns a CLI subprocess, and leaking one
        # per turn would exhaust the single core this box has (3.1).
        await client.disconnect()

    return AgentResult(
        text="\n".join(parts).strip(),
        session_id=result.session_id if result else None,
        cost_usd=result.total_cost_usd if result else None,
        num_turns=result.num_turns if result else None,
    )

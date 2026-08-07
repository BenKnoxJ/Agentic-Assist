"""Single point of contact with the Claude Agent SDK's agent loop.

Every agent call in Gojo goes through run_agent(). Nothing else runs the
agent loop (ClaudeSDKClient) directly - tools.py defines in-process MCP
tools, but execution always comes through here. This is what makes nodes
testable: tests inject a fake runner instead of spawning a real subprocess.

Uses ClaudeSDKClient rather than query(): 6.2 specifies the client "for the
full agentic loop with tools, sessions and manual control", and query() has
no session support at all. Sessions are what make a conversation continue
rather than restart on every message.

⚠ The SDK owns the transcript, not LangGraph. run_agent returns a session id
and the graph stores only that - never the messages themselves (6.3 rule 3,
unbounded state growth is the named failure mode of this pattern).
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)
from langsmith import traceable

from gojo.config import get_settings

logger = logging.getLogger(__name__)

AgentRunner = Callable[..., "object"]

# The CLI subprocess ships Claude Code's full built-in toolset. Absence from
# allowed_tools leaves them denied only by the SDK's permission default - a
# behaviour, not a configuration. This list is the explicit denial the
# threat model cites: if that default ever loosens, these stay off. Bash and
# the file tools could read the credentials in $HOME; WebFetch would be the
# exfiltration channel for a prompt injected via mail (THREAT-MODEL.md).
DISALLOWED_BUILTIN_TOOLS = [
    "Task",
    "Bash",
    "BashOutput",
    "KillShell",
    "Glob",
    "Grep",
    "Read",
    "Edit",
    "Write",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "SlashCommand",
    "ExitPlanMode",
]


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
    mcp_servers: dict | None = None,
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
        mcp_servers: in-process MCP servers by name (tools.py builds them).
            Empty means the agent gets no tools at all.
    """
    settings = get_settings()

    options = ClaudeAgentOptions(
        system_prompt=system_prompt or None,
        allowed_tools=list(allowed_tools or []),
        # Explicit denial of the subprocess's built-ins, on every call,
        # regardless of what the caller allowed. See DISALLOWED_BUILTIN_TOOLS.
        disallowed_tools=list(DISALLOWED_BUILTIN_TOOLS),
        max_turns=max_turns or settings.max_turns_per_agent,
        # Agents get only the context passed in above. No CLAUDE.md, settings,
        # or slash commands are inherited from the host Claude Code environment
        # - not the user's, not the project's, not the local ones.
        setting_sources=[],
        mcp_servers=dict(mcp_servers or {}),
        # Same isolation intent as setting_sources: no MCP server config can
        # arrive from the filesystem, only from the dict above.
        strict_mcp_config=True,
        resume=resume,
    )

    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        exchange = await _traced_sdk_exchange(
            prompt,
            client=client,
            resume=resume,
            allowed_tools=list(allowed_tools or []),
        )
    finally:
        # Always disconnect: the client owns a CLI subprocess, and leaking one
        # per turn would exhaust the single core this box has (3.1).
        await client.disconnect()

    if not exchange["text"]:
        # An empty answer is always a defect somewhere - Teams rejects empty
        # messages, and respond substitutes a placeholder the user then reads.
        # The subtype is the SDK's own reason (e.g. error_max_turns), and it
        # crosses the subprocess boundary nowhere else (9.2).
        logger.warning(
            "agent returned no text: subtype=%s num_turns=%s",
            exchange["subtype"],
            exchange["num_turns"],
        )

    return AgentResult(
        text=exchange["text"],
        session_id=exchange["session_id"],
        cost_usd=exchange["cost_usd"],
        num_turns=exchange["num_turns"],
    )


def _drop_client(inputs: dict) -> dict:
    return {key: value for key, value in inputs.items() if key != "client"}


@traceable(name="claude-agent-sdk", run_type="chain", process_inputs=_drop_client)
async def _traced_sdk_exchange(
    prompt: str,
    *,
    client: ClaudeSDKClient,
    resume: str | None,
    allowed_tools: list[str],
) -> dict:
    """One prompt/response exchange with the SDK, as a LangSmith span.

    The agent's reasoning runs in a subprocess LangSmith cannot see into
    (9.2), so this span carries what does cross back - text, cost, turns,
    session id, subtype - and makes the megumi/sukuna nodes show a child
    with spend data instead of nothing (13.8). With tracing off, @traceable
    is a no-op. resume and allowed_tools are inputs only so a trace shows
    what shaped the call; the client object is dropped from capture.
    """
    parts: list[str] = []
    result: ResultMessage | None = None

    await client.query(prompt)
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
        elif isinstance(message, ResultMessage):
            result = message

    return {
        "text": "\n".join(parts).strip(),
        "session_id": result.session_id if result else None,
        "cost_usd": result.total_cost_usd if result else None,
        "num_turns": result.num_turns if result else None,
        "subtype": getattr(result, "subtype", None),
    }

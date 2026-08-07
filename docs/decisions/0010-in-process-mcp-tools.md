# ADR 0010 — In-process SDK MCP servers as the agent tool mechanism

**Status:** Accepted
**Date:** 2026-08-07

## Context
Step 4 gives Megumi its first real tools: read-only Graph mail and Jira.
Something has to carry those Python functions across to the agent, and the
candidates were:

- **LangGraph tool binding** (`bind_tools`, `ToolNode`) — already forbidden.
  §6.2: it requires a LangChain chat model authenticating with
  `ANTHROPIC_API_KEY`, and it duplicates the agent loop the SDK owns.
- **External MCP server processes** — §8.1 already rejects this at one
  user's scale: another process to secure and monitor on one physical core,
  for two tools.
- **First-party remote MCP** (Atlassian Rovo) — §8.1 sanctions it precisely
  because the upstream credential never lands on this box. Considered
  seriously for Jira; see Consequences for why not now.
- **SDK in-process MCP servers** (`create_sdk_mcp_server` + `@tool`) — the
  same MCP tool protocol, served from inside our process, no subprocess, no
  IPC, tools are plain async functions with direct access to our config.

## Decision
**Tools reach agents via in-process SDK MCP servers, one server per agent
role.** The `gather` server carries both read tools
(`mcp__gather__list_recent_mail`, `mcp__gather__search_issues`) and is
handed only to Megumi. Step 5's write tools will be a separate server,
handed only to Sukuna behind the gate — the server boundary tracks the
read/write boundary (§7.2), not the vendor boundary.

Three wiring rules travel with the mechanism, all enforced in `runner.py`
and pinned by tests:

- **`strict_mcp_config=True` on every call.** No MCP server configuration
  can arrive from the filesystem — the same isolation intent as
  `setting_sources=[]` (ADR 0004's environment wall, finished).
- **Built-in tools are explicitly denied.** The CLI subprocess ships Claude
  Code's full toolset (Bash, Read/Write/Edit, WebFetch, WebSearch...).
  Absence from `allowed_tools` leaves them denied only by the SDK's
  permission default — a behaviour, not a configuration. `disallowed_tools`
  names them all, every call, so a future loosening of that default changes
  nothing here. THREAT-MODEL.md carries the argument; `test_runner_options.py`
  pins the wiring.
- **Static definitions only.** Names, descriptions and schemas are built
  once at import (§6.3 rule 1). A per-call value in a tool description
  busts the prompt cache on every turn.

Connectors themselves stay SDK-free (`libs/connectors/*`, plain httpx —
§8.1); only `agents/tools.py` and `agents/runner.py` import
`claude_agent_sdk`. The runner docstring rule is amended accordingly:
nothing else *runs the agent loop* directly.

## Rationale
The in-process server keeps one process on one core (§3.1), needs no new
trust boundary between Gojo and its own tools, and leaves tool execution
inside our logging and test seams. The per-role server split means an
agent's tool list *is* its capability list — the approval gate at step 5
gates a server, not a convention.

## Consequences
- **Jira authenticates with a stored API token, not Rovo MCP.** §8.1's
  argument for first-party MCP is real (no stored upstream credential), and
  this decision goes the other way for v1: Rovo's remote OAuth flow is a
  second auth dance and a network dependency for what is one read-only
  endpoint here, and the token is delegated — its blast radius is the
  owner's own Jira permissions, revocable at id.atlassian.com in one click.
  Revisit if Jira write scopes ever arrive.
- **MSAL's token cache stays in-memory**, not §8.3's encrypted
  `SerializableTokenCache`. One always-on process re-mints a
  client-credentials token in a single network call; an on-disk cache adds
  a secret at rest for zero benefit. §8.3 is amended to point here.
- **Recovery replay is safely idempotent — today.** ADR 0008 re-runs an
  interrupted turn's agent call at startup, which now re-runs tool calls.
  Both tools are read-only, so a replay costs latency and tokens, nothing
  else. **This property dissolves at step 5**: a write tool behind a replay
  needs idempotency keys before it exists (§9.3 failure 2). Recorded here
  so step 5 inherits the obligation explicitly.
- **Tool results are data, not instructions.** Everything a connector
  returns enters the model wrapped in `<external-data>` markers with the
  closing tag stripped from payloads; `/compact` summaries re-enter under
  the same wrapper, and summarisation itself runs tool-free. Mitigation,
  not proof — THREAT-MODEL.md owns the full argument.
- **The gather server is spun up per agent call** by the SDK. At one user
  this is noise; if per-call setup ever shows up in latency traces, measure
  before optimising.

## Related
- §6.2, §6.3, §7.2, §8.1, §8.5 — the constraints this mechanism satisfies.
- ADR 0008 — recovery replay, whose idempotency this ADR now carries.
- THREAT-MODEL.md — the trust boundaries this mechanism participates in.

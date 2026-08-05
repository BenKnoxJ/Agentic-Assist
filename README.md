# Agentic-Assist

A private Microsoft Teams assistant that reads across the systems a working
day is spread over, works out what matters, and answers — or does something
about it, after asking.

You message it like a colleague. It replies in the same chat, remembers the
conversation, and holds anything that would *change* something behind an
approval gate.

> **Status:** build step 3 of 6 complete. It runs as a service, answers from
> Teams, and remembers conversations across restarts. It has no connectors
> yet, so it will tell you plainly that it cannot see your mail — that is
> step 4. See [Where this is](#where-this-is).

---

## The problem

A working day is spread across eleven systems — mail, SharePoint, OneDrive,
Teams, Jira, Confluence, two Zoho products, Clerk, Vercel, GitHub. Nobody
holds all of that in their head. The information exists; **assembling it is
the work.**

The test this project is measured against is one sentence:

> Send **"what needs my attention today"** in Teams, and get back a
> prioritised, actionable answer drawn from real systems.

If a feature does not move toward that, it is out of scope. That rule has
already killed more work than it has authorised.

## Architecture

```
Teams message
     ↓
Azure Bot Service  ──POST──▶  FastAPI /api/messages   (JWT validated, single tenant)
                                     ↓
                        LangGraph orchestrator         ← state machine, not an agent
                          ├── classify (deterministic)
                          ├── Megumi — gather agent    ← read-only
                          ├── Sukuna — act agent       ← gated, writes only
                          └── respond
                                     ↓
                        Claude Agent SDK               ← the agent loop
                                     ↓
                        SQLite checkpointer            ← survives restarts
```

**The division that matters most:**

| Layer | Owns |
|---|---|
| **LangGraph** | Control flow. What runs, in what order, when to stop and ask |
| **Claude Agent SDK** | Execution. Given a scoped job, do it |
| **Connectors** | Fetching and acting. They never reason |

### Why not `create_react_agent`

LangGraph ships a prebuilt ReAct agent — `create_react_agent`, `ToolNode`,
`bind_tools` — and most tutorials start there. This project deliberately
does not, for two reasons.

It requires a **LangChain chat model**, which authenticates with
`ANTHROPIC_API_KEY`. That key sits *above* the subscription token in
Claude's credential precedence chain, so adopting the prebuilt pattern would
have silently moved every inference call onto pay-as-you-go API billing
while appearing to work normally. The application asserts that key is unset
at boot and refuses to start otherwise.

It also duplicates the agent loop. The Claude Agent SDK already provides
tool use, context management and the loop itself; running LangGraph's on top
would mean two agent loops, one of them redundant.

So **tools are given to the Agent SDK, not bound to LangGraph.** LangGraph
is kept deliberately narrow — routing, checkpointing, interrupts, recursion
limits — which is the whole of what it was adopted for.

### Agent boundaries

Agents are split by **context boundary, not by system**. One agent per API
would be a connector taxonomy wearing an agent costume — and every hop
multiplies the failure rate, so a boundary has to buy something. The
boundary that earns its place is **read versus write**, because that is what
the approval gate, the permission scopes, idempotency and blast radius all
line up behind.

## Decisions, including the reversed ones

Every significant choice is an [ADR](docs/decisions/), recorded with the
reasoning at the time — **including the ones that were later overturned.**
The reversals are the point: they are the evidence of judgement.

| ADR | Decision |
|---|---|
| [0001](docs/decisions/0001-runtime-choice.md) | Bun runtime — **superseded** |
| [0002](docs/decisions/0002-monorepo-structure.md) | Single monorepo |
| [0003](docs/decisions/0003-secrets-handling.md) | Secrets never enter git |
| [0004](docs/decisions/0004-python-runtime.md) | Python 3.12 supersedes Bun |
| [0005](docs/decisions/0005-event-loop-pinning.md) | Pin asyncio; uvloop breaks the Agent SDK |
| [0006](docs/decisions/0006-async-teams-replies.md) | Two-part replies inside the channel timeout |
| [0007](docs/decisions/0007-persistence-before-connectors.md) | Persistence before connectors |
| [0008](docs/decisions/0008-in-flight-turn-resumption.md) | Resume in-flight turns after a restart |

Two are worth reading for what they say about verifying rather than
assuming. **ADR 0005** records that every Agent SDK call fails
deterministically under uvloop — measured 3/3 against 3/3 — while honestly
stating that the root cause is inferred, not established. **ADR 0007**
reverses the build order because using the system revealed a gap that
reading the plan had not.

## Constraints that shaped it

**One physical core.** The host is 2 vCPU on a single hyperthreaded core.
That single fact explains the single Uvicorn worker, the absence of Docker,
queued rather than parallel agent execution, and the decision to use hosted
embeddings when retrieval eventually lands.

**A 10–15 second channel timeout.** Azure Bot Service returns `504` to the
user if the bot does not respond in time. Agent turns will exceed that once
connectors exist, so every turn is answered in two parts: a typing indicator
and a fast reply if it fits the budget, otherwise an acknowledgement and a
proactive message when the work completes.

**One user, one tenant.** Not a product. No multi-user, no billing, no
public rate limiting. Access is a two-check allow-list: a validated
single-tenant JWT proves the message came from Azure Bot Service, and an
Entra object ID check proves *who sent it*. Both must pass, and an unset
allow-list denies everyone rather than everyone.

## Deliberately not built

- **Retrieval and a vector database.** Committed and sequenced as step 6,
  not dropped. The corpus does not exist yet, and chunking decisions made
  before the corpus exists are guesses.
- **Nine of the eleven connectors.** Added when a workflow actually needs
  one.
- **Anything past the six production properties** — survives restart, runs
  as a service, every turn traceable, failures contained, secrets handled,
  destructive actions gated. Deliberately no more, to stop scope creep
  dressed as rigour.

## Where this is

| # | Step | |
|---|---|---|
| 1 | Scaffold, graph, model in the loop | ✅ |
| 2 | Teams surface, JWT, allow-list | ✅ |
| 3 | Persistence, sessions, systemd, session commands | ✅ |
| 4 | Two read connectors — Graph mail, Jira | — |
| 5 | Approval gates and write scopes | — |
| 6 | Memory and retrieval | — |

Working today: continuity across restarts, `/new` `/compact` `/help`, a
wall-clock timeout and agent budget, per-turn correlation ids and cost
figures in the journal.

Known gaps, stated rather than hidden: agent *reasoning* is invisible in
LangSmith because the Agent SDK runs in a subprocess, so orchestration is
traced and thinking is not. Session replay makes a long conversation cost
several times a short one. And a turn acknowledged but not yet answered does
not survive a restart — the acknowledgement is sent, the reply is not. That
one is designed and specified in **ADR 0008** and not yet built; it went
unrecorded outside ADR 0006 for two sessions, which is itself the argument
for the ownership rule the master document now carries.

## Running the tests

```
uv sync
uv run pytest -q
```

46 tests, no network and no inference spend — every one swaps the Agent SDK
at a single injectable seam. That seam exists precisely so the graph can be
tested end to end in seconds without spawning a subprocess.

## Layout

```
apps/gojo/src/gojo/     orchestrator, Teams surface, agents, commands
apps/gojo/tests/        the suite
docs/GOJO-MASTER.md     architecture and build sequence — the source of truth
docs/decisions/         ADRs
docs/build-log.md       what was done, when, and what broke
infra/                  systemd unit, Teams app package
```

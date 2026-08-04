# Gojo — Master Document

**Version:** 3.1 — build in progress
**Date:** 4 August 2026
**Owner:** Ben Knox (GitHub: `BenKnoxJ`)
**Repo:** `BenKnoxJ/Agentic-Assist` (private monorepo)
**Package:** `gojo` (repo carries the portfolio-facing name; the Python package stays `gojo`)

> **This document supersedes v1 and v2.** It is the single source of truth. Where it conflicts with an ADR, a code comment, or an earlier document, **this wins** — see §9.2 for known supersessions.
>
> **What changed in v3:** scope cut to a shippable v1. The retrieval layer (Postgres, pgvector, embeddings, reranking, evals) is researched and documented but **deliberately not built yet** — see §8. Agent topology settled. Four corrections applied from external review.

---

# PART ONE — THE IDEA

## 1. What Gojo is

A private Microsoft Teams bot that acts as the front door to a small team of AI agents working across your real business systems. You message it like a colleague; it reads your mail, checks your tickets, works out what matters, and tells you — or does something about it, after asking.

Named after the Jujutsu Kaisen character.

### 1.1 The one-sentence test

You send **"what needs my attention today"** in Teams. Gojo reads your mail, cross-references your tickets, and replies with a prioritised, actionable answer.

If a feature doesn't move toward that, it's out of scope. This test is the arbiter for every "should we build X" question.

### 1.2 Why build it

**Because your working day is spread across eleven systems.** Mail, SharePoint, OneDrive, Teams, Jira, Confluence, two Zoho products, Clerk, Vercel, GitHub. Nobody holds all of that in their head. The information exists; assembling it is the work. Gojo does the assembling.

**Because it's the strongest possible portfolio artefact.** Multi-agent orchestration, production infrastructure, real enterprise integrations, human-in-the-loop safety, observability. That's the actual job description for AI engineering roles, demonstrated rather than claimed.

These two goals mostly agree. Where they conflict: **lean and agents-first**. Complexity gets added deliberately, never speculatively. The commercial goal sets direction; the career goal sets the quality bar.

**A note on career-driven choices.** A well-justified simple choice beats a poorly-justified fashionable one. "I benchmarked X against Y and chose X because of Z at my scale" is a better interview answer than "I used Y because it's on job specs." Out-of-stack skill gaps — Qdrant, CrewAI, Kubernetes — go in **satellite repos**, never bent into Gojo's architecture to chase a keyword.

### 1.3 What Gojo is not

- **Not a chatbot with a nice UI.** If it can't take real actions in real systems, it has failed.
- **Not a product.** One user, one tenant. No multi-user, no billing, no onboarding, no public rate limiting.
- **Not a search engine over your documents.** Not yet — see §8 for why that's deferred rather than dropped.
- **Not a monolith.** The orchestrator/agent split exists so adding a system doesn't mean rewriting the thing that reads mail.

## 2. The concept in one paragraph

A Teams message arrives at a Python service on your Azure VPS. A **LangGraph orchestrator** — a state machine you own — classifies what's being asked and routes it. Read-only questions go to a **gather agent** that calls your systems' APIs and returns findings. Anything that would *change* something goes through an **approval gate** first, and only executes after you say yes in Teams. State is checkpointed to disk, so a reboot mid-conversation doesn't lose the thread. systemd keeps the process alive. LangSmith records every turn so you can see what it did and why.

That's the whole system. Everything in Part Two exists to deliver that paragraph.

---

# PART TWO — THE STACK

Each layer below states **what it is**, **why it's chosen**, and **what it replaced**, so you can defend every choice.

## 3. Infrastructure

### 3.1 Host — Azure VPS, Ubuntu 24.04

Already running. Specs measured 27 July 2026:

| Resource | Value |
|---|---|
| CPU | **2 vCPU = 1 physical core**, hyperthreaded. Xeon Platinum 8370C @ 2.80GHz |
| RAM | 7.7 GiB total, ~7.1 GiB available |
| Swap | 4.0 GiB, unused |
| Disk | 61 GB, 54 GB free |

**One physical core is the constraint that shapes everything.** RAM is comfortable. CPU is not. This is why agent execution is queued rather than parallel, why there's no Docker, and why (when you get there) embeddings and reranking will be hosted rather than local.

**Do not upgrade yet.** Nothing in v1 gets close to constrained. Azure resizing takes minutes and a reboot — it's a reversible decision you can make when monitoring gives you evidence, not before.

### 3.2 TLS and reverse proxy — Caddy

**What it is:** a web server that sits in front of your app, terminates HTTPS, and forwards plain HTTP to `localhost:3000`.

**Why:** it obtains and auto-renews Let's Encrypt certificates with no configuration. Already working, cert issued for `cc-sfq4tema454pu.uksouth.cloudapp.azure.com`.

*(Note: it's `caddy version`, no dashes. `caddy --version` errors.)*

### 3.3 Process management — systemd

**What it is:** Linux's service manager. It starts your app on boot, restarts it if it crashes, and captures its logs.

**Why:** two of your six production-grade properties — "survives restart" and "runs as a service" — are literally this. There's no alternative worth considering on a single VPS.

**Configuration:** `Restart=always`, `RestartSec=5`, `StartLimitBurst` to prevent crash-loop hammering, `MemoryMax` at 5–6 GiB initially (verify empirically), `StandardOutput=journal`.

## 4. Application

### 4.1 Language — Python 3.12

**Why:** deepest ecosystem for AI engineering work, LangGraph's primary implementation, strongest career signal for the roles you're targeting, and already installed.

**Replaced:** TypeScript/Bun (ADR 0001). Your existing application code was ~one file, so the migration cost was effectively zero.

**Gotcha:** Ubuntu 24.04 enforces PEP 668. System-wide `pip install` is blocked. This is why uv matters.

### 4.2 Package and environment management — uv

**What it is:** a fast Python package manager and virtual-environment tool that replaces pip, venv, and Poetry.

**Why:** solves PEP 668 cleanly, and gives systemd a stable interpreter path (`.venv/bin/python`) that survives OS updates. Workspace mode handles the monorepo without publishing packages.

### 4.3 Web framework — FastAPI, single Uvicorn worker

**What it is:** FastAPI defines your HTTP endpoints; Uvicorn is the server that runs them.

**Why FastAPI:** ecosystem gravity. Every LangGraph and Agents SDK example assumes it. Pydantic validation comes free.

**Why one worker:** one physical core, and the workload is I/O-bound (waiting on APIs and LLMs, not computing). Multiple workers would contend for the same core and fragment your in-memory state.

### 4.4 Testing — pytest

LangGraph's own testing documentation assumes it.

## 5. The Teams surface

### 5.1 Microsoft 365 Agents SDK for Python

**What it is:** Microsoft's current library for building bots. It handles the Bot Framework protocol — receiving Activities from Teams, validating they're genuinely from Microsoft, and sending replies back.

**Why:** the previous library, Bot Framework SDK (`botbuilder-python`), is **archived — support ended 31 December 2025**. This is the only current path. It also has a Python-native FastAPI hosting package that doesn't exist for other languages.

**Packages:**
```
microsoft-agents-hosting-core
microsoft-agents-hosting-fastapi
microsoft-agents-hosting-teams
microsoft-agents-authentication-msal
```

**⚠ Version:** earlier research reported v0.9.0 as current. **That was stale.** The SDK is past 1.0 — `microsoft-agents-hosting-core` was verified at 1.1.0 on PyPI, and later releases may exist. **Check PyPI for the actual current version before pinning.** Do not pin 0.9.0.

### 5.2 Why not hand-roll JWT validation

You *could* write the endpoint directly against the protocol — it's authenticated JSON over HTTPS. Don't. The SDK's middleware does it correctly, and hand-rolled validators routinely skip `iss` and `aud` checks, which is the difference between validating a token and validating *the right* token.

**Single-tenant configuration** is the most common failure point: set `MicrosoftAppType: SingleTenant` **and** `MicrosoftAppTenantId` explicitly.

## 6. Orchestration and execution

This is the heart of the system, and the distinction below matters more than any other in this document.

### 6.1 LangGraph — the orchestrator

**What it is:** a Python library for building state machines. You define **nodes** (functions that do something) and **edges** (rules for which node runs next). It holds **state** that flows between nodes, and a **checkpointer** that saves that state to disk after every step.

**It is not an agent.** It makes no decisions of its own. It's the scaffolding that decides which agent gets called, what happens with the answer, and when to stop and ask you.

**Why it earns its place** — two things you'd otherwise hand-write:

- **Checkpointing** → production property 1 (survives restart). Free.
- **`interrupt()`** → production property 6 (destructive actions gated). Free.

That's roughly 300 lines you'd own forever, plus it's a named skill on job specs. Keep it deliberately narrow: routing, checkpointing, interrupts, recursion limit. Nothing else.

**Version pins — mandatory, security:**
```
langgraph>=1.0.10
langgraph-checkpoint-sqlite>=3.0.1
langchain-core>=1.2.22
```

There's a disclosed SQL-injection-to-RCE chain in older checkpointers (CVE-2025-67644, CVE-2026-28277) plus a path traversal in `langchain-core` (CVE-2026-34070). Current versions clear these; **pin explicitly in the lockfile** so a future resolution can't slide underneath. The realistic attack vector once connectors exist is a malicious calendar invite or email processed by an agent — which is exactly this architecture.

**Checkpointer: SQLite.** Not Postgres, not Redis. At one user's write volume SQLite is lower overhead, needs no extra process, and is a single file to back up.

### 6.2 Claude Agent SDK — the execution layer

**What it is:** Anthropic's Python/TypeScript library exposing the same agent loop, tools and context management that power Claude Code, callable from your own code.

**Package:** `claude-agent-sdk` (Python 3.10+). Bundles the Claude Code CLI — no separate install needed.

**Why:** a mature agent loop you don't have to write, and it runs on your existing Claude Max subscription.

#### Auth — settled, do not reopen

Gojo authenticates through the owner's **Claude Max subscription** via Claude Code's stored credentials. This is a fixed constraint of the project, not a trade-off to be re-evaluated. Any session that proposes API-key authentication has misread this document.

**⚠ Precedence chain — the operational hazard:**

```
cloud creds → ANTHROPIC_AUTH_TOKEN → ANTHROPIC_API_KEY
            → apiKeyHelper → CLAUDE_CODE_OAUTH_TOKEN
```

`ANTHROPIC_API_KEY` sits **above** the subscription token. Setting it anywhere — `.env`, shell profile, systemd unit, CI — silently redirects all inference to pay-as-you-go API billing while appearing to work normally.

**Rules:**
- Never set `ANTHROPIC_API_KEY` in this project
- Never add a config field for it — a field that doesn't exist can't be populated by accident
- Assert it is unset at startup and fail loudly if present

**Interactive vs service auth.** Under an interactive shell the SDK uses Claude Code's stored login. Under systemd (build step 4), generate a one-year token with `claude setup-token` and set it as `CLAUDE_CODE_OAUTH_TOKEN`.

**Two APIs:** `query()` for single-shot generation without tools; `ClaudeSDKClient` for the full agentic loop with tools, sessions and manual control. Megumi and Sukuna use the latter.

**How it composes with LangGraph:** a LangGraph node is a plain Python function. Inside it, you `await` an Agent SDK call and put the result into graph state. That's the whole integration.

**Division of responsibility — the thing to internalise:**

> **LangGraph owns control flow.** What runs, in what order, with what state, when to stop and ask.
> **Agent SDK owns execution.** Given a scoped job, do it.
> **Connectors are thin.** They fetch and act. They never reason.

If you find yourself putting cleverness into a LangGraph node, it belongs in an agent instead.

### 6.3 Three mandatory build rules

1. **Tool descriptions and schemas must be static strings, generated once at process start.** Any per-call unique value (UUID, timestamp, session ID) in a tool description busts the entire prompt cache on every call. There's a live bug doing exactly this in Anthropic's own SDK.
2. **Wrap every Agent SDK call in a single injectable function per node.** The SDK is too young to have mocking tooling; this is the only way to make nodes testable.
3. **Never push full agent transcripts into LangGraph state.** Summarise. Unbounded state growth is the named failure mode of this pattern.

### 6.4 Billing

Gojo's inference runs on the owner's Claude Max subscription through the Agent SDK. **Settled.** See §6.2 for the auth mechanism and the precedence hazard.

Cost controls: `max_turns` per agent invocation, prompt caching on stable context, cheapest model for routing nodes.

*Note: Anthropic's Agent SDK docs bar third-party developers from offering claude.ai login in products distributed to other users. Gojo is single-user on the owner's own subscription — not applicable. Would apply if handed to colleagues.*

## 7. The agent topology

### 7.1 The principle

Split agents by **context boundary**, not by system. One agent per API is a connector taxonomy wearing an agent costume — your connectors are already thin wrappers, so wrapping each in an agent adds a hop that decides nothing.

And hops compound: three agents at 70% reliability each gives 34% end to end. Every boundary multiplies your failure rate. Split only where the split buys something.

### 7.2 The boundary that earns its place: read vs write

That single line maps onto four things you're already building:

- The approval gate (production property 6)
- Graph permission scopes, staged across build steps 3 and 5
- Idempotency keys — only writes need them
- Blast radius if something goes wrong

"Mail vs Jira" maps onto nothing except which URL gets called.

### 7.3 v1 topology

```
Teams message
     ↓
Orchestrator (LangGraph)          ← not an agent. Deterministic router
     ├─────────────────┐
     ↓                 ↓
Gather agent      Approval gate (interrupt)
(read-only)            ↓
     ↓            Act agent (writes)
Read connectors        ↓
                  Write connectors
```

**Orchestrator** — classify intent, pick a path, hold state, enforce the gate. Keep deterministic where possible. If you need an LLM for routing, use the cheapest model with a fixed set of labels. Unpredictable control flow is untestable control flow.

**Gather agent** — where nearly all v1 value lives. Given a question, works out what to fetch, calls read connectors, returns structured findings. Read-only means no gate and no blast radius, so it can be genuinely autonomous.

**Act agent** — composes and executes changes. Only ever runs behind `interrupt()`. The only thing needing idempotency keys.

**Response formatting is a node, not an agent.** No tools, no loop, one LLM call. Don't dignify it with an agent boundary.

**Naming (JJK theme — mapping is recorded so it isn't obfuscation):**

| Component | Name | Why |
|---|---|---|
| Orchestrator | **Gojo** | Six Eyes: perception and dispatch, doesn't do the work itself |
| Gather agent | **Megumi** | Ten Shadows: summons and sends out scouts, returns with findings |
| Act agent | **Sukuna** | Sealed by default, acts only when explicitly released. The gate *is* the seal |

Module docstrings carry the same mapping so a fresh session opening `sukuna.py` knows immediately it is the gated write path.

### 7.4 When to add the third

Concrete triggers, not vibes:

- **A tool list past ~15–20 tools** — context bloat degrades tool selection. This is the real reason to split.
- **Conflicting instructions** — when the system prompt needs to say "be terse about tickets" and "be thorough about email" in the same breath.
- **A genuinely different reasoning mode** — iterative research is a different shape from a fetch-and-return lookup.

When it happens, split the **gather** agent by domain. The act agent almost never needs splitting — writes are simple and the gate is identical regardless of target.

## 8. Connectors

### 8.1 The rule

**Plain Python with `httpx` by default.** At one user, a 50-line wrapper beats standing up, securing and monitoring an MCP server process.

**Use official MCP only where a vendor has already hardened an OAuth flow** — GitHub, Vercel, Atlassian Rovo. Those are first-party, actively maintained, and mean you never store the upstream credential.

**Never use:** the Zoho MCP server (a third-party lead-magnet for a paid product, read-only) or the "official" Clerk MCP server (the claim traces to a third-party gateway's catalog, not clerk.com).

### 8.2 v1 connectors — two, not eleven

| Connector | Scope at build |
|---|---|
| **Microsoft Graph — mail** | Read-only at step 3. Write scopes at step 5 |
| **Jira** | Read-only at step 3 |

The other nine are a **backlog**, added when a workflow you actually run needs one. Eleven connectors specified up front is the opposite of "adding more as we go."

### 8.3 Microsoft Graph — permissions

**Application permissions, not delegated.** Reason: the proactive morning digest fires at 07:00 with nobody present. There's no supported way to mint a delegated token with zero user interaction — ROPC is discouraged and breaks with MFA, and persisted refresh tokens decay on password change, admin revocation, policy change, or 90 days' inactivity.

**Library:** MSAL Python, `ConfidentialClientApplication`, `acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])`. Encrypted `SerializableTokenCache`, loaded at start, re-serialised after acquisition.

**Note:** in app-only mode `/me/messages` is invalid. Use `GET /users/{ownerUPN}/messages`.

**Scope staging — do not grant everything at step 3:**

| Build step | Scopes |
|---|---|
| Step 3 | `Mail.Read` only |
| Step 5 | `Mail.Send`, `Mail.ReadWrite` — once `interrupt()` and idempotency keys exist |

Granting send capability two steps before the approval gate exists, during the phase with the most crashes and restarts, is how you accidentally email a client at 2am.

### 8.4 ⚠ Blast radius — mandatory mitigations

**Application permissions on Graph are tenant-wide.** `Mail.Read` reads *every mailbox in `conversant.technology`*, not just yours. `Files.Read.All` reads every OneDrive. `Sites.ReadWrite.All` covers every SharePoint site.

You're a Global Admin, so you *can* consent to this. That's exactly why it's dangerous. A leaked client secret otherwise reads your entire employer's correspondence.

**Both of these are mandatory and simultaneous with the grant, not follow-up tasks:**

1. **`New-ApplicationAccessPolicy`** (Exchange Online PowerShell) scoping the app registration to your mailbox only.
2. **`Sites.Selected`** instead of `Sites.ReadWrite.All` when you reach SharePoint.

**Known gap:** `New-ApplicationAccessPolicy` is **Exchange-only**. It does nothing for OneDrive or SharePoint. There is no equivalent one-line mitigation for `Files.Read.All` — so **do not grant `Files.Read.All` in v1 at all.** Investigate Graph's newer granular file-level permissions before you need OneDrive access. Treat that as an open lead, not a solved problem.

### 8.5 MCP notes

**Do not build against the 2026-07-28 stateless spec revision yet** — it landed 28 July 2026 and is the largest revision since launch. Target the 2025-11-25 stable spec.

**Ownership rule:** the **Agent SDK owns MCP clients** (native support via `options.mcpServers`). LangGraph owns them only if the orchestration layer itself needs one — which should be rare. Never let both hold connections to the same server; it doubles OAuth refresh cycles for nothing.

**Auth boundary:** the MCP-client→server hop and the server→upstream-API hop are different trust boundaries. Never collapse them into one token. Never return raw upstream tokens through a tool-call response.

## 9. Knowledge, observability and data

### 9.1 Knowledge — markdown files on disk

**What it is:** a folder of markdown files. Accumulated context about projects, people, decisions, how you work.

**Why this and nothing else in v1:** the Agent SDK already ships with read, grep and glob. Point it at a folder and it reads the files. That covers a surprising amount of what a full retrieval pipeline is built for.

**The write rule:** agents write new learnings to a `pending/` folder only, never to canonical files. You promote or reject on a periodic pass. This is the human-review gate that stops agent-written memory drifting into mush — and it's the part that survives into the deferred retrieval layer unchanged.

Git-versioned, so you get diffs and history for free.

### 9.2 Observability — LangSmith (EU region)

**What it is:** a hosted service that records every step of every turn and shows it in a web UI. It watches; it decides nothing. Delete it tomorrow and Gojo runs identically — you'd just be debugging blind.

**Why:** already configured, one environment variable, and LangSmith/LangChain fluency is an explicit career goal.

**Configuration gotcha:** the EU region needs `LANGSMITH_*` variable names — **not** `LANGCHAIN_*` — plus:
```
LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com
LANGSMITH_PROJECT=Gojo-Agent-OS
```
Wrong prefix gives a 403 that doesn't look like a naming problem.

**Free tier:** Developer is 5,000 traces/month, 14-day retention, hard 429 at the cap (no silent billing).

**⚠ Unknown:** LangChain documents a trace as "a collection of runs for a single operation" but **never defines how nested runs compose into a billable trace**. Whether one Teams turn produces one root or several — particularly across the Agent SDK subprocess boundary — is **unverified**. Break-even is roughly 11 roots/turn. See §12 item 1; this is the first experiment to run.

**Mandatory:** gate dev tracing behind a flag (`LANGSMITH_TRACING=false` locally). Build iteration generates far more traces than actual use.

**Considered and rejected:** self-hosted Arize Phoenix, recommended by research on data-residency grounds since traces carry full mail bodies. Rejected because LangSmith fluency is a stated goal, EU residency is already configured, and data processing has been signed off by the owner. Self-hosted LangSmith is **not an option at any hardware spec** — it's an Enterprise-plan add-on requiring a sales-issued licence key, and expects 16+ vCPU with Kubernetes, Postgres, Redis and ClickHouse. Self-hosted Langfuse is also unviable here (ClickHouse mandatory).

### 9.3 Cost and failure control

**Prompt caching:** front-load stable content (system prompt, tool definitions) and never mutate it mid-session. Cache prefix order is `tools → system prompt → messages`; a change anywhere invalidates everything downstream.

**Model routing:** hand-written rule only. Cheap fast model for classification and routing; larger model for reasoning. Do not build a learned router — that needs volume to amortise and you have one user.

**Budget enforcement — use both:**
- `recursion_limit` (default 25) as a framework-level crash guard
- A **budget field in graph state** every node checks, with conditional routing to a graceful summariser exit

**The three ways single-VPS agent systems actually fail, ranked by evidence:**

1. **Runaway loops** — best-documented failure mode by a distance. Documented cases include an 11-day two-agent clarification loop with a green dashboard throughout. Build the hardest guard here.
2. **Partial-failure loss** — crash at step 47 of 50, restart repeats all 47 *including real side effects*. Checkpointing alone doesn't fix this. **Idempotency keys on every side-effecting connector call.**
3. **Compounding errors** — every agent hop is multiplicative. Keep chains short.

---

# PART THREE — BUILDING IT

## 10. Production grade — the definition

Six properties, deliberately no more. This exists to stop scope creep dressed as rigour.

1. **Survives restart** — conversation state persists
2. **Runs as a service** — systemd, auto-restart, starts on boot
3. **Every turn traceable** — meaning *debuggable*, not durable audit (LangSmith free tier is 14-day retention)
4. **Failures contained** — dead upstream API degrades the answer, doesn't crash the process
5. **Secrets handled properly** — including client secret rotation with a **recorded expiry date** (~June 2027)
6. **Destructive actions gated** behind human approval

**Not in scope:** multi-user, horizontal scale, HA, public rate limiting.

## 11. Build sequence — v1 is steps 1 to 5

| # | Step | Done when |
|---|---|---|
| **1** | **Scaffold + model in the graph** | uv workspace, FastAPI, LangGraph graph with a real model node. Testable via `curl` |
| **2** | **Teams surface** | Agents SDK wired, JWT validated. You talk to Gojo from your phone and it replies |
| **3** | **Two read connectors** | Graph mail (`Mail.Read` only) + Jira. Gather agent returns real findings. Access policy applied |
| **4** | **Persistence + service** | SQLite checkpointer, systemd unit. Properties 1 and 2 true |
| **5** | **Approval gates + write scopes** | `interrupt()` before writes, idempotency keys, `Mail.Send` granted. Property 6 true |

**At step 5 you have a complete, production-grade system you use every day.** Then you use it for a fortnight and let real gaps drive what comes next.

### 11.0 Current position — step 1, ~80% complete

| Piece | State |
|---|---|
| uv workspace, locked | ✅ langgraph 1.2.10, langchain-core 1.5.2, fastapi 0.141.1 — CVE floors cleared |
| LangGraph graph | ✅ 4 nodes (`classify`, `megumi`, `sukuna`, `respond`), conditional routing proven on both paths |
| LangSmith tracing | ✅ EU region, project `Gojo-Agent-OS`, traces landing |
| Agent SDK + subscription auth | ✅ `claude-agent-sdk 0.2.128`, guard in `config.py` rejects `ANTHROPIC_API_KEY` |
| Megumi reasoning for real | ✅ Verified — coherent output, not a stub |
| **FastAPI endpoint** | ❌ **The one remaining piece** |

**Outstanding debt — clear before step 2:**

1. **ADR 0004 not written.** Repo still contains ADR 0001 ("Bun") with nothing superseding it
2. **This document not yet on the VPS** at `docs/GOJO-MASTER.md`
3. **`build-log.md` untouched** across the build sessions
4. **`setting_sources=[]`** not yet applied to `runner.py` — agents currently inherit ambient Claude Code config

**Repo layout as built:**

```
Agentic-Assist/
├── apps/gojo/src/gojo/
│   ├── config.py          settings + assert_subscription_auth()
│   ├── orchestrator.py    graph, 4 nodes, conditional routing
│   ├── state.py           GojoState with reducers
│   └── agents/
│       ├── megumi.py      gather agent + static system prompt
│       └── runner.py      single Agent SDK entry point (§6.3 rule 2)
├── docs/decisions/        ADRs 0001-0003
└── pyproject.toml         uv workspace root
```

### 11.1 Already working — do not rebuild

- VPS bootstrapped, swap configured, NSG 80/443 open
- Caddy + TLS, chain verified HTTPS → Caddy → app → 200
- Entra app registration (`benkn-gojo-agent-orchestrator`, single-tenant), Azure Bot resource in `RG-BenKnox-Claude-Code`
- Monorepo, security-first `.gitignore`, ADRs 0002–0003
- LangSmith account and project
- Claude Code installed and authenticated on the VPS

### 11.2 Project structure

```
Gojo-multi-agent-os/
├── pyproject.toml              # [tool.uv.workspace]
├── uv.lock
├── apps/
│   └── gojo/                   # orchestrator + FastAPI endpoint
├── libs/
│   └── connectors/
│       ├── graph/
│       └── jira/
└── docs/
    ├── GOJO-MASTER.md          ← this document
    ├── build-log.md
    └── adr/
```

## 12. Deferred — researched, not built

**Everything below is fully specified in the research findings and deliberately not in v1.**

The reasoning: **"what needs my attention today" is a live API question, not a search question.** Graph knows your inbox; Jira knows your tickets. The agent calls them and reads the answer. Retrieval solves "find the needle in a large static corpus" — a real problem you'll hit, but not this one. Building it now means guessing what the corpus is before you know.

| Deferred | Trigger to build |
|---|---|
| PostgreSQL 17 + pgvector, `halfvec`, HNSW | First time you ask Gojo something it can't answer from a live API call |
| Voyage embeddings, RRF fusion, reranking, chunking | Same |
| RAGAS, DeepEval, golden sets | When retrieval exists and you need to prove a change improved it |
| Nine remaining connectors | When a workflow you actually run needs one |
| Multi-agent routing beyond gather/act | When a tool list passes ~15–20 tools |

**The research isn't wasted — it's a map, not a build plan.** It caught the LangGraph CVE chain, the archived Bot Framework SDK, the delegated-auth problem, and the tenant-wide blast radius. All four are v1 concerns and all four are handled above. The retrieval half is a reference you open at the trigger.

## 13. Open — settle by experiment, not research

1. **Roots-per-turn in LangSmith.** Ten instrumented turns with a known topology answers it. **Do this first** — it retires the biggest unverified assumption in this document.
2. **Current Agents SDK version.** Check PyPI before pinning.
3. **`MemoryMax` sizing.** Start 5–6 GiB, load-test.
4. **Agent SDK cache TTL** — flagged as a documentation gap in Anthropic's own repo. Verify against response metadata.
5. **Adaptive Cards schema version** Teams currently renders — five-minute empirical test.
6. **Jira and Zoho API access** at Conversant's licence tier — account verification, not research.
7. **Graph granular file permissions** — whether a `Files.Read.All` equivalent to `Sites.Selected` exists.

## 14. Working principles

**Learn as you build.** For every new technology — LangGraph, MSAL, Agents SDK, pgvector — explain the concept and why it exists **before** giving code. Not after. Standing requirement, no exceptions.

**Get it working first.** No premature security tightening, architectural perfectionism, or speculative abstraction.

**Verified answers only.** No speculation. Flag uncertainty explicitly. Check when checkable. Confident wrong answers have cost this project real time — Anthropic's billing model, the Bot Framework SDK's status, LangSmith's trace-counting rules, and the Agents SDK version were all asserted incorrectly before being verified.

**Decisions become ADRs.** Recorded with reasoning at the moment they're made, including superseded ones. The history of reversals is itself portfolio evidence.

**The stack is frozen.** §3–§9 do not reopen until something runs end to end in Teams. The foundation was reopened five times before this document; each reopening was individually reasonable and collectively expensive. Frozen until there's working software to have opinions about — not frozen forever.

## 15. Superseded — check before trusting any ADR

| Source | Original | Status |
|---|---|---|
| **ADR 0001** | Bun runtime, Node 20 compatibility | **SUPERSEDED** — Python 3.12. Needs ADR 0004 |
| *informal* | Teams bot bridges to a Claude Code session as the whole system | **SUPERSEDED** — LangGraph orchestrates, Agent SDK executes |
| *informal* | Agent SDK moves to a separate credit pool | **INCORRECT** — paused. §6.4 |
| *informal* | Write directly against Bot Framework protocol, skip the SDK | **SUPERSEDED** — use Agents SDK. §5 |
| *research* | Agents SDK v0.9.0 is current | **STALE** — past 1.0. §5.1 |
| *v2 §4.11* | Nested runs don't bill separately (stated as fact) | **UNVERIFIED** — undocumented. §9.2, §13 item 1 |
| *informal* | Config includes `anthropic_api_key`; API key is a viable auth route | **INCORRECT** — shadows subscription auth. §6.2 |
| *v2 build seq* | Eleven connectors, steps 1–8 | **SUPERSEDED** — two connectors, steps 1–5. §11, §12 |

ADRs 0002 (monorepo structure) and 0003 (secrets handling) remain valid.

## 16. Portfolio

Two repos: private working repo (permanent, full history including failures) and a curated public showcase repo created later.

**Reference standard:** your existing **Meeting Intelligence** project. Gojo should match or exceed its presentation. *(Fresh sessions: ask for the link and conventions rather than guessing.)*

**What makes this portfolio-grade:**
- README explaining problem, architecture and reasoning — not setup steps
- ADRs visible, **including superseded ones** — recorded reversals demonstrate judgement
- Tests that run
- Honest documentation of constraints and trade-offs
- The deferral decisions themselves: "I researched the retrieval architecture, then deliberately deferred it until I had data to retrieve over" is a stronger story than building it speculatively

## 17. Fresh session onboarding

1. Read this document fully.
2. Check §15 — superseded decisions — before trusting any ADR or code comment.
3. Check §11 for current build position.
4. Check §13 for what's open. **If you need one to proceed, stop and ask** — don't choose silently.
5. Respect §14, particularly *learn as you build* and *verified answers only*.
6. Remember §3.1 — one physical core shapes almost every choice.
7. §6.1 version pins and §6.3 build rules are mandatory. §8.4 mitigations are mandatory.

**Ask before assuming.** Several expensive detours came from confident assumptions that turned out wrong. Uncertainty stated plainly is always cheaper.

---

**Next action:** ADR 0004 (Python supersedes Bun), then the uv workspace scaffold, then step 1.

# Build Log

> Chronological record of what was set up, the commands used, and why.
> A learning reference and a narrative of how the system was built.

## Session 1 — Foundation (VPS bootstrap + repo)

**Date:** 2025-06-17
**Goal:** Stand up a hardened VPS and a clean repo before writing any application code.
**Outcome:** Hardened Ubuntu box, dual runtime, SSH-auth'd GitHub, clean monorepo, commit one live.

### 1. GitHub SSH authentication
**Why:** The VPS needs to push to GitHub without storing passwords/tokens in plaintext. An SSH key keeps the private key on the box, authenticates silently, nothing to leak.
- ed25519 key generated to ~/.ssh/github_gojo, ~/.ssh/config maps github.com to it, public key added to GitHub.
- Concepts: .pub = public (shareable), no-.pub = private (never shared). "GitHub does not provide shell access" = success.
- Gotcha: first keygen didn't complete -> files didn't exist -> "Permission denied (publickey)". Verify the artefact exists before building on it.

### 2. Swap file (OOM insurance)
**Why:** 7.7 GB RAM, zero swap. Multiple services risk an OOM kill with no warning.
- 4GB /swapfile, chmod 600, mkswap, swapon; persisted via /etc/fstab; vm.swappiness=10 (prefer RAM).
- Concepts: swap = disk overflow for RAM; swappiness = how eagerly Linux swaps; fstab = what mounts at boot.
- Gotcha: merged commands meant swappiness didn't persist first time. Verify config writes with grep.

### 3. System update + core tooling
**Why:** Fresh box needs current packages and base tools.
- apt update/upgrade; installed git curl wget unzip build-essential ca-certificates gnupg.

### 4. Runtimes — Bun + Node
**Why:** Bun primary (runs TS directly, fast, matches blueprint). Node = compatibility net (Claude Code + npm tooling expect it).
- Bun 1.3.14 via official installer; Node 20.20.2 via NodeSource.
- Concepts: runtime executes code; package manager installs/tracks deps; LTS = stable long-term release; PATH = where the shell finds commands.

### 5. Clone repo + clean structure + commit one
**Why:** Repo links VPS (runs code) to GitHub (versions it, becomes portfolio). Deliberate structure before code keeps the estate clean.
- Cloned over SSH; set git identity; built folder skeleton (only what we'll use soon); .gitignore BEFORE any secrets; master doc -> docs/context.md; git add/status/commit/push.
- Concepts: monorepo (one repo, many packages); git stages (working->staging->commit->push); git status before commit = security discipline; .gitkeep keeps empty dirs; build the wall before secrets exist.

### Session 1 result
Hardened Ubuntu 24.04 · 4GB swap · Bun 1.3.14 + Node 20.20.2 · SSH-auth'd GitHub · clean monorepo with security-first .gitignore + documented context · commit one live.

**Next session:** Gojo — Azure Bot + Entra ID, Teams bridge plugin (four-layer security + prompt-injection fence), Caddy, Claude Code, first round-trip.

## Session 2 — Python rebuild (LangGraph orchestrator + Agent SDK execution layer)

**Date:** 2026-07-30, 2026-08-04
**Goal:** Rebuild the estate on Python, with LangGraph as orchestrator and the Claude Agent SDK as execution layer.
**Outcome:** uv workspace on Python, four-node LangGraph orchestrator routing on both paths, Agent SDK wired through a single entry point, tracing live, billing hazard guarded, ADR 0004 written.

### 1. Repo recreated as Agentic-Assist
**Why:** Portfolio-facing name.
- Repo recreated as Agentic-Assist; the Python package remains gojo.
- ADRs 0001-0003 carried across from Gojo-multi-agent-os.

### 2. uv workspace + dependencies
- uv workspace scaffolded; langgraph 1.2.10, langchain-core 1.5.2, fastapi 0.141.1.
- CVE floors from GOJO-MASTER 6.1 cleared.

### 3. LangGraph orchestrator
- Nodes built: classify, megumi, sukuna, respond.
- Conditional routing verified on both the read and write paths.

### 4. LangSmith tracing
- Tracing live; EU region; project Gojo-Agent-OS.

### 5. Claude Agent SDK
- SDK 0.2.128 wired via agents/runner.py as a single injectable entry point, per GOJO-MASTER 6.3 rule 2.
- Megumi verified reasoning for real on the Max subscription.

### 6. Auth hazard — ANTHROPIC_API_KEY precedence
- ANTHROPIC_API_KEY takes precedence over Claude Code's subscription credentials and would silently move billing to API rates.
- Guard added in config.py; GOJO-MASTER 6.2 updated.

### 7. ADR 0004
- ADR 0004 written, superseding ADR 0001.

### Session 2 result
Agentic-Assist on a Python uv workspace · langgraph 1.2.10 + langchain-core 1.5.2 + fastapi 0.141.1, CVE floors cleared · LangGraph orchestrator (classify, megumi, sukuna, respond) routing verified read and write · LangSmith tracing live (EU, Gojo-Agent-OS) · Agent SDK 0.2.128 behind agents/runner.py · ANTHROPIC_API_KEY billing guard in config.py · ADR 0004 supersedes ADR 0001.

## Session 3 — Steps 1, 2 and 3 complete (FastAPI, Teams, persistence)

**Date:** 2026-08-04
**Goal:** Finish step 1, build the Teams surface, then persistence and service management.
**Outcome:** Gojo answers from Teams on a phone, behind JWT validation and a single-user allow-list, runs under systemd, and remembers conversations across restarts. Steps 1-3 done; step 4 blocked on Azure permissions.

### 1. FastAPI surface — step 1 complete
- `POST /chat`, `GET /health`. Graph compiled once in lifespan; auth asserted at boot.
- **Gotcha — ADR 0005:** every Agent SDK call fails under uvloop with "Reached maximum number of turns", 3/3 against 3/3 under asyncio. `uvicorn[standard]` selects uvloop by default. Pinned `loop="asyncio"` in a module entrypoint so a systemd unit cannot silently reintroduce it. Root cause inferred, not established.

### 2. Teams surface — step 2 complete
- Agents SDK **1.3.0** (PyPI-verified; the doc's 1.1.0 was two minor versions stale). `hosting-teams` requires py>=3.12.
- **ADR 0006:** turns answered in two parts — typing indicator and a single reply inside the budget, else an acknowledgement and a proactive delivery. Azure Bot Service returns 504 after 10-15s.
- Config keys are `CLIENTID`/`TENANTID`/`CLIENTSECRET`, **not** Bot Framework v4's `MicrosoftAppType`/`MicrosoftAppTenantId`.
- **Security gotcha:** `/api/messages` was registered without `jwt_authorization_decorator` and was publicly unauthenticated between two commits. The adapter does not authenticate on its own. Now 401 for unsigned and bogus tokens, verified from the public internet.
- **Gotcha:** `continue_conversation` — `ChannelAdapter` takes a `ConversationReference`, `ChannelServiceAdapter` (which `CloudAdapter` inherits) overrides it to take an Activity. Acknowledgements arrived, answers never did. The error named neither the argument nor the method.
- **Gotcha:** `MsalConnectionManager` key must be the literal `"SERVICE_CONNECTION"`. `AgentApplication` needs both `ApplicationOptions.storage` and `connection_manager`.
- Teams app package required — a bot with the channel enabled is reachable but not installable. `packageName` was removed in manifest schema 1.17 and `additionalProperties` is false. Build now validates against a vendored schema.
- **Authorisation:** JWT proves the message came from Bot Service, not who sent it. Added an Entra object-ID allow-list plus a tenant check, failing closed on every missing value.

### 3. Persistence and service — step 3 complete
- **ADR 0007:** swapped steps 3 and 4. Using the system showed continuity and restart-survival matter before tenant-wide mail permissions.
- `AsyncSqliteSaver` keyed by Teams conversation id; `runner.py` moved from `query()` to `ClaudeSDKClient` for sessions. The SDK owns the transcript, the graph stores only the session id (6.3 rule 3).
- **Gotcha:** `operator.add` reducers would have grown state for the life of a conversation once persisted. A `new_turn` node clears per-turn fields.
- systemd unit; **subscription auth works under systemd with no `CLAUDE_CODE_OAUTH_TOKEN`**, contrary to 6.2 — the unit runs as `ccuser` and reads stored credentials, which is why `ProtectHome` must stay unset.
- **Gotcha:** `StartLimitBurst`/`StartLimitIntervalSec` belong in `[Unit]`. systemd ignores them with a warning in `[Service]`, so the crash-loop guard did nothing.
- Runaway guards: 180s wall clock, explicit `recursion_limit`, 5 agent calls per turn with a graceful exit.
- `/new`, `/compact`, `/help`. Commands never reach an agent.
- **Gotcha:** every `logger.info` was being dropped — uvicorn configures only its own loggers. Structured logging with per-turn correlation ids now reaches the journal, including from the SDK's own logger.
- **Gotcha:** `ApplicationOptions.start_typing_timer` defaults to True. The SDK's own typing loop re-sends via reply-to-activity, which Teams rejects with 400, surfacing as "Exception caught" while turns succeeded.

### 4. Measurements
- LangSmith: **1 root run per turn** (8 turns / 8 roots), retiring 13 item 1. Break-even was ~11.
- **Agent reasoning is invisible in LangSmith** — all runs are `chain` type, zero tokens. The SDK subprocess does not report back. New open item, 13 item 8.
- Turn cost: **$0.009 fresh, $0.036-0.052 on a long session**. Session replay is real and shows in tokens, not latency. `/compact` and `/new` control it.
- Turn latency 3.4-6.5s; fast-reply budget raised 5s -> 8s.

### 5. Step 4 blocked, and a correction
- **8.4's mandated mitigation is deprecated.** Application Access Policies are replaced by RBAC for Applications. Following the doc as written would have granted tenant-wide `Mail.Read` and scoped nothing — Entra and RBAC grants are a union.
- Runbook with real values: `infra/graph-mail-rbac.ps1`. **Do not grant `Mail.Read` in Entra.**

### Session 3 result
Steps 1-3 complete · 46 tests · Teams live behind JWT + single-user allow-list · systemd, restart-survival verified · continuity via checkpointer + SDK sessions · runaway guards · session commands · structured logging · ADRs 0005-0007 · README · 8.3/8.4 corrected.

**Next session:** run `infra/graph-mail-rbac.ps1` (needs Exchange Administrator via PIM), verify the negative scoping test, then build the Graph mail connector as SDK `@tool` functions given to Megumi.

## Session 4 — In-flight resumption (ADR 0008) and the root cause behind it (ADR 0009)

**Date:** 2026-08-05
**Goal:** Close ADR 0006's accepted gap: an acknowledged Teams turn is lost if the process restarts before the proactive reply.
**Outcome:** Closed and verified live. Four independent design reviews before a line of code; the fourth-round root cause became ADR 0009. The on-box acceptance run found two further live bugs and fixed them.

### 1. Design — four review rounds, each finding a real defect
- Round 1-2: unguarded recovery would deliver the NEW question's answer twice (`ainvoke(None)` returns whatever the thread's latest turn produced).
- The fix, a checkpoint-id pin, was worse: **the pin moves during the owed turn's own progress** (ACK observes `next=('megumi',)`, then `megumi` and `respond` checkpoint), so it abandoned exactly the replies it existed to deliver. Round 3.
- Round 3's turn-id guard reopened `/new`, which `aupdate_state` makes invisible to it. Round 4 found the guard was check-then-act over a resume that holds the thread for seconds.
- **Root cause (ADR 0009): nothing serialises operations on one conversation.** Measured on the deployed code with no recovery involved: `/new` racing an in-flight turn was silently reverted (checkpoint fork), overlapping turns forked history. One lock per thread fixed the class; every prior defect was a shadow of it.

### 2. Execution — 8 tasks, TDD, one commit each
- Pre-existing bug fixed first: **the test suite wrote to the production checkpoint DB** (`Settings(_env_file=None)` kept the relative default path; VPS.md puts `uv run pytest` in the deploy procedure). Found independently by two reviewers.
- `owed_replies` outbox in the checkpointer's file; `new_turn` stamps the turn id into state; `resume_turn` (same 9.3 guards); per-thread locks; debt recorded before the ACK, never able to break the turn; `/new` clears owed rows; recovery under the lock; lifespan wiring with shutdown cancellation; `owed_replies` on `/health`.
- **Gotcha:** asyncio.Lock binds to the loop that first acquires it; module-level registry + pytest's per-test loops = "bound to a different event loop". Autouse conftest fixture clears the registry; production has one loop for the process lifetime (ADR 0009 records the assumption).
- **Gotcha:** a cancel-mid-graph test raced the first checkpoint write and flaked with EmptyInputError. Deterministic shape: crash first, then hang the resume.

### 3. Acceptance on the box — two staging misfires, two real bugs, then proof
- Misfire 1: a ~5s turn beats a human-relayed restart. Misfire 2: a 5-minute watcher window expired. Fix: a script that kills the service the instant the debt row appears; `FAST_REPLY_SECONDS=1.0` via a temporary systemd drop-in so every turn ACKs (removed after).
- **Live bug 1:** Teams split a long message; the fragment turn's megumi returned empty text; `[""]` is truthy, `""` is not, so respond passed it through; Teams 400s an empty activity and the SDK surfaced "Exception caught" into the chat. The same trace showed the ADR 0009 lock serialising the split turns cleanly. Fixed with a fallback in respond plus a belt in `_run_graph`.
- **Live bug 2:** every RESUMED session returned empty text ("(no findings)"), deterministically, while fresh sessions answered. Megumi's `max_turns=1` dated from the tool-less stub; the SDK's turn accounting spans a resumed session, so the cap was spent before the model spoke. Now takes `max_turns_per_agent` (8); the runner logs subtype + turn count whenever an agent returns no text.
- **Proof (turn 03b655b8):** /new 21:41:42 → question → ACK 21:41:57 (debt recorded) → **service killed 21:41:58 mid-agent-call** → new process 21:41:59: "recovering 1 owed repl(ies)", resumes the same turn id → **21:42:03 "recovery delivered 1 of 1"** — the user's answer arrived from a process that did not exist when the question was asked. Kill-to-delivery ~6s. Settle-on-delivery separately proven: killed 2s after a delivered answer, next boot ran zero recovery passes, no duplicate.
- The moved-on and /new-during-recovery scenarios are unit-covered (guard + lock); not re-staged live — the timing cannot be reliably staged by hand and the mechanisms were each proven separately.

### Session 4 result
Steps 1-3 remain complete, hardened · 87 tests · in-flight turns survive restarts, verified live · per-conversation serialisation (ADR 0009) fixing a live /new race · empty-answer and max_turns=1 bugs fixed · tests no longer touch the production DB · ADRs 0008-0009 · four review rounds recorded, including the reviews' own wrong turns.

**Next session:** build step 4 — run `infra/graph-mail-rbac.ps1` (Exchange Administrator via PIM), verify the negative scoping test, then the Graph mail connector as SDK `@tool` functions.

## Session 5 — Step 4: read connectors, RBAC scoping, threat model

**Date:** 2026-08-07
**Goal:** Build step 4 (Graph mail + Jira read connectors behind RBAC-scoped permissions) plus two agreed extras: THREAT-MODEL.md and the LangSmith reasoning span. Sequenced so the security narrative is complete early — Snyk interview in 5 days uses Gojo as its centrepiece.
**Outcome:** In progress. Build half done and pushed before the RBAC session; scoping granted and proven both ways on the live tenant.

### 1. Plan and review before any code
- Full plan drafted from GOJO-MASTER v3.5, then an unprimed adversarial review (standing practice). Verdict "executable with fixes"; two majors both sat on the security story: the threat model's draft claim "no write tool exists in the process" was **false** — the SDK subprocess ships Claude Code's built-ins, merely default-denied — and `/compact` laundered untrusted mail content past the data wrapper into the next session's bare prompt. Both fixed in design before implementation. Also caught: Graph's orderby-in-filter constraint, a client-singleton test-isolation hazard, MSAL's error-dict (not exception) contract.

### 2. Build — TDD throughout, 133 tests, pushed as five commits
- `gojo-graph` and `gojo-jira` workspace packages: thin, SDK-free, httpx; reduced field sets are the whole surface. Graph: MSAL client-credentials on a thread, `/users/{upn}/messages`, `bodyPreview` only. Jira: delegated basic auth, `/rest/api/3/search/jql` (old `/search` is gone), 400s surface Jira's own JQL diagnosis.
- Gather tool layer: one in-process MCP server (ADR 0010), `mcp__gather__list_recent_mail` + `mcp__gather__search_issues`. Runner: explicit `disallowed_tools` for every CLI built-in, `strict_mcp_config=True`, `mcp_servers` param. All fetched content wrapped as `<external-data>` (closing tag stripped); `/compact` summarises tool-free and re-wraps its summary; tool logs carry counts/shapes, never content.
- LangSmith span around the SDK exchange (13.8 first half) — cost/turns/session-id will show as a child of agent nodes; live trace shape still to be measured.
- **Gotcha:** two connector test files named `test_client.py` with no package `__init__` collide in pytest collection — renamed to unique basenames.
- docs: THREAT-MODEL.md (assets, six boundaries, containment argument, control→evidence table), ADR 0010, fifth row in §18.

### 3. RBAC for Applications — granted and proven, live tenant
- **Gotcha (live):** the runbook carried the App registration's Object ID where the service principal's was needed; `New-ServicePrincipal` failed loud with `AADServicePrincipalNotFound`. Correct value comes from the Enterprise application page (App registration → Overview → "Managed application in local directory"). Runbook fixed: SP Object ID is `ad389ad2-5beb-4738-aa36-92052bc365e8`.
- Grant sequence ran clean under Global Administrator via PIM (confirmed sufficient — GA maps to Organization Management, which holds Role Management). Nothing granted in Entra.
- **Evidence — positive test (owner's mailbox):**
```
RoleName                GrantedPermissions  AllowedResourceScope  ScopeType             InScope
--------                ------------------  --------------------  ---------             -------
Application Mail.Read   Mail.Read           Gojo-OwnerMailbox     CustomRecipientScope  True
```
- **Evidence — negative test (real colleague's mailbox, adnan.khan@):**
```
RoleName                GrantedPermissions  AllowedResourceScope  ScopeType             InScope
--------                ------------------  --------------------  ---------             -------
Application Mail.Read   Mail.Read           Gojo-OwnerMailbox     CustomRecipientScope  False
```
- The first attempt at the negative test used a placeholder address and returned "object not found" — which proves nothing. A meaningful `False` needs a mailbox that exists. Re-run against a real colleague: `InScope: False`. **`Mail.Read` was scoped to one mailbox without ever being tenant-wide.**

# ADR 0006 — Answer Teams turns in two parts: acknowledge, then reply proactively

**Status:** Accepted
**Date:** 2026-08-04

## Context
Azure Bot Service POSTs an Activity to `/api/messages` and waits for the HTTP response. Microsoft documents a **10–15 second timeout depending on channel**, after which the user receives `504 GatewayTimeout` and the turn is lost.

Measured against that budget:

| | Latency |
|---|---|
| Megumi today — no tools, `max_turns=1` | ~5s |
| `settings.max_turns_per_agent` default | 8 |
| Step 3 adds Graph + Jira round trips | more, per turn |

A synchronous reply fits today and will not fit at step 3.

Microsoft's own guidance for long operations uses Azure Functions, Queue Storage and Direct Line, because it assumes a stateless bot that cannot hold work in flight.

## Decision
Answer every turn in two parts. On receiving an Activity: capture the conversation reference, send an immediate acknowledgement, and return. Run the graph in a background task in-process, then deliver the answer with `adapter.continue_conversation()` as a separate outbound call.

No Azure Functions, no Queue Storage, no Direct Line.

## Rationale
- **Building synchronously means rewriting at step 3.** The timeout is a hard channel limit, not a tunable — and this is measured, not anticipated.
- **We are not the stateless bot Microsoft's sample assumes.** Gojo is a persistent single-worker service on its own VPS (§4.3), so the "external service" that runs the long operation can be an `asyncio` task in the same process. Three Azure resources to achieve that would be scope creep under §10.
- **The acknowledgement is honest UX, not a workaround.** A turn that takes 20 seconds should say so.
- **`continue_conversation` is the SDK's supported path**, verified in the installed source at `channel_adapter.py:93`, not inferred from the C# documentation.

## Consequences
- **A turn in flight does not survive a restart.** The acknowledgement is sent, the reply is not. Accepted at step 2; the checkpointer at step 4 is what makes resumption possible, and the gap should be closed there rather than papered over now.
  **Closed 2026-08-05 by ADR 0008** — the outbox records the debt at the acknowledgement, matched by turn id, and startup recovery resumes it from the checkpoint under ADR 0009's per-conversation lock. Verified live: a turn killed mid-agent-call was delivered by the next process.
- **Background tasks must be held in a module-level set.** `asyncio` keeps only weak references to tasks, so an unheld task can be garbage-collected mid-flight and the reply disappears with no error.
- `/health` reports `turns_in_flight`, so work in progress is visible rather than inferred.
- Graph failures inside a background task cannot become an HTTP error code. They are caught, logged, and reported to the user as a message (§10 property 4).
- The 10–15s figure comes from Bot Framework v4 documentation. The **constraint** is verified; the exact per-channel number for Teams specifically was not re-measured, and the design does not depend on it — anything above a couple of seconds argues for the same shape.

## Related
- Package versions verified against PyPI on 4 August 2026: the four `microsoft-agents-*` packages are **1.3.0**, not the 1.1.0 recorded in GOJO-MASTER §5.1. Closes §13 item 2.
- The Agents SDK does **not** use Bot Framework v4's `MicrosoftAppType` / `MicrosoftAppTenantId`. It reads `CLIENTID` / `TENANTID` / `CLIENTSECRET`, and builds the accepted token issuers from `TENANT_ID` (`agent_auth_configuration.py:167`). Setting the tenant is what makes validation single-tenant. §5.2's advice holds; its key names do not.

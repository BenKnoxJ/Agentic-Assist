# ADR 0007 — Build persistence before connectors: swap steps 3 and 4

**Status:** Accepted
**Date:** 2026-08-04

## Context
Step 2 is complete — Gojo answers from Teams on a phone. Using it immediately surfaced three gaps that reading the plan had not:

- **Every message starts from zero.** No conversation continuity, so it cannot be talked to; it can only be queried.
- **The service is a `nohup` process.** It dies on reboot, nothing restarts it, and a turn in flight during a restart is lost after the user has already been acknowledged.
- **No `/new` or `/compact`.** There is no state to clear or summarise, so the familiar controls have nothing to act on.

All three are step 4 (persistence + service). The documented order puts step 3 (two read connectors) first.

Step 3 is also the largest authorisation exercise in the plan: Graph application permissions are tenant-wide (§8.4), the `New-ApplicationAccessPolicy` mitigation is mandatory and simultaneous with the grant, PIM elevation is required, and Jira access at Conversant's licence tier is still unverified (§13 item 6).

## Decision
Swap them. Persistence and service management become **step 3**; the two read connectors become **step 4**. Steps 5 and 6 are unchanged.

## Rationale
- **§11 says to let real gaps drive what comes next.** These gaps were found by using the system, which is exactly the evidence the plan asks for. This is not a speculative reorder.
- **Connectors should land on a base that survives.** Granting tenant-wide mail permissions to a process that dies on reboot, loses in-flight work, and has no structured logs is the wrong order of operations.
- **Persistence needs no privileges.** No tenant consent, no PIM, no §8.4 mitigations, no third-party licence questions. It is the lowest-risk work available, and it is entirely within our control.
- **It unblocks the requested experience.** Session continuity, `/new` and `/compact` all depend on the checkpointer.
- **The carried debt belongs here anyway.** The runaway-loop guard and the graph timeout (§11.0) are both about running unattended, which is what step 3 now delivers.

## Consequences
- **§1.1's one-sentence test is delayed by one step.** Gojo cannot answer "what needs my attention today" until connectors land. Accepted: it cannot answer it today either, and the delay buys a foundation that does not have to be redone.
- **Step numbers in older ADRs and code comments now refer to the old order.** §18 already establishes that ADRs are point-in-time; §11 is the current sequence and wins. Comments citing "step 4" for persistence were correct when written.
- **`runner.py` must move from `query()` to `ClaudeSDKClient`.** §6.2 specifies that Megumi and Sukuna use the latter "for the full agentic loop with tools, sessions and manual control"; the runner currently uses `query()`. That was adequate for a tool-less stub and is not adequate for sessions. This divergence was found while planning continuity and belongs to the new step 3.
- The Agents SDK version check (§13 item 2) is already closed, so nothing in step 4 is blocked by this swap.

## Related
- ADR 0006 — the two-part reply, whose in-flight loss on restart is one of the gaps this addresses.

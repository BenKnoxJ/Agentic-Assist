# ADR 0011 — The approval gate: propose, approve, execute exactly once

**Status:** Accepted
**Date:** 2026-08-07

## Context
Step 5 makes production property 6 true: destructive actions gated behind
human approval. §13 item 9 had parked the design question — routed
classification (`classify` guessing "this will change something" from
phrasing) versus the Agent SDK's `can_use_tool` callback (a gate at the
moment a write tool is invoked). Both assume something this decision
rejects: that an agent holds write tools at all.

Design constraints that shaped the outcome: the human wait must survive a
restart (ADR 0008's world); everything on one conversation must serialise
(ADR 0009); recovery replay must stay safe now that replays can have side
effects (ADR 0010's inherited obligation); and mail content is adversarial
input that the compose step will read (THREAT-MODEL.md).

## Decision
**No write tool is ever exposed to any agent.** The write path is three
separated powers:

- **Sukuna proposes.** A compose agent holding exactly the same read-only
  tools as Megumi; its entire output is one strict-JSON `ActionProposal`
  (unknown fields are a parse failure). Malformed output — including its
  deliberate `ABSTAIN` — fails safe to a plain reply.
- **Only the owner approves.** A LangGraph `interrupt()` in a dedicated
  gate node pauses the graph; the decision arrives as the next Teams
  message (Adaptive Card tap or bare yes/no). The interrupt persists in the
  checkpointer, so the approval can arrive at a process that did not exist
  when the question was asked.
- **Only deterministic code executes.** `actions.execute` replays the
  approved bytes from the ledger row verbatim, under a sha256 check, with
  no model anywhere between approval and the Graph call.

**§13 item 9 is resolved by dissolving it**: classify stays routing-only —
a misroute is a UX error, never a safety error, because the gather path
cannot write and the act path cannot act without approval. `can_use_tool`
(verified present in SDK 0.2.128, shadowed by whole-tool `allowed_tools`
entries) is the recorded evolution path for a future Sukuna that needs
multi-step write autonomy; it is not used now. This supersedes the master
document's earlier sketch of a "write-tool server for Sukuna".

**The ledger is the idempotency mechanism and the audit trail.** One row
per action: `proposed → approved → (draft_created →) executed`, or
`declined | cancelled | failed`. The row, not the HTTP call, is the unit of
exactly-once. **Send is two-phase**: the draft id is persisted *before* the
send POST, so a crash-replay re-sends the same draft — which Graph refuses
once sent — and can never mint a second copy.

**Reply targets are verified deterministically.** For a reply, the sukuna
node fetches the target message by id via the connector and embeds its real
sender/subject in the proposal; the approval prompt leads with them. The
human is never asked to trust agent prose about where a reply goes.

## Rationale
An agent that can only speak can at worst be fooled into *proposing*
something the owner then reads verbatim and refuses. Gating an agent's
write tools instead would put a model between approval and action — exactly
where a prompt injection wants to stand — and the matching logic ("does
this tool call correspond to what was approved?") is the same
patch-the-races complexity that produced ADR 0009's four review rounds.

## Consequences
- **Verified langgraph 1.2.10 behaviours the implementation leans on**,
  established by an adversarial review with empirical probes: a new input
  on a paused thread silently discards the interrupt (teams.py guards
  every inbound message under the conversation's lock); `aupdate_state`
  with values empties `snapshot.interrupts` while the gate stays resumable
  (all guards key on `snapshot.next` too; `/compact` refuses on a paused
  gate; `/new`'s gate check runs before its values-update);
  `Command(resume=…)` on a non-paused thread silently returns the previous
  final state (`resume_gate_locked` re-checks pending under its own lock —
  a double card tap is refused, not replayed).
- **Approval turns always record their outbox debt before the resume
  attempt** and settle it on fast-path delivery — an approved action cannot
  vanish silently in a crash window. The lifespan additionally warns about
  any approved-but-unfinished ledger row at boot.
- **Recovery re-delivers the approval prompt** on a gate-paused thread
  instead of resuming (resuming would read an empty reply and deliver
  FAILED — reproduced by test before the fix), and a resume that runs
  *into* the gate (crash mid-compose) also delivers the prompt. After a
  re-delivery clears the row, a further restart re-prompts nothing — the
  gate persists and the next message hits the guard, so nothing is lost.
- **Accepted warts, stated:** a prompt abandoned by the outbox age ceiling
  means the owner may later get a "discarded the pending action" notice for
  an action they never saw; a crash between the approval message's arrival
  and the gate checkpoint leaves a debt row recovery abandons with a
  WARNING, and typing "yes" again resumes cleanly.
- **Sessions:** compose resumes the thread's shared SDK session (context for
  "reply to the mail we just discussed") but the node does not write its
  session id back — Megumi's thread stays canonical. Trade: the strict-JSON
  persona resumes a prose transcript, which raises malformed-proposal odds;
  the fail-safe path absorbs it.
- **Consent is exact.** "yes but change the subject" cancels loudly and is
  then handled as a new message; a stale card tap is refused with a notice.
- The `Mail.ReadWrite` / `Mail.Send` RBAC grants happen only after
  THREAT-MODEL.md §4's re-argument (its §7 requires this), scoped by the
  same management scope as read, nothing granted in Entra.

## Related
- ADR 0008 — the restart-survival machinery the gate extends.
- ADR 0009 — the lock discipline every gate path runs under.
- ADR 0010 — the read-only tool mechanism, and the replay obligation this
  ADR discharges.
- THREAT-MODEL.md — §4 (re-argued containment), §7 (review triggers).

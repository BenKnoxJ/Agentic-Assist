# ADR 0008 — Resume in-flight turns after a restart via an outbox table

**Status:** Accepted
**Date:** 2026-08-05

## Context
ADR 0006 answers every Teams turn in two parts: acknowledge inside the channel's
10–15 second window, then deliver the answer proactively with
`continue_conversation`. Its consequences record the cost:

> A turn in flight does not survive a restart. The acknowledgement is sent, the
> reply is not. Accepted at step 2; the checkpointer at step 4 is what makes
> resumption possible, and the gap should be closed there rather than papered
> over now.

That step is now step 3 (ADR 0007), and step 3 is complete. **The gap was not
closed.** ADR 0007 went further and listed in-flight loss among the gaps the
swap addresses; the checkpointer landed, the resumption did not.

Two things needed to finish an acknowledged turn live only in process memory:
the `asyncio` task computing the answer, and the `ConversationReference`
captured in `on_message` — the only route back into that conversation. A
restart loses both. The user is left holding "On it — give me a moment." and
receives nothing, with no record anywhere that a reply is owed.

The gap is invisible in the documentation as well as at runtime. It appears
in no §11.0 debt list and no §13 open item — only as a consequence line inside
an accepted ADR, which is why it stayed quiet across two sessions.

What the checkpointer does provide is half the answer. `AsyncSqliteSaver`
writes after every super-step, not only at the end, so `ainvoke(None, config)`
resumes a thread from its last completed node. A crash after `megumi` returned
re-runs only `respond`, and the agent call is not paid for twice. What the
checkpoint cannot express is that a human is owed a reply, or where to send it.

## Decision
Record owed replies in an **outbox table** in the existing
`checkpoints/gojo.sqlite`, written before the acknowledgement and deleted after
successful delivery. On startup, resume each owed thread from its checkpoint
and deliver.

```
owed_replies(
  turn_id      TEXT PRIMARY KEY,   -- the correlation id the logs already carry
  thread_id    TEXT NOT NULL,      -- Teams conversation id = LangGraph thread
  reference    TEXT NOT NULL,      -- ConversationReference, serialised
  attempts     INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL
)
```

**The acknowledgement creates the debt.** Before the ACK there is no promise:
the turn dies, Azure Bot Service returns 504, and the user sees a failure
rather than silence. After it, a reply is owed unconditionally.

**Recovery is silent.** The answer arrives as the proactive reply ADR 0006
already promised, with no "I restarted" preamble. The user experience of a
recovered turn is a slower turn.

**Recovery runs after the app begins serving,** as a lifespan background task
rather than before `yield`.

**A resumed turn that fails is still answered.** If resumption raises, times
out, or yields no reply, the user receives the same failure message
`_run_graph` already produces for a live turn, and the row is cleared. A turn
that cannot be completed is not left owed; the promise was a reply, not a
correct one.

**The recovered turn keeps its original id.** Recovery sets `turn_id` from the
row before resuming, so `grep turn=<id>` spans the crash and the recovery in
one trace rather than splitting into two unrelated halves.

Scope is owed Teams replies only. The Agents SDK's `MemoryStorage` stays
in-process; `teams.py`'s claim that persistence arrives "for both" is not part
of this decision.

## Rationale
- **It uses the checkpoint already paid for.** Measured turn cost is $0.009
  fresh and $0.036–0.052 on a long session. Replaying a turn from scratch
  re-pays that in full; resuming from the last super-step usually does not.
- **It keeps LangGraph narrow.** §6.1 limits the orchestrator to routing,
  checkpointing, interrupts and the recursion limit. A Teams
  `ConversationReference` in `GojoState` is none of those. The alternative —
  carrying the reference in graph state — also turns "which turns owe a reply"
  from a lookup into a scan of every thread's latest checkpoint, which is not
  what the checkpointer's `alist` is shaped for.
- **Retention is structural, not a routine.** §9.1 requires a ceiling at the
  moment a store is created. Rows are deleted on success and abandoned after
  three failed attempts, so the table only ever holds outstanding work. The
  audit trail stays in the structured logs, where `grep turn=<id>` already
  isolates a turn.
- **It is the same shape step 5 needs.** An idempotency key per side-effecting
  call is the write-path version of this record. Building the read-path version
  first is cheap and informs it.

## Consequences
- **Delivery becomes at-least-once, not exactly-once.** A crash between
  `deliver_reply` returning and the row being cleared re-sends the answer on
  the next start. The window is milliseconds and the cost is a duplicate
  message. Closing it needs the send and the delete in one transaction, which
  is not available across an HTTP call to Azure. Documented rather than hidden.
- **A race exists between recovery and a new message on the same thread.**
  Recovery reads the checkpoint after the service starts accepting traffic. If
  the user sends a new message on that thread first, the checkpoint advances and
  the rescued answer is lost — the status quo, not a regression. Blocking
  startup until recovery finished would remove the race, but could hold the
  service down for up to `graph_timeout_seconds` per owed turn against
  `Restart=always`.
- **An abandoned reply is silent to the user.** After three failed delivery
  attempts the row is dropped with a WARNING naming the turn id. The user is
  not told, because the reason delivery failed is that we cannot reach them.
- **`run_turn` gains a sibling, not a flag.** `resume_turn` passes `None`
  instead of an initial state and applies the same timeout and recursion limit,
  preserving `run_turn`'s rule that the §9.3 guards live at one surface.
- **Two connections open the same SQLite file.** LangGraph owns its schema, we
  own ours. At one user's write volume, contention is not a concern; if it ever
  becomes one, that is evidence for a decision, not a reason to pre-empt it.

## Related
- ADR 0006 — the two-part reply. This ADR closes the consequence it accepted.
- ADR 0007 — swapped steps 3 and 4, and listed this gap among those the swap
  addresses. It was not addressed; this ADR is the correction.
- §9.3 failure mode 2 (partial-failure loss) is the general case. Resumption
  from a checkpoint is the read-path half; idempotency keys at step 5 are the
  write-path half.

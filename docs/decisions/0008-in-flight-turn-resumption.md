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

That step became step 3 (ADR 0007), and step 3 completed without closing it.
ADR 0007 had listed the gap alongside the `nohup` problem as something the swap
brought forward; the systemd half landed, the resumption half did not.

Until this ADR the gap was recorded only as a consequence line inside an
accepted ADR, which is why it stayed quiet across two sessions.

Two things needed to finish an acknowledged turn live only in process memory:
the `asyncio` task computing the answer, and the `ConversationReference`
captured in `on_message` — the only route back into that conversation. A
restart loses both. The user is left holding "On it — give me a moment." and
receives nothing.

What the checkpointer provides is half the answer. `AsyncSqliteSaver` writes
after every super-step, so `ainvoke(None, config)` resumes a thread from its
last completed node, and a thread that had already finished returns its final
state without re-running anything. What the checkpoint cannot express is that a
human is owed a reply, or where to send it.

## Decision
Record owed replies in an **outbox table** in the existing
`checkpoints/gojo.sqlite`, written before the acknowledgement and deleted after
successful delivery. On startup, resume each owed thread from its checkpoint
and deliver.

```
owed_replies(
  turn_id       TEXT PRIMARY KEY,   -- the correlation id the logs already carry
  thread_id     TEXT NOT NULL,      -- Teams conversation id = LangGraph thread
  checkpoint_id TEXT NOT NULL,      -- the checkpoint this debt is pinned to
  reference     TEXT NOT NULL,      -- ConversationReference, serialised
  attempts      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
)
```

**The acknowledgement creates the debt.** Before the ACK there is no promise:
the turn dies, Azure Bot Service returns 504, and the user sees a failure
rather than silence. After it, a reply is owed.

**The debt is pinned to a checkpoint.** `checkpoint_id` is read from
`aget_state` at ACK time. Recovery compares it against the thread's current
checkpoint and **abandons the row without delivering** if they differ. While
the process is down nothing can advance a thread, so a difference means one
thing: the user sent a new message after the restart and has moved on. Without
this, resumption delivers a second copy of an answer they already have — see
Consequences.

**Recovery is silent.** A recovered answer arrives as the proactive reply
ADR 0006 already promised, with no "I restarted" preamble.

**Recovery runs after the app begins serving,** as a lifespan background task
rather than before `yield`.

**A resumed turn that fails is still answered.** If resumption raises, times
out, or yields no reply, the user receives the same failure message a live turn
produces, and the row is cleared. The promise was a reply, not a correct one.

**The recovered turn keeps its original id.** Recovery sets `turn_id` from the
row before resuming, so `grep turn=<id>` spans the crash and the recovery.

**`/new` and `/compact` need no special handling.** Both call `aupdate_state`,
which writes a new checkpoint and therefore moves the pin — so the same guard
that stops duplicate delivery also stops a discarded conversation being
answered. Verified, not assumed: after `/new` on a thread owing a reply, the
pin differs and recovery abandons the row. No connection is threaded through
the command path.

**Rows expire.** A row older than `owed_reply_max_age_seconds` (default 6
hours) is abandoned rather than delivered. An answer to a question asked before
a restart the user has long since forgotten is noise, not service.

Scope is owed Teams replies only. The Agents SDK's `MemoryStorage` stays
in-process; `teams.py`'s claim that persistence arrives "for both" is not part
of this decision.

## Rationale
- **It keeps LangGraph narrow.** §6.1 limits the orchestrator to routing,
  checkpointing, interrupts and the recursion limit. A Teams
  `ConversationReference` in `GojoState` is none of those. Carrying the
  reference in graph state would also turn "which turns owe a reply" from a
  lookup into a scan of every thread's latest checkpoint, which is not what the
  checkpointer's `alist` is shaped for.
- **Resuming is simpler than replaying, and never worse.** One call —
  `ainvoke(None, config)` — handles both "died mid-graph" and "died after
  finishing", with no branch and no need to store the original message. Replay
  would need both.
- **It sometimes saves an agent call, though less often than it first appears.**
  A crash *inside* `megumi` leaves the thread pending at `megumi`, so
  resumption re-runs it and the call is re-paid exactly as a replay would be.
  The saving applies only to a crash landing after `megumi` returned — a narrow
  window against a multi-second call, and the acknowledgement exists precisely
  because that call is slow. Real, but not the main argument. Turn costs are
  recorded in `build-log.md`.
- **Retention is structural, not a routine.** §9.1 requires a ceiling at the
  moment a store is created. Rows are deleted on success, abandoned after three
  failed attempts, abandoned when stale, and abandoned on `/new` — so the table
  only ever holds outstanding work. The audit trail stays in the structured
  logs, where `grep turn=<id>` already isolates a turn.
- **It is the same shape step 5 needs.** An idempotency key per side-effecting
  call is the write-path version of this record. Building the read-path version
  first is cheap and informs it.

## Consequences
- **Without the checkpoint pin, recovery would send duplicates.** Measured: run
  a fresh turn on a thread that owes a reply, and `ainvoke(None, config)`
  returns *that* turn's final state — so recovery would deliver the answer to
  the user's newest question a second time and clear the row as a success. The
  `checkpoint_id` guard exists specifically to prevent this. It is the reason
  the column exists, not an optimisation.
- **A narrow race remains.** If a new turn arrives after recovery reads the
  checkpoint id but before it resumes, the two invocations overlap on one
  thread. The window is small because `new_turn` is the graph's first node and
  checkpoints almost immediately, so a live turn is usually visible to the
  guard. It is not eliminated. Blocking startup until recovery finished would
  remove it, at the cost of holding the service down for up to
  `graph_timeout_seconds` per owed turn against `Restart=always`.
- **Delivery is at-least-once, not exactly-once.** A crash between
  `deliver_reply` returning and the row being cleared re-sends the answer on
  the next start. The window is milliseconds and the cost is a duplicate
  message. Closing it needs the send and the delete in one transaction, which
  is not available across an HTTP call to Azure.
- **An abandoned reply is silent to the user.** After three failed delivery
  attempts, or once stale, the row is dropped with a WARNING naming the turn
  id. The user is not told, because in the delivery-failure case the reason is
  that we cannot reach them.
- **`run_turn` gains a sibling, not a flag.** `resume_turn` passes `None`
  instead of an initial state and applies the same timeout and recursion limit,
  preserving `run_turn`'s rule that the §9.3 guards live at one surface.
- **Two connections open the same SQLite file.** LangGraph owns its schema, we
  own ours. Note that `AsyncSqliteSaver` sets `journal_mode=WAL` lazily — it is
  not yet in WAL when the outbox table is created at boot. Measured under the
  real lifespan ordering with a concurrent graph write: no contention.
- **`aiosqlite` becomes a declared dependency.** It was already resolved
  transitively via `langgraph-checkpoint-sqlite`, but §6.1's pin discipline
  applies: a module that imports it directly should not depend on another
  package's resolution.
- **`/health` reports the backlog.** A count that stays above zero across
  restarts means delivery is failing, not that turns are slow — §9.1's
  "capture needs a health signal", applied to this store.

## Open
- **A pending task survives `/new`.** `aupdate_state` clears `session_id` and
  `summary` but leaves the thread's pending node, so `aget_state().next` still
  names it. Deleting the owed row means nothing resumes it, which is sufficient
  for this ADR. What a *subsequent* message does on a thread with a stale
  pending task is untested and predates this work. Worth settling at step 5,
  when `interrupt()` makes pending state load-bearing.

## Related
- ADR 0006 — the two-part reply. This ADR closes the consequence it accepted.
- ADR 0007 — swapped steps 3 and 4 and brought this gap forward. Its systemd
  half landed; this is the other half.
- §9.3 failure mode 2 (partial-failure loss) is the general case. Resumption
  from a checkpoint is the read-path half; idempotency keys at step 5 are the
  write-path half.

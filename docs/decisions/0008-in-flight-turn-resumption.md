# ADR 0008 — Resume in-flight turns after a restart via an outbox table

**Status:** Accepted
**Date:** 2026-08-05

## Context
ADR 0006 answers every Teams turn in two parts: acknowledge inside the
channel's 10–15 second window, then deliver the answer proactively. Its
consequences record the cost:

> A turn in flight does not survive a restart. The acknowledgement is sent,
> the reply is not. Accepted at step 2; the checkpointer at step 4 is what
> makes resumption possible, and the gap should be closed there rather than
> papered over now.

That step became step 3 (ADR 0007), and step 3 completed without closing it.
ADR 0007 recorded the gap again in its own Context, alongside the `nohup`
problem; the systemd half landed, the resumption half did not. What neither
ADR did was put the gap anywhere the master document tracks open work, which
is why it stayed quiet across two sessions.

Two things needed to finish an acknowledged turn live only in process memory:
the `asyncio` task computing the answer, and the `ConversationReference` — the
only route back into that conversation. A restart loses both. The user is left
holding "On it — give me a moment." and receives nothing.

The checkpointer provides half the answer: `ainvoke(None, config)` resumes a
thread from its last completed node, and a completed thread returns its final
state without re-running anything. What the checkpoint cannot express is that
a human is owed a reply, or where to send it.

**This ADR reached its final form through four independent reviews**, each of
which found a real defect in the previous revision. The defects were all
interleavings of one underlying flaw — no per-conversation serialisation —
which is settled separately as **ADR 0009**. This design assumes ADR 0009's
invariant: at most one graph operation per conversation at a time.

## Decision
Record owed replies in an **outbox table** in the existing
`checkpoints/gojo.sqlite`, written before the acknowledgement and deleted
after successful delivery. On startup, resume each owed thread and deliver —
each row processed entirely inside its conversation's ADR 0009 lock.

```
owed_replies(
  turn_id    TEXT PRIMARY KEY,   -- the correlation id the logs already carry
  thread_id  TEXT NOT NULL,      -- Teams conversation id = LangGraph thread
  reference  TEXT NOT NULL,      -- ConversationReference, serialised
  attempts   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
)
```

**The acknowledgement creates the debt.** Before the ACK there is no promise:
the turn dies, Azure Bot Service returns 504, and the user sees a failure
rather than silence. After it, a reply is owed.

**The debt is matched to a turn, not to a checkpoint.** `new_turn` writes the
current turn id into `GojoState`. Recovery delivers only if
`values["turn_id"]` still equals the row's `turn_id`; a different id means a
newer turn owns the thread and this answer is stale. This handles the one
staleness case a lock cannot: the user moved on *between* boots, which is
sequential. (An earlier revision pinned to a `checkpoint_id` captured at ACK
time. That was wrong in the dangerous direction — see Evidence.)

**`/new` deletes the thread's owed rows.** A turn id survives `aupdate_state`,
so the guard cannot see a cleared conversation. Under ADR 0009 the ordering is
defined: a `/new` issued during a resume waits, the answer is delivered, then
the conversation is forgotten. `/compact` is deliberately not included — it
summarises and carries on, so the answer is still wanted.

**Recovery is silent.** A recovered answer arrives as the proactive reply
ADR 0006 already promised, with no "I restarted" preamble.

**Recovery runs after the app begins serving**, as a lifespan background task.
ADR 0009's locks are what make that safe.

**A resumed turn that fails is still answered.** If resumption raises or
yields no reply, the user receives the same failure message a live turn
produces (a timeout gets the timeout wording), and the row is cleared. The
promise was a reply, not a correct one.

**The recovered turn keeps its original id**, set from the row before
resuming, so `grep turn=<id>` spans the crash and the recovery.

**One bad row must not stop the pass.** Every row is handled inside its own
error boundary; a row that cannot be processed is logged and dropped.

**Rows expire.** A row older than `owed_reply_max_age_seconds` (default 6
hours) is abandoned rather than delivered.

Scope is owed Teams replies only. The Agents SDK's `MemoryStorage` stays
in-process.

## Evidence
Measured against langgraph 1.2.10 and the installed dependencies on
5 August 2026. Recorded here because a decision that rests on measurement
should carry it, and because two revisions of this ADR asserted consequences
that measurement then refuted.

- A crash inside `megumi` leaves `next == ('megumi',)`, and
  `ainvoke(None, config)` completes the turn from there.
- `ainvoke(None, config)` on a completed thread returns its final state,
  re-runs nothing, and writes no new checkpoints — repeated passes are
  idempotent.
- **`ainvoke(None, config)` on a thread that has since run a new turn returns
  the NEW turn's state.** Unguarded, recovery delivers the answer to the
  user's latest question a second time. This is why a staleness guard exists.
- **The checkpoint id moves during the owed turn's own progress** — at ACK
  time the thread sits at `megumi`, then writes checkpoints for `megumi` and
  `respond` as it completes. A checkpoint-id guard therefore abandons exactly
  the replies it exists to deliver, silently. This is why the guard is keyed
  on turn identity.
- **A turn id written by `new_turn` is stable across that turn's own progress
  and changes when a new turn starts** — measured across the background-task
  ordering `teams.py` actually uses, across a real process boundary, and
  under `kill -9`. Contextvar propagation from handler to graph node is exact,
  including under two interleaved turns. Resumption rehydrated from SQLite
  alone does not re-run `new_turn`.
- **A turn id is not changed by `aupdate_state`**, which is why `/new` needs
  explicit row deletion rather than relying on the guard.
- **A guard checked only before the resume was not enough** — `/new` landing
  mid-resume had its row deleted after the guard passed, and the discarded
  answer was delivered on top of "Fresh start". This, plus the discovery that
  the same interleaving corrupts the live path with no recovery code involved
  at all, is what produced ADR 0009. The races are now excluded by the lock
  rather than shrunk by re-checks.
- `ConversationReference` round-trips through `model_dump_json` at ~530
  bytes; the rehydrated continuation activity has `recipient` populated.
- A second `aiosqlite` connection to the checkpointer's file, with a
  concurrent graph write, showed no contention.

## Consequences
- **Delivery is at-least-once.** A crash between `deliver_reply` returning and
  the row being cleared re-sends the answer on the next start. The window is
  milliseconds and the cost is a duplicate message; the send and the delete
  cannot share a transaction across an HTTP call to Azure.
- **A delivery failure while the process stays up is not retried until a
  restart.** The outbox is drained at boot and nowhere else. The row survives,
  so the answer is not lost — it waits. In-process retry is deliberate future
  work.
- **The attempts ceiling counts delivery failures only.** A resume that
  crashes the process leaves its row at `attempts=0` and is retried each boot
  until the age ceiling drops it. Bounded, but the ceiling does not cover the
  failure mode its name suggests.
- **A turn that never reached the graph is not recoverable.** Under ADR 0009 a
  queued turn that dies waiting has checkpointed nothing; its row is abandoned
  by the guard with a WARNING — a promise not kept, logged rather than silent.
- **Recording the debt must never break the turn.** The write sits before the
  acknowledgement and is wrapped: a failed write is logged and the ACK is sent
  regardless. Losing the debt is strictly better than losing the turn.
- **`GojoState` gains one field.** A turn id is not a transcript and not a
  transport detail (§6.3 rule 3, §6.1), but it is state carried for recovery's
  benefit, and that is worth naming.
- **The outbox stores personal data** — display name, Entra object id, tenant
  id and service URL inside the serialised reference. It inherits the
  checkpoint file's handling; the expiry ceiling bounds how long it is kept.
- **`turn_id` is 32 bits of randomness** (`logs.py` takes 8 hex characters). A
  collision with an outstanding row would silently replace it. Not worth
  widening at one user; recorded so the assumption is visible.
- **`/chat` can touch a Teams thread if handed its conversation id.** The
  thread id on `/chat` is caller-chosen; a question there moves the turn id
  and an owed Teams reply is conservatively abandoned. Requires deliberately
  supplying a Teams conversation id to a curl endpoint on one's own box —
  documented, not defended against.
- **`run_turn` gains a sibling, not a flag.** `resume_turn` passes `None` and
  applies the same timeout and recursion limit (§9.3's one surface). Note the
  ADR 0009 lock is *not* inside either — see that ADR for why.
- **`aiosqlite` becomes a declared dependency** (§6.1 pin discipline for a
  directly-imported module).
- **`/health` reports the backlog.** A count that stays above zero across
  restarts means delivery is failing, not that turns are slow (§9.1's health
  signal).

## Open
- **A pending task survives `/new`.** `aupdate_state` clears the pointers but
  leaves the thread's pending node. Deleting the owed row means nothing
  resumes it, which is sufficient here. What a subsequent message does on a
  thread carrying a stale pending task is untested and predates this work —
  settle at step 5, when `interrupt()` makes pending state load-bearing.

## Related
- ADR 0006 — the two-part reply. This ADR closes the consequence it accepted.
- ADR 0007 — brought the gap forward; its systemd half landed, this is the
  other half.
- **ADR 0009 — the root cause found by this work's reviews.** Recovery runs
  entirely under its per-conversation locks.
- §9.3 failure mode 2 (partial-failure loss) is the general case: resumption
  is the read-path half, idempotency keys at step 5 the write-path half.

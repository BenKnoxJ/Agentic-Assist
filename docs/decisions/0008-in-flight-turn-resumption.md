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
ADR 0007 recorded the gap again in its own Context, alongside the `nohup`
problem, as something the swap brought forward. The systemd half landed; the
resumption half did not. What neither ADR did was put the gap anywhere the
master document tracks open work — not §11.0's debt list, not §13 — which is
why it stayed quiet across two sessions.

Two things needed to finish an acknowledged turn live only in process memory:
the `asyncio` task computing the answer, and the `ConversationReference`
captured in `on_message` — the only route back into that conversation. A
restart loses both. The user is left holding "On it — give me a moment." and
receives nothing.

The checkpointer provides half the answer. `AsyncSqliteSaver` writes after
every super-step, so `ainvoke(None, config)` resumes a thread from its last
completed node, and a thread that already finished returns its final state
without re-running anything. What the checkpoint cannot express is that a
human is owed a reply, or where to send it.

## Decision
Record owed replies in an **outbox table** in the existing
`checkpoints/gojo.sqlite`, written before the acknowledgement and deleted after
successful delivery. On startup, resume each owed thread and deliver.

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
current turn id into `GojoState`. Recovery reads the thread's state and
delivers only if `values["turn_id"]` still equals the row's `turn_id`; a
different id means a newer turn owns the thread and this answer is stale.

This is the corrected form of an earlier design that pinned to a
`checkpoint_id` captured at ACK time. That was wrong, and wrong in the
dangerous direction — see Evidence.

**`/new` deletes the thread's owed rows.** A turn id survives `aupdate_state`,
so the guard above cannot see a cleared conversation. A user who has told Gojo
to forget the conversation must not then be answered from it. `/compact` is
deliberately *not* included: it summarises and carries on, so the answer is
still wanted.

**Recovery is silent.** A recovered answer arrives as the proactive reply
ADR 0006 already promised, with no "I restarted" preamble.

**Recovery runs after the app begins serving,** as a lifespan background task
rather than before `yield`.

**A resumed turn that fails is still answered.** If resumption raises, times
out, or yields no reply, the user receives the same failure message a live turn
produces, and the row is cleared. The promise was a reply, not a correct one.

**The recovered turn keeps its original id.** Recovery sets `turn_id` from the
row before resuming, so `grep turn=<id>` spans the crash and the recovery.

**One bad row must not stop the pass.** Every row is handled inside its own
error boundary; a row that cannot be processed is logged and dropped.

**Rows expire.** A row older than `owed_reply_max_age_seconds` (default 6
hours) is abandoned rather than delivered. An answer to a question asked before
a restart the user has long since forgotten is noise, not service.

Scope is owed Teams replies only. The Agents SDK's `MemoryStorage` stays
in-process.

## Evidence
Measured against langgraph 1.2.10 and the installed dependencies on
5 August 2026. Recorded here because a decision that rests on measurement
should carry it.

- A crash inside `megumi` leaves `aget_state(config).next == ('megumi',)`, and
  `ainvoke(None, config)` completes the turn from there.
- `ainvoke(None, config)` on an already-completed thread returns its final
  state and re-runs nothing. Recovery needs no branch between the two cases.
- **`ainvoke(None, config)` on a thread that has since run a new turn returns
  the NEW turn's state.** Unguarded, recovery delivers the answer to the user's
  latest question a second time. This is why a guard is needed at all.
- **The checkpoint id moves during the owed turn's own progress.** Captured at
  ACK time the thread sits at `megumi`; it then writes checkpoints for `megumi`
  and `respond` as it completes. A checkpoint-id guard therefore abandons
  exactly the replies it exists to deliver, and fails silently. This is why the
  guard is keyed on turn identity instead.
- **A turn id written by `new_turn` is stable across that turn's own progress
  and changes when a new turn starts** — measured across the background-task
  ordering `teams.py` actually uses.
- **A turn id is not changed by `aupdate_state`**, which is why `/new` needs an
  explicit deletion rather than relying on the guard.
- `ConversationReference` round-trips through `model_dump_json` at ~530 bytes;
  the rehydrated continuation activity has `recipient` populated.
- A second `aiosqlite` connection to the checkpointer's file, under the real
  lifespan ordering and with a concurrent graph write, showed no contention.
- **A guard checked only before the resume is not enough.** The resume holds
  the thread for seconds while the app serves. Measured: `/new` landing in
  that window had its row deleted *after* the guard had already passed, the
  discarded answer was delivered on top of "Fresh start", and `/new`'s own
  `aupdate_state` was silently lost — its checkpoint forked from the same
  parent as the resume's, the resume's line won, and the resumed turn's
  `session_id` write resurrected the session the user had just discarded.
  Hence the pre-delivery re-check, and hence it must test row existence and
  turn id both: `/new` deletes the row but changes no turn id; a new message
  changes the turn id but deletes no row.
- The rest of the mechanism was probed adversarially and held: the turn id
  propagates correctly from the handler's contextvar through the background
  task into the graph node, including under two interleaved turns; the stamp
  is durably checkpointed well before the ACK and survives `kill -9`;
  resumption after a real process restart, rehydrated from SQLite alone, does
  not re-run `new_turn`; and repeated `ainvoke(None)` on a completed thread
  writes no new checkpoints.

## Consequences
- **Delivery is at-least-once, not exactly-once.** A crash between
  `deliver_reply` returning and the row being cleared re-sends the answer on
  the next start, because the turn id still matches. The window is milliseconds
  and the cost is a duplicate message. Closing it needs the send and the delete
  in one transaction, which is not available across an HTTP call to Azure.
- **A delivery failure while the process stays up is not retried until a
  restart.** The outbox is drained at boot and nowhere else. The row survives,
  so the answer is not lost, but it waits. An in-process retry is deliberate
  future work rather than an oversight.
- **Two turns acknowledged on one thread leave only the newer recoverable.**
  The graph keeps one state per thread, so the older turn's answer is gone. The
  older row is abandoned with a WARNING. Rare — it needs two slow turns back to
  back — but it is a promise that will not be kept, so it is logged as one.
- **A recovery pass holds a thread for as long as the resume takes**, up to
  `graph_timeout_seconds`, while the app is already serving. The guard is
  re-checked immediately before delivery, which shrinks the exposure to
  milliseconds but does not remove it: a message arriving mid-resume still
  produces two concurrent invocations of one thread, and their checkpoints
  fork — the losing line's state writes are silently discarded. That fork is
  not unique to recovery: `/new` racing a live in-flight turn has the same
  shape today. Recorded rather than fixed; per-thread serialisation is the
  real cure and belongs with step 5's gate work, where pending state becomes
  load-bearing. Blocking startup until recovery finished would remove the
  recovery half, at the cost of holding the service down for up to
  `graph_timeout_seconds` per owed row against `Restart=always`.
- **The attempts ceiling counts delivery failures only.** A resume that
  crashes the process leaves its row at `attempts=0` and is retried on every
  boot until the age ceiling drops it. Bounded by the six-hour expiry, but the
  ceiling does not cover the failure mode its name suggests.
- **Recording the debt must never break the turn.** The write sits before the
  acknowledgement, so a database error there would otherwise cost the user
  their answer *and* their acknowledgement. It is wrapped: a failed write is
  logged and the ACK is sent regardless. Losing the debt is strictly better
  than losing the turn.
- **`GojoState` gains one field.** A turn id is not a transcript and not a
  transport detail, so §6.3 rule 3 and §6.1 are unaffected — but it is state
  the orchestrator carries for recovery's benefit, and that is worth naming.
- **The outbox stores personal data** — display name, Entra object id, tenant
  id and service URL, inside the serialised reference. It inherits the
  checkpoint file's handling, and the expiry ceiling bounds how long it is
  kept.
- **`/chat` can touch a Teams thread if handed its conversation id.** The
  thread id on `/chat` is caller-chosen. A question moves the turn id and an
  owed Teams reply is conservatively abandoned; a `/new` via `/chat` clears
  no rows because that surface owes nothing. Both require deliberately
  supplying a Teams conversation id to a curl endpoint on one's own box —
  documented, not defended against.
- **`turn_id` is 32 bits of randomness** (`logs.py` takes 8 hex characters). A
  collision with an outstanding row would silently replace it. At one user with
  a handful of concurrent owed replies this is not worth widening; it is
  recorded so the assumption is visible.
- **`run_turn` gains a sibling, not a flag.** `resume_turn` passes `None`
  instead of an initial state and applies the same timeout and recursion limit,
  preserving `run_turn`'s rule that the §9.3 guards live at one surface.
- **`aiosqlite` becomes a declared dependency.** It was already resolved
  transitively via `langgraph-checkpoint-sqlite`, but §6.1's pin discipline
  applies to a module that imports it directly.
- **`/health` reports the backlog.** A count that stays above zero across
  restarts means delivery is failing, not that turns are slow — §9.1's
  "capture needs a health signal", applied to this store.

## Open
- **A pending task survives `/new`.** `aupdate_state` clears `session_id` and
  `summary` but leaves the thread's pending node. Deleting the owed row means
  nothing resumes it, which is sufficient here. What a *subsequent* message
  does on a thread carrying a stale pending task is untested and predates this
  work. Worth settling at step 5, when `interrupt()` makes pending state
  load-bearing.

## Related
- ADR 0006 — the two-part reply. This ADR closes the consequence it accepted.
- ADR 0007 — swapped steps 3 and 4 and brought this gap forward. Its systemd
  half landed; this is the other half.
- §9.3 failure mode 2 (partial-failure loss) is the general case. Resumption
  from a checkpoint is the read-path half; idempotency keys at step 5 are the
  write-path half.

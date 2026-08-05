# ADR 0009 — Serialise all graph operations per conversation

**Status:** Accepted
**Date:** 2026-08-05

## Context
Four independent reviews of ADR 0008 each found a real defect, and every one
was a concurrency defect, not a persistence one: the checkpoint advancing
under a pin, `/new` racing a recovery resume, a new message racing a resume,
checkpoint histories forking. Each was patched individually — pin, then
turn-id guard, then a pre-delivery re-check — and each patch surfaced the
next interleaving. A growing consequences list is the signature of a design
fighting its architecture rather than fixing it.

The root cause was then established by measurement against the **live system
as deployed, with no ADR 0008 code involved**:

- `/new` issued while a slow turn is in flight is **silently reverted** — the
  turn's final state writes land after `aupdate_state`'s checkpoint, so the
  session the user discarded survives, and "Fresh start" is a lie.
- Two overlapping messages on one conversation **fork the checkpoint
  history** — two checkpoints sharing one parent — with the losing line's
  state writes discarded.

A LangGraph thread is a single-writer structure. `teams.py` runs turns as
background tasks (ADR 0006), commands call `aupdate_state` directly, and
nothing serialises them. One user sending one message at a time is what has
kept this latent. Recovery (ADR 0008) adds a guaranteed second writer at boot,
which is why its reviews kept hitting shadows of this flaw.

## Decision
**At most one graph operation runs per conversation at a time, process-wide.**

An `asyncio.Lock` per thread id, held for the whole critical section of each
operation:

- a live turn (`teams.py`'s `_run_graph`) — the `run_turn` call
- a `/chat` turn (`api.py`) — the `run_turn` call
- a command (`commands.handle`) — the state read/update sequence
- a recovery row (`recovery.py`) — guard check, resume, deliver, clear, as
  one section

The registry (`lock_for(thread_id)`) lives in `orchestrator.py`: §6.1 gives
the orchestrator control flow, and this is control flow.

**The lock is acquired at call sites, not inside `run_turn`.** `asyncio.Lock`
is not re-entrant, and the critical sections have different spans — recovery
must hold the lock across check-resume-deliver-clear, which contains a
`resume_turn` call. A lock inside `run_turn`/`resume_turn` would deadlock the
caller that needs the wider span. This is a deliberate exception to the §9.3
"guards live at one surface" pattern, and the reason is recorded here so it
is not "fixed" back.

## Consequences
- **The live `/new`-revert bug is fixed.** `/new` now waits for the in-flight
  turn, so the ordering is defined: answer arrives, then the conversation is
  forgotten. Same for `/compact`.
- **Overlapping messages on one conversation queue instead of forking.** The
  second turn's graph run waits; its fast-reply budget still expires on time,
  so the user gets an acknowledgement and a proactive answer (ADR 0006's slow
  path) rather than a corrupted thread.
- **Worst-case wait on one conversation is `graph_timeout_seconds`** (180s)
  behind a hung turn. Different conversations are unaffected. At one user
  this is acceptable; it is the price of the invariant.
- **A queued turn that never reached the graph is not recoverable.** If the
  process dies while turn B waits behind turn A, nothing of B was ever
  checkpointed; its owed row (if acknowledged) is abandoned by ADR 0008's
  guard with a WARNING. The promise-not-kept is logged, not silent.
- **Single event loop only.** The registry is process-local. §4.3's
  single-worker decision is what makes this sound; if that ever changes, this
  ADR must be revisited first.
- **The lock dictionary grows with distinct thread ids.** Unbounded in
  principle; in practice bounded by conversations one user opens. Recorded so
  the assumption is visible.
- ADR 0008's recovery collapses: the pre-delivery re-check and its residual
  race windows disappear, because `/new` and new messages can no longer
  interleave with a resume at all. Its staleness guard (turn id) remains — the
  user moving on *between* boots is sequential, and no lock addresses it.

## Related
- ADR 0006 — background-task turns are what made the live path racy.
- ADR 0008 — the work whose reviews exposed this; its recovery runs entirely
  under these locks.

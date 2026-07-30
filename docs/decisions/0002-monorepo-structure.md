# ADR 0002 — Single monorepo for the whole estate

**Status:** Accepted
**Date:** 2025-06-17

## Context
The system will grow to an orchestrator (Gojo) + multiple connector agents + shared code, prompts, evals, infra. One repo with many packages, or many repos.

## Decision
Single private monorepo (Gojo-multi-agent-os) with a packages/ layout, one self-contained package per agent.

## Rationale
- Easier to present as a single portfolio piece — one repo, one story.
- Shared code (auth, types) simpler to factor out within one repo.
- "Estate as one clean repo" is an explicit project goal.
- packages/ makes reference-then-clone clean: build Outlook fully, replicate its shape.

## Consequences
- All agents version together; a connector can split out later if needed.
- Discipline required to keep packages self-contained.
- shared/ only receives code once a pattern actually repeats — avoids premature abstraction.

# ADR 0005 — Pin the asyncio event loop, do not run under uvloop

**Status:** Accepted
**Date:** 2026-08-04

## Context
The FastAPI surface runs under Uvicorn. `uvicorn[standard]` installs uvloop and selects it by default, so the HTTP path ran on a different event loop from the direct `asyncio.run` used during earlier development. Under uvloop every Claude Agent SDK call through Megumi failed with `Claude Code returned an error result: Reached maximum number of turns (1)`, while the same graph invoked directly succeeded.

Measured on the same code, same `.env`, same machine:

| Event loop | Result |
|---|---|
| uvloop (Uvicorn default) | 3/3 requests failed |
| asyncio (`--loop asyncio`) | 3/3 requests succeeded |
| asyncio (`python -m gojo.orchestrator`) | succeeded |

## Decision
Pin `loop="asyncio"` in the server entrypoint, `apps/gojo/src/gojo/__main__.py`. Keep the `uvicorn[standard]` extra; override only the loop.

## Rationale
- The failure is deterministic and loop-dependent: 3/3 against 3/3 on otherwise identical requests.
- uvloop's throughput advantage is not spendable here. One physical core, I/O-bound workload — the process is waiting on Claude and, later, on Graph and Jira (§3.1, §4.3).
- Pinning in code rather than on a command line means step 4's systemd unit cannot silently reintroduce uvloop.
- The symptom names turns, not the event loop, so the failure reads as an agent or `max_turns` problem. Recording the real cause here is worth more than the fix itself.

## Consequences
- The server must be started via `python -m gojo`. A bare `uvicorn gojo.api:app` reintroduces the bug.
- The systemd unit at build step 4 must invoke the module entrypoint, not `uvicorn` directly.
- `uvicorn[standard]` is retained for httptools and websockets; only the loop selection changes.
- **Root cause is not established.** The Agent SDK runs the bundled Claude Code CLI as a subprocess and reads it over anyio streams, so uvloop's subprocess handling is the plausible mechanism — but that is inference, not something this project verified. What is verified is the loop dependency. Revisit if the SDK changes its transport, and treat any future "maximum number of turns" error as a loop question first.

# ADR 0004 — Python as the application runtime

**Status:** Accepted
**Date:** 2026-08-04

## Context
ADR 0001 chose Bun/TypeScript when the plan was a Teams bot bridging to a Claude Code session. The architecture has since changed: LangGraph is now the orchestrator and the Claude Agent SDK is the execution layer. That makes the runtime question a different question than the one ADR 0001 answered.

## Decision
Use Python 3.12 as the application runtime, with uv for packages and environments. Supersedes ADR 0001.

## Rationale
- LangGraph's primary implementation is Python; the JS port trails it.
- The deferred retrieval and evaluation layer (§12) is Python-first.
- Python is the stronger career signal for AI engineering roles.
- Migration cost was ~one file of application code.

## Consequences
- Bun and Node remain installed on the VPS but are not the application runtime.
- packages/ is replaced by apps/ + libs/ (uv workspace).
- PEP 668 on Ubuntu 24.04 means no system-wide pip; uv handles this.
- ADR 0001 is superseded, not deleted.

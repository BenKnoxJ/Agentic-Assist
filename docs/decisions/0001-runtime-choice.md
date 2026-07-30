# ADR 0001 — Bun as primary runtime, Node as compatibility layer

**Status:** Accepted
**Date:** 2025-06-17

## Context
The system needs a JS/TS runtime to execute Gojo and the connector MCP servers. Realistic options: Bun and Node.js.

## Decision
Use Bun as the primary runtime; install Node 20 LTS alongside as a compatibility layer.

## Rationale
- Bun runs TypeScript directly — no compile step, fewer moving parts.
- Fast startup matters with Gojo + several connector processes.
- The Teams bridge blueprint is written for Bun — aligned with a proven design.
- Node retained: Claude Code and some npm tooling assume Node; some libraries are Node-first.

## Consequences
- Two runtimes to keep updated (low cost).
- Node is the fallback if a library misbehaves under Bun, no re-architecting.
- Start scripts target Bun; document any component that must run under Node.

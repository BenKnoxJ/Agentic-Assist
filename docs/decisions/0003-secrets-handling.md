# ADR 0003 — Secrets stay out of git; build the wall first

**Status:** Accepted
**Date:** 2025-06-17

## Context
The system holds bot credentials, API tokens, .env files. Repo is private now, public-bound later. A secret in git history is painful to remove and a real incident for a public repo.

## Decision
Create a comprehensive .gitignore before any secret-bearing file exists; run git status before every commit. Secrets live only on the host (mode 0600), never in the repo.

## Rationale
- Correct order: build the wall before there's anything to leak.
- .gitignore blocks .env, *.pem, *.key, *-secret*, credentials.json, state dirs.
- git status before commit is a cheap repeatable check against stray secrets.
- Mirrors the blueprint security model: state at 0600, secrets never committed.

## Consequences
- A small repeated verification step before each commit.
- Secrets provisioned on the host out-of-band, not via the repo.
- When sanitising to public, history is already clean.

## Related
- GitHub auth uses an on-host SSH key (private key never leaves the VPS) — same keep-secrets-on-host principle.

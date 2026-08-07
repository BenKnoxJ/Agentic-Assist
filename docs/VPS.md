# VPS operations

How to run, inspect and recover this box. **Operational only** — architecture
and build sequence live in [GOJO-MASTER.md](GOJO-MASTER.md), decisions in
[decisions/](decisions/), history in [build-log.md](build-log.md). §18 of the
master document owns the rule about which document owns which fact.

## The box

| | |
|---|---|
| Host | Azure VPS, Ubuntu 24.04 |
| CPU | 2 vCPU = **1 physical core**, hyperthreaded |
| RAM | 7.7 GiB, 4 GiB swap |
| Public name | `cc-sfq4tema454pu.uksouth.cloudapp.azure.com` |
| App user | `ccuser` |
| Repo | `/home/ccuser/Agentic-Assist` |

One physical core is the constraint behind most architectural choices —
single Uvicorn worker, no Docker, hosted rather than local embeddings.

## Services

| Service | Role | Port |
|---|---|---|
| `caddy` | TLS termination, reverse proxy | 80, 443 |
| `gojo` | The application | 127.0.0.1:3000 |

Caddy proxies the whole host to `localhost:3000`; there is no per-path
config, so new routes need no proxy change.

## Everyday commands

```bash
systemctl status gojo            # is it up
sudo systemctl restart gojo      # after a code change
journalctl -u gojo -f            # follow logs
journalctl -u gojo | grep turn=abc12345   # one complete turn, SDK lines included
curl -s localhost:3000/health    # liveness + teams state + turns in flight
```

`/health` returns `teams: enabled|disabled`, `turns_in_flight` and
`owed_replies` (ADR 0008 — steady state 0; a count that stays above 0 across
restarts means proactive delivery is failing, not that turns are slow). A process
that is up but not listening to Teams looks identical from outside
otherwise.

## Deploying a change

```bash
cd ~/Agentic-Assist
git pull                              # if changed elsewhere
uv sync                               # only if dependencies moved
LANGSMITH_TRACING=false uv run pytest -q
uv run ruff check .
sudo systemctl restart gojo
```

Tracing is forced off for local test runs (§9.2 makes that mandatory) and
forced **on** for the service, via `Environment=` in the unit overriding
`.env`. That way real traffic is traced and build iteration is not.

## Things that will bite

**Never start the app with `uvicorn` directly.** `python -m gojo` pins
`loop="asyncio"`; uvicorn selects uvloop, under which every Agent SDK call
fails with *"Reached maximum number of turns"* — which reads as an agent
fault, not an event-loop one. **ADR 0005.**

**Do not set `ANTHROPIC_API_KEY` anywhere** — not `.env`, not a shell
profile, not the unit. It outranks the subscription token and silently
moves billing to API rates. The app asserts it is unset at boot and refuses
to start.

**Do not add `ProtectHome` to the unit.** The Agent SDK reads Claude Code's
stored subscription credentials from `/home/ccuser`. Hiding it breaks auth.

**`.env` is mode 0600 and has no trailing-newline guarantee.** Append with a
tool that adds one — a bare `printf` will concatenate onto the last value
and corrupt whatever key sits there.

## Secrets

Live only in `/home/ccuser/Agentic-Assist/.env`, mode `0600`, gitignored
along with `.env.*`. Never in the repo, never in the journal.

| Key | Purpose |
|---|---|
| `LANGSMITH_*` | Tracing, EU region. ⚠ `LANGSMITH_TRACING` here is what the *service* runs with — `EnvironmentFile` beats the unit's `Environment=` (build-log Session 5). Keep it `true` |
| `TEAMS_CLIENT_ID` / `TEAMS_TENANT_ID` / `TEAMS_CLIENT_SECRET` | Bot auth |
| `ALLOWED_USER_IDS` | Entra object IDs permitted to use Gojo |
| `GRAPH_CLIENT_ID` / `GRAPH_TENANT_ID` / `GRAPH_CLIENT_SECRET` / `GRAPH_OWNER_UPN` | Mail connector, app-only. Mailbox scope lives in Exchange RBAC, not here |
| `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN` | Jira connector, delegated as the owner |

**Client secret expiry is a recorded liability** (§10 property 5). When it
rotates, update `.env` and restart — nothing else changes.

| Credential | Expiry | Revoke/rotate at |
|---|---|---|
| Teams/Graph client secret (shared app registration) | **~June 2027** | Entra → App registrations → benkn-gojo-agent-orchestrator → Certificates & secrets |
| Jira API token `gojo` | none (revocable) | id.atlassian.com → Security → API tokens |

## State

| Path | Contents | Backed up? |
|---|---|---|
| `checkpoints/gojo.sqlite` | Conversation state, one thread per Teams chat, plus the `owed_replies` outbox (ADR 0008 — rows carry sender name, Entra object id and tenant id inside the serialised reference) | ✅ Daily 03:00, `gojo-backup.timer` |
| `.env` | Secrets | Not yet — recreate from the portal |

Both are gitignored. Losing the checkpoint file loses conversation history
and any not-yet-delivered answers,
not the application.

**Backups:** `gojo-backup.timer` runs `infra/backup-checkpoints.py` daily at
03:00 (`Persistent=true`, so a missed run fires at next boot). It takes an
online SQLite snapshot — never `cp`, which mid-write produces a corrupt copy
that looks fine — integrity-checks it, and keeps the newest 14 in
`~/backups/gojo/`. ⚠ Same disk as the live file: this protects against
corruption and accidental deletion, not against losing the VPS.

## Recovery

**Service will not start:** `journalctl -u gojo -n 50`. Most likely an
`.env` problem — the boot-time auth assertion fails loudly on purpose.

**Teams says `teams: disabled`:** one of the three `TEAMS_*` values is
missing or misnamed. There is no prefix; the names are exactly as above.

**Every message refused:** `ALLOWED_USER_IDS` is empty or wrong. Send one
message and read the refusal line — it logs the sender's object ID at
WARNING precisely so it can be added without hunting through the portal.

**Full rebuild:** clone the repo, restore `.env`, `uv sync`, copy
`infra/gojo.service` to `/etc/systemd/system/`, `daemon-reload`,
`enable --now`.

## Build environment (Plane A)

The tooling used to *build* Gojo is deliberately separate from what Gojo
*runs*. `setting_sources=[]` in the runner means Gojo's agents inherit
nothing from the host Claude Code environment — no CLAUDE.md, no settings,
no skills.

Installed for building, not for the runtime: `claude-mem` and `superpowers`
plugins (user scope). They add ~1.8k tokens per session and take effect on
the next session start.

**The three couplings to guard** (§18): Claude Code's subscription
credentials serve both, the single core is shared so dev tooling must be
invocable rather than resident, and build-session memory must never merge
with Gojo's `knowledge/` corpus — one is development history, the other
carries business mail.

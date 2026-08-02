# Zagent

An ADO-native autonomous engineering platform for the HAMI fleet. Zagent takes a
task, plans it, runs Claude agents in isolated per-repo sandboxes, streams every
step to a live console, and opens the pull request — with a human in the loop at
whatever depth you choose.

The unit of work is a **run**. A run owns one or more **lanes** (each a container
running the Claude Agent SDK against one repo), moves through a fixed **stage**
machine, and exposes exactly the actions that are legal right now. Everything a
lane does arrives as an event on a Redis stream, lands in Postgres, mirrors to a
JSONL transcript, and fans out over a WebSocket to the browser.

---

## Architecture

```mermaid
flowchart LR
  Web[React PWA<br/>sessions console] -->|REST + WS| BE[FastAPI control plane]
  BE --> PG[(Postgres)]
  BE <-->|streams| RD[(Redis)]
  BE -->|docker.sock| Lane[Lane containers<br/>Claude Agent SDK]
  Lane -->|events| RD
  Lane -->|LLM only| GW[LiteLLM gateway<br/>virtual keys + budgets]
  GW --> Foundry[Azure AI Foundry]
  BE --> ADO[Azure DevOps<br/>repos, PRs, work items]
```

Lane containers sit on an **internal-only** Docker network. Their sole routes out
are the gateway (for LLM calls) and Redis (for events and control). They cannot
reach the internet, and they never hold a long-lived credential: the per-lane
gateway key carries its own budget, and the ADO PAT is injected at container
start through a git credential helper so it never touches `.git/config`.

## Repo layout

| Path | What lives there |
|------|------------------|
| `backend/` | FastAPI control plane — API, orchestrator, event bus, sandbox manager, ADO + gateway adapters |
| `worker/` | Lane runtime: Claude Agent SDK loop, tool-permission bridge, session fork/resume |
| `apps/web/` | React PWA — the sessions console, knowledge, ideas, patrol, costs, repos, team |
| `packages/contracts/` | Shared Pydantic contracts (run stages, intents, step events) |
| `packages/maps/`, `packages/maps-ts/` | Repo map generators (Python + TS) |
| `infra/` | Local Compose, `infra/vm/` production kit, k8s scaffold, LiteLLM + squid configs |
| `fleet-config/` | Fleet service graph and repo seeds |
| `scripts/` | Seeding, validation, and the git credential helper |

Python is a **uv workspace** (`backend`, `worker`, `packages/contracts`,
`packages/maps`); the web app is a separate npm project.

---

## Quick start (local, no Docker)

The defaults are deliberately self-contained: SQLite on disk and an in-process
fake Redis, so the backend boots with zero services running.

```bash
uv sync --package zagent-backend --all-extras

cd backend
uv run alembic upgrade head
uv run python -m app.auth.seed_users     # modes, playbooks, first admin
uv run uvicorn app.main:create_app --factory --port 8000
```

In a second terminal:

```bash
cd apps/web
npm install
npm run dev        # http://localhost:5173, proxies API + WS to :8000
```

Sign in with `ZAGENT_BOOTSTRAP_ADMIN_USERNAME` / `ZAGENT_BOOTSTRAP_ADMIN_PIN`
(defaults `sahil` / `4545` — override both before any shared deployment).

Runs will reach the point of needing a real LLM gateway and ADO credentials; the
UI, auth, and run metadata all work without them.

## Full stack (Docker Compose)

```bash
cd infra
cp .env.example .env      # fill LITELLM_MASTER_KEY, GATEWAY_DB_PASSWORD, Foundry key
docker compose up -d      # backend on :8000
```

This runs Redis, the LiteLLM gateway and its Postgres, and the backend on
SQLite. Migrations are **not** automatic here — run `alembic upgrade head`
yourself. Lane spawning is unavailable (no Docker socket); use the VM kit for
that.

## Production (Ubuntu VM)

`infra/vm/` is the production-shaped deployment: app Postgres, Redis with AOF,
the gateway, a one-shot migrate+seed job, the backend, and nginx serving the
built SPA behind a single origin.

```bash
cd infra/vm
cp .env.example .env       # every :?-marked var must be filled or deploy aborts
./deploy.sh                # builds images, migrates, seeds, starts the stack
./deploy.sh tailscale      # optional: tailnet-only HTTPS access
```

Reachable at `http://<vm>:8080`, or `https://zagent.<tailnet>.ts.net` with the
Tailscale profile.

### Data root — read this before changing volumes

All durable state lives under `ZAGENT_DATA_ROOT` (default `/srv/zagent`):
`golden/`, `sessions/`, `workspaces/`, `transcripts/`, `evidence/`. Back up that
one directory plus a Postgres dump and you have the whole system.

These are host bind mounts deliberately mapped to the **same path inside the
backend container**, and that parity is load-bearing rather than stylistic. The
backend spawns lanes as sibling containers through `/var/run/docker.sock`, so
the bind sources it hands the daemon are resolved in the *host* namespace. If
the backend wrote to a named volume at `/golden`, the daemon would look for
`/golden` on the host, not find it, silently create an empty directory, and
every lane would mount an empty repo with no error anywhere. Keep host path and
container path identical for anything a lane mounts.

---

## Configuration

Settings live in `backend/app/core/config.py` and take a `ZAGENT_` env prefix.
The ones that matter most:

| Variable | Default | Notes |
|----------|---------|-------|
| `ZAGENT_DB_URL` | `sqlite:///./data/zagent.db` | Postgres in the VM stack |
| `ZAGENT_REDIS_URL` | `memory://0` | In-process fake; set a real URL in any deployment |
| `ZAGENT_JWT_SECRET` | `dev-only-change-me` | **Must** be overridden |
| `ZAGENT_BYO_PAT_ENCRYPTION_KEY` | `dev-only-byo-pat-key` | At-rest key for user PATs |
| `ZAGENT_BOOTSTRAP_ADMIN_PIN` | `4545` | First admin, created once by the seed |
| `FETCH_PAT` / `FLEET_PAT` | empty | ADO: read-only fetcher, and read/write for pushes and PRs |
| `ZAGENT_ADO_WEBHOOK_SECRET` | empty | Empty means webhook ingress rejects everything (fail-closed) |
| `ZAGENT_DEFAULT_LANE_BUDGET_USD` | `5.0` | Enforced by the gateway virtual key |
| `ZAGENT_GLOBAL_LANE_CAP` | `12` | Concurrent lanes across the whole system |
| `ZAGENT_APPROVAL_TIMEOUT_SECONDS` | `900` | After this the worker denies and the card expires |

## Modes and autonomy

Four seeded modes — **ask** (read-only investigation), **plan**, **development**,
and **debug** — each with its own persona, topology, and SKILL.md playbooks.

Autonomy is a separate dial, and it earns its way up:

- **supervised** — every write, bash, and git action raises an approval card.
- **gated** — file edits proceed; bash, git, and MCP mutations still ask.
- **autonomous** — nothing is bridged. Unlocked only after a track record of
  completed gated runs.

Approval cards are not open-ended. The worker blocks for
`approval_timeout_seconds`, then denies deterministically; the backend expires
the row in step so the console never shows a button that does nothing.

## Testing

```bash
cd backend && uv run pytest          # ~677 tests, 90% coverage gate
cd apps/web && npm test              # vitest
cd apps/web && npm run build         # typecheck + production build
```

Tests are mock-only per the HAMI testing standard: no live ADO, gateway, Docker,
or Redis. `memory://` Redis and SQLite make the suite hermetic.

## Known gaps

Honest about what isn't built yet:

- **Prewarm pool** is specified and stubbed; lane spawn is cold.
- **Repo maps** aren't generated, so onboarded repos sit at `ready-no-map`.
- **Playwright evidence** stamps paths but never captures screenshots.
- **Push notifications** need `pywebpush` plus VAPID keys to actually send.
- **Retention** (`session_retention_days`, `events_ttl_months`) is configured and
  the purge helpers exist, but nothing schedules them yet.
- **No rate limiting** on the API; concurrency is bounded only by the lane cap.
- **`infra/k8s/`** is a scaffold — no database, gateway, web, or secrets
  manifests. The VM kit is the supported deployment.

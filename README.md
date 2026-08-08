# Collegium

An ADO-native autonomous engineering platform — org-agnostic and adoptable
against any set of repositories. Collegium takes a task, plans it, runs LLM agents
in isolated per-repo sandboxes, streams every step to a live console, and opens
the pull request — with a human in the loop at whatever depth you choose.

Collegium is a [Collegium Labs](https://collegiumlabs.com) product.

The unit of work is a **run**. A run owns one or more **threads** (each a
container running Collegium's custom agent engine against one repo), moves through
a fixed **stage** machine, and exposes exactly the actions that are legal right
now. Everything a thread does arrives as an event on a Redis stream, lands in
Postgres, mirrors to a JSONL transcript, and fans out over a WebSocket to the
browser.

---

## Architecture

```mermaid
flowchart LR
  Web[React PWA<br/>sessions console] -->|REST + WS| BE[FastAPI control plane]
  BE --> PG[(Postgres)]
  BE <-->|streams| RD[(Redis)]
  BE -->|docker.sock| TH[Thread containers<br/>custom LangGraph engine]
  TH -->|StepEvents + heartbeats| RD
  RD -->|nudges, approvals, control| TH
  TH -->|LLM only| GW[LiteLLM gateway<br/>virtual keys + budgets]
  GW --> LLM[LLM providers]
  BE --> ADO[Azure DevOps<br/>repos, PRs, work items]
```

Thread containers sit on an **internal-only** Docker network. Their sole routes
out are the gateway (for LLM calls) and Redis (for events and control). They
cannot reach the internet, and they never hold a long-lived credential: the
per-thread gateway key carries its own budget, and the ADO PAT is injected at
container start through a git credential helper so it never touches
`.git/config`.

## The engine (`worker/worker/engine/`)

Threads run Collegium's **own harness** — a LangGraph StateGraph with
interrupt-driven approvals, not a vendor SDK loop. (The legacy Claude Agent SDK
runtime still exists behind `COLLEGIUM_ENGINE_RUNTIME=sdk` as a fallback through
the RE soak; `custom` is the default since the RB cutover.) What the custom
engine gives you:

- **Two-tier tool surface** — 8 tools bound every turn (`file_read`,
  `file_edit`, `file_write`, `terminal_exec`, `code_search`, `file_glob`,
  `update_tasks`, `tool_search`); everything else (`web_fetch`, `web_search`,
  `git_snapshot`, `compact`, `knowledge.draft`, `file_delete`, `terminal_await`,
  `playbook_load`, `mode_request`, MCP tools) is discovered at runtime via
  `tool_search` and checkpointed in `discovered_tools`.
- **Fail-closed mode filters** — ask / plan / development / debug each bind
  only their legal tool set, and denied tools are absent from both the binding
  and the discovery index.
- **A real approval gate** — glob rulesets (allow/ask/deny with findLast
  precedence), verbatim args on the card, edit-and-resend, deterministic deny
  on timeout, and destructive-tool pairing (`action_id` links request ↔
  decision in the event stream).
- **Security boundaries in-band** — secrets redaction, typed
  `<untrusted_content>` quarantine markers on fetched content, and a
  diagnostics hook that feeds lint errors back on `file_write`.
- **Ops primitives** — in-process metrics registry, context compaction (auto
  and agent-triggered), stuck-loop watchdogs, background-terminal contract
  (ring buffer, regex watch), and goal-stage recaps emitted as first-class
  StepEvents.
- **Durable state** — Postgres checkpointer when `COLLEGIUM_ENGINE_DATABASE_URL`
  is set (falls back to in-memory with a loud warning), so interrupted runs
  resume with approvals intact.

## Repo layout

| Path | What lives there |
|------|------------------|
| `backend/` | FastAPI control plane — API, orchestrator, event bus, sandbox manager, ADO + gateway adapters |
| `worker/` | Thread runtime: the custom engine (graph, tools, approvals, permissions, metrics, MCP) plus the Redis control/heartbeat loop |
| `apps/web/` | React PWA — the sessions console (15 card kinds from live StepEvents), swarm view, knowledge, ideas, patrol, costs, repos, team |
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
uv sync --package collegium-backend --all-extras

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

Sign in with `COLLEGIUM_BOOTSTRAP_ADMIN_USERNAME` / `COLLEGIUM_BOOTSTRAP_ADMIN_PIN`
(defaults `sahil` / `4545` — override both before any shared deployment).
Teammates join without a shared password: an admin generates a per-user setup
code on the team screen, and the new user claims it at `/first-login` with
their own pin.

Runs will reach the point of needing a real LLM gateway and ADO credentials; the
UI, auth, and run metadata all work without them.

## Full stack (Docker Compose)

```bash
cd infra
cp .env.example .env      # fill LITELLM_MASTER_KEY, GATEWAY_DB_PASSWORD, provider keys
docker compose up -d      # backend on :8000
```

This runs five services: Redis, the LiteLLM gateway, the gateway's Postgres, the
backend on SQLite, and an optional squid package proxy. Migrations are **not**
automatic here — run `alembic upgrade head` yourself. Thread spawning is
unavailable (no Docker socket); use the VM kit for that.

## Production (Ubuntu VM)

`infra/vm/` is the production-shaped deployment: app Postgres, Redis with AOF,
the gateway, a one-shot migrate+seed job, the backend, and nginx serving the
built SPA behind a single origin.

```bash
cd infra/vm
cp .env.example .env       # every :?-marked var must be filled or deploy aborts
./deploy.sh                # builds images, migrates, seeds, starts the stack
```

Reachable at `http://<vm>:8080`.

### Data root — read this before changing volumes

All durable state lives under `COLLEGIUM_DATA_ROOT` (default `/srv/collegium`):
`golden/`, `sessions/`, `workspaces/`, `transcripts/`, `evidence/`. Back up that
one directory plus a Postgres dump and you have the whole system.

These are host bind mounts deliberately mapped to the **same path inside the
backend container**, and that parity is load-bearing rather than stylistic. The
backend spawns threads as sibling containers through `/var/run/docker.sock`, so
the bind sources it hands the daemon are resolved in the *host* namespace. If
the backend wrote to a named volume at `/golden`, the daemon would look for
`/golden` on the host, not find it, silently create an empty directory, and
every thread would mount an empty repo with no error anywhere. Keep host path
and container path identical for anything a thread mounts.

---

## Configuration

Settings live in `backend/app/core/config.py` and take a `COLLEGIUM_` env prefix.
The ones that matter most:

| Variable | Default | Notes |
|----------|---------|-------|
| `COLLEGIUM_DB_URL` | `sqlite:///./data/collegium.db` | Postgres in the VM stack |
| `COLLEGIUM_REDIS_URL` | `memory://0` | In-process fake; set a real URL in any deployment |
| `COLLEGIUM_JWT_SECRET` | `dev-only-change-me` | **Must** be overridden |
| `COLLEGIUM_BYO_PAT_ENCRYPTION_KEY` | `dev-only-byo-pat-key` | At-rest key for user PATs |
| `COLLEGIUM_BOOTSTRAP_ADMIN_PIN` | `4545` | First admin, created once by the seed |
| `COLLEGIUM_FETCH_PAT` / `COLLEGIUM_FLEET_PAT` | empty | ADO: read-only fetcher, and read/write for pushes and PRs |
| `COLLEGIUM_ADO_WEBHOOK_SECRET` | empty | Empty means webhook ingress rejects everything (fail-closed) |
| `COLLEGIUM_DEFAULT_THREAD_BUDGET_USD` | `5.0` | Enforced by the gateway virtual key |
| `COLLEGIUM_GATEWAY_MODEL` | `kimi-k2.6` | Default model when a run doesn't pick one |
| `COLLEGIUM_GATEWAY_UI_URL` | derived from gateway URL | Public LiteLLM proxy UI address opened by the admin Usage button |
| `COLLEGIUM_AVAILABLE_MODELS` | built-in fleet | JSON array overriding the composer model registry |
| `COLLEGIUM_GLOBAL_THREAD_CAP` | `12` | Concurrent threads across the whole system |
| `COLLEGIUM_APPROVAL_TIMEOUT_SECONDS` | `900` | After this the worker denies and the card expires |
| `COLLEGIUM_ENGINE_RUNTIME` | `custom` | `sdk` keeps the legacy fallback alive through the RE soak |
| `COLLEGIUM_ENGINE_CANARY` | `false` | Read-only threads on the custom engine before the flag flip |
| `COLLEGIUM_ENGINE_DATABASE_URL` | empty | Postgres checkpointer DSN for workers; empty = in-memory fallback |

The Compose files accept the ADO and gateway secrets under short aliases
(`FETCH_PAT`, `FLEET_PAT`, `ADO_ORG`, `LITELLM_MASTER_KEY`, …) and map them onto
the prefixed settings — fill in the `.env.example` names, not the table names,
when deploying.

## Models

The composer has a **model picker** fed by the backend registry
(`backend/app/core/models.py`, overridable via `COLLEGIUM_AVAILABLE_MODELS`).
Pick none and the run uses the deployment default; pick one and every lane of
the run uses it; pick several in **ask** mode and the run fans out into a
compare — one lane per model answering the same prompt side by side. Each
selected model also gets a per-lane **reasoning control** (off / low / medium /
high / max, per what that model actually accepts — validated fail-closed at
run creation, never silently clamped).

Every lane's virtual key is scoped to exactly its model, so spend attribution
stays exact at the gateway, and a kill/replace replays the original lane's
model and reasoning choice.

The settings sidebar (gear in the rail) holds a **swarm agent model**: when
set, subagent lanes — goal explorers and swarm slices — always spawn on it,
regardless of the composer's lane selection. Lead/planner/synthesis lanes are
unaffected. Unset, subagents follow the run's lane/default model.

**Image attachments** (up to 10 per message, 5 MB each) ride the same
composer. Vision-capable models receive the images natively — they live in
the lane's first message, so image+text is present at every step of the turn.
For text-only models, a vision model first produces a detailed description
conditioned on the user's own words, and that description is embedded in the
lane's prompt instead — attach a screenshot of a bug and even a text-only
lane reasons about the actual error text. An image with no text is a legal
run; the lane is asked to examine the attachments.

Every agent message carries a **metrics footer** — time-to-first-token, total
latency, input/output/reasoning token counts, and cached-token reads when the
provider reports them.

## Modes and autonomy

Four seeded modes — **ask** (read-only investigation), **plan**, **development**,
and **debug** — each with its own persona, topology, SKILL.md playbooks, and a
fail-closed tool filter enforced by the engine (not the prompt).

Autonomy is a separate dial, and it earns its way up:

- **supervised** — every write, bash, and git action raises an approval card.
- **gated** — file edits proceed; bash, git, and MCP mutations still ask.
- **autonomous** — nothing is bridged. Unlocked only after a track record of
  completed gated runs (`COLLEGIUM_AUTONOMY_PROMOTE_*` thresholds).

Approval cards are not open-ended. The worker blocks for
`approval_timeout_seconds`, then denies deterministically; the backend expires
the row in step so the console never shows a button that does nothing. Humans
can also **edit-and-resend**: the decision carries edited args and the gate
applies them verbatim.

## Testing

```bash
cd backend && uv run pytest          # control-plane suite, 90% coverage gate
cd worker && uv run pytest           # engine suite — contract, resume, redaction tests
cd apps/web && npm test              # vitest — store WS handler, approvals, deep links, …
cd apps/web && npm run build         # typecheck + production build
```

Tests are mock-only by default: no live ADO, gateway, Docker, or Redis.
`memory://` Redis and SQLite make the backend suite hermetic; the engine suite
runs against a mock gateway and fixture events. On top of that sits an opt-in
**integration tier** — `backend/tests/integration/` (pg race conditions, Redis
stream redelivery) runs only when pointed at live infra:

```bash
COLLEGIUM_INTEGRATION=1 \
INTEGRATION_DATABASE_URL=postgresql+psycopg://postgres:test@localhost:55432/postgres \
INTEGRATION_REDIS_URL=redis://localhost:6379/9 \
uv run pytest backend/tests/integration -q
```

CI provisions throwaway Postgres/Redis services and runs this tier on every PR.

## Known gaps

Honest about what isn't built yet:

- **Prewarm pool** is specified and stubbed; thread spawn is cold.
- **Repo maps** aren't generated, so onboarded repos sit at `ready-no-map`.
- **Playwright evidence** stamps paths but never captures screenshots.
- **Push notifications** need `pywebpush` plus VAPID keys to actually send.
- **Retention** (`session_retention_days`, `events_ttl_months`) is configured and
  the purge helpers exist, but nothing schedules them yet.
- **No rate limiting** on the API; concurrency is bounded only by the thread cap.
- **`infra/k8s/`** is a scaffold — no database, gateway, web, or secrets
  manifests. The VM kit is the supported deployment.
- **RE hardening** (soak, SLOs, rollback drill, 8-thread swarm acceptance) is
  the last gate before the legacy SDK seam is cut for good.

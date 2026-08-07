# ServerApp — Collegium Guidebook (Phase 1 seed, hand-curated)

> Navigation map for the read-only researcher lane. Orient here, then verify
> with grep/glob/read on the mounted tree — the tree is ground truth, this file
> is the index. The authoritative contributor guide is `AGENTS.md` at the repo
> root (~490 lines); cite file:line in answers.

## What this service is

NestJS backend — the central hub for the fleet. REST + three WebSocket namespaces;
all clients (ClientApp, LiveKit agents, PromptFlow callers) connect here.
Owns auth, persistence (Postgres via Drizzle), and orchestration to LiveKit
agents and PromptFlow. TypeScript, Jest, ESLint+Prettier, Azure DevOps CI.

## Run / verify commands

- `npm run start:dev` — hot-reload dev server, Swagger at `/api/docs`
- `npm run test` / `npm run test:e2e` / `npm run test:cov`
- `npm run lint` — must pass before PR
- `npx drizzle-kit generate | migrate | check` — schema lifecycle; `check`
  must be clean in CI (drift is a blocker, not a warning)
- Postgres needs `pg_trgm` (`CREATE EXTENSION IF NOT EXISTS pg_trgm;`) before
  first migration

## Layout (where things live)

- `src/main.ts` — bootstrap: CORS, helmet, ValidationPipe, Redis IO adapter,
  Swagger, 300s server timeout
- `src/app.module.ts` — root module; **no feature routes here**
- `src/areas/` — ALL features. Module-per-feature (`*.module.ts`,
  `*.controller.ts`, `*.service.ts`, `dto/`), registered in the parent domain
  module:
  - `core/` — auth, profile, user, user-tokens
  - `clinical/` — metrics, organization, patients, scribe
  - `patient/` — conversations, encounters, uploads, speech, real-time intake,
    resource revisions
  - `sharing/` — appointments, groups, members, sharing, webhooks, WhatsApp
  - `admin/`, `content/`
- `src/services/` — cross-cutting infra:
  - `brainLlm/`, `azureOpenAi/`, `openAiSpeech/`, `speech/`, `intake/` — LLM + audio
  - `encounter-queue/` — true Service Bus consumer (encounter LLM jobs,
    session-based, 20 concurrent)
  - `clinical/`, `messaging/`, `email/`, `posthog/`, `translation/`, `jwt/`
- `src/database/`
  - `drizzle/drizzle.provider.ts` — `DRIZZLE` token over pooled `pg.Pool`
  - `drizzle/schema/<domain>.ts` — table defs, re-exported from `schema/index.ts`
    (~16 domains: identity, profiles, groups, encounters, labs, medications,
    conversations, scribe, ai-processing, prompts, llm-observability,
    organizations, audit, reference)
  - `drizzle/migrations/` — generated SQL + `meta/` snapshots (never hand-edit
    committed migrations)
  - `repository/` — typed repositories per table; **the only place feature
    code talks to the DB**
- `src/domain/` — shared cross-area types (`base.ts`, `clinical.ts`,
  `conversations.ts`, `common/patch-operation.ts`)
- `src/common/` — guards (`jwt-auth.guard.ts`, `jwt.strategy.ts`),
  `adaptor/redis-io.adapter.ts`, `audit-logs/`, Key Vault, filters,
  interceptors, pipes, decorators

## Load-bearing invariants (cite these when answering "how should X work")

1. **Repositories only.** Only `src/database/repository/` imports
   `drizzle-orm` / the `DRIZZLE` token. No `db.select()` in services.
2. **Audit-in-transaction.** Every mutation emits an `auditLogs` row via
   `AuditLogsService.logEntityChange(...)` inside the SAME `db.transaction(...)`
   as the mutation. Unaudited PHI changes are a HIPAA finding.
3. **Tenant isolation from the principal.** Every PHI-returning repository
   method filters `WHERE organization_id/group_id = ...` derived from the
   authenticated principal — never from body/path params.
4. **ORDER BY with tie-breaker on every list endpoint** — default
   `ORDER BY updatedAt DESC, id DESC`; missing ORDER BY = flaky pagination.
5. **JSON-patch allow-lists.** Resource patches go through
   `patchOperationsToSetObject` + per-resource allow-listed paths; arbitrary
   ops reject at the controller.
6. **WebSocket identity from the handshake**, never from message payloads.
   Namespaces: `conversation`, `scribe`, `realtime-intake`. Rooms keyed by
   `encounterId`/`conversationId`. Redis adapter fans out across pods.
7. **LLM calls through shared services** (`brainLlm/`, `azureOpenAi/`) —
   caching, retries, observability live there; never instantiate SDKs in
   feature code. PromptFlow is HTTP to deployed flow endpoints
   (`NullConnectionPool` — don't share PG pools with it).
8. **DTOs + ValidationPipe on every input** (REST and WS). Guards on every
   route except `@Public()`. No `any` in services/repositories.
9. **Idempotent queue consumers** — derive the idempotency key from the
   message; DLQ + alert on non-zero depth.
10. **PHI segregation (staged):** ServerApp is the only service holding
    Registrations-DB credentials; the two-client split
    (`DRIZZLE_REGISTRATIONS` / `DRIZZLE_APPLICATION`) has no cross-DB joins.

## Naming caveats (things that bite newcomers)

- Most `AZURE_SERVICE_BUS_*` env vars actually hold **Azure Storage Queue**
  names; only `AZURE_SERVICE_BUS_CONNECTION_QUEUE_STRING` is true Service Bus
  (`services/encounter-queue/`).
- `auth.service.ts` is mid-split into `RegistrationService`, `OtpService`,
  `MagicLinkService`, `PasswordService` — new auth work lands in the small
  services.
- Removed under Postgres (do not reintroduce): `encounter-queue/icd`, FHIR
  conformance suite, legacy "Old-Account" type discriminator.
- Request-scoped context (correlation id, principal, org/group, traceparent)
  lives in `AsyncLocalStorage` — pull from the context store, don't thread
  args.
- Logging uses Nest `Logger` per class (never `console.log`); PHI redaction
  is the `LoggingInterceptor`'s job via a registry — new free-text PHI columns
  register there in the same PR.

## Fleet position (see services.json for the full graph)

Upstream callers: ClientApp (REST + WS). Downstream: LivekitScribe /
LiveKitIntake (LiveKit RPC), PromptFlowApp flows (HTTP), Postgres (app DB),
Redis (adapter + throttler + blacklist), Azure Service Bus, Storage Queues,
SendGrid, Twilio, Azure Key Vault.

# Web Frontend — Drift Audit

> 10 parallel agents · ~10,700 lines across `apps/web/` · every finding verified against the **post-remediation** backend/worker · Aug 8, 2026 · Companion to [harness_diagnosis.md](./harness_diagnosis.md) (worker), [backend_diagnosis.md](./backend_diagnosis.md) (backend), [integration_diagnosis.md](./integration_diagnosis.md) (seam) · Full per-agent reports: [web-harness-diagnosis.md](./web-harness-diagnosis.md)
>
> Honesty protocol: every claim carries file:line evidence read this session on **both** sides; statuses are CONFIRMED / RISK / UNVERIFIED; agents were told zero findings is acceptable and correcting a false prior claim is valuable.

| Metric | Value |
|---|---|
| Raw findings | 108 across 10 agents |
| Unique clusters after cross-agent dedup | **~48** |
| Unique BLOCKER clusters | **6** |
| Findings triangulated by ≥3 agents | 5 |
| Prior-brief claims corrected | 6 |

**⚠ THE META-FINDING: the remediation hardened the worker↔backend seam, but nobody re-negotiated the backend↔web contract — and the two sides now hold *opposite* assumptions.** The backend assumes the UI hardcodes a Stop button (`services/runs.py:11`) while the UI renders only server-sent actions; the backend advertises `resume_run`/`edit_and_resend` in `available_actions` while the intent dispatcher has no working handler for either; the backend now emits `input_required` thread status while the web's status union can't represent it. **Five of the six blocker clusters are this single skew.** The remediation also left two regressions the web audit caught from the far side: seq is *not* single-sourced in practice (D2 incomplete — the new unique constraint silently eats one worker event per user message), and the C3 mode reconciliation missed `agent-rnd`, so every UI-started swarm run fails at spawn.

---

## The 6 blocker clusters, ranked

### W-B1 · Stop/Abandon are unreachable from the web — triangulated by 3 agents (W2, W3, W6)

`services/runs.py:11` states "The ONE control always visible (Stop) is hardcoded by the UI — never in this list", and `ACTIONS_BY_STAGE` omits `stop_run`/`abandon_run` for every stage. But `ActionCard.tsx:70-87` renders **only** `available_actions`, and `SessionsScreen.tsx:322-327` passes them verbatim — nothing hardcodes Stop. `runMachine.ts:44-49` has `ALWAYS_SHOW`/`visibleActions()` for exactly this, but only *tests* import it — and `actionCard.test.tsx:27` / `runMachine.test.ts:30-35` pin `stop_run` inside `available_actions`, a state the backend never produces, so CI is green over a fictional contract. **The entire Wave-1 lifecycle-truth investment (acked stop, safe abandon) cannot be invoked from the product's primary surface.** Fix: hardcode Stop (always) + Abandon (overflow, two-tap) firing `sendIntent`, as the backend contract assumes; fix the masking tests.

### W-B2 · Interrupted runs are unrecoverable from the UI — all three recovery affordances are broken (W3, W5, W6)

- **Resume button is a silent no-op:** `resume_run` is advertised for INTERRUPTED/FAILED (`services/runs.py:24,26`) and passes the intent gate, but `post_intent` (`api/runs.py:287-490`) has **no RESUME_RUN branch** — it falls through to `return {"status": "ok"}`. The working resume lives at `POST /sessions/{id}/resume`, which the button never calls.
- **Edit & resend always 422s:** M6 wired the backend handler, but the web has no edit box and never sends `text` (`ActionCard.tsx:26` fires it bare) → guaranteed 422 on every click.
- **Resume banner is gated on heuristics, not resumability:** `SessionResume.tsx:24` enables on `!working`, so it offers silent no-op clicks on queued/awaiting_user runs and shows a nonsense "workspace expired" line on fresh queued runs; real resume failures escape as unhandled 500 rejections.

Fix: route `resume_run` to the sessions resume endpoint (or add the dispatcher branch), wire an edit affordance, gate the banner on `stage ∈ {interrupted, completed, failed, abandoned} ∧ resumable`.

### W-B3 · Swarm mode is offered but every swarm run fails at spawn (W6) — a C3 remediation miss

The UI offers `{ value: "agent-rnd", label: "swarm" }` (`SessionsScreen.tsx:38`), but post-remediation `thread_env` rejects it: `WORKER_MODES = {"ask","plan","development","debug","goal"}` (`sandbox/manager.py:128`) → `InvalidModeError` → `ThreadSpawnError` → run FAILED (`thread_manager.py:167-178`, `run_manager.py:287-313`). The Lead never starts. Fix: map `agent-rnd` → a worker-vocabulary mode in `thread_env`, or add it to the worker `Mode` enum.

### W-B4 · Run creation idempotency is inert — the web never sends the key (W6, W2, W3)

H5 added `idempotency_key` to POST `/runs` with a partial unique index — but `stores/run.ts:28,196` never sends it, and the Enter handler (`SessionsScreen.tsx:336-341`) bypasses the `busy` guard that only disables the button. Double-Enter on a slow network mints two runs and double-spends budget. Same class: idea→run promote has no idempotency on *either* side (`api/ideas.py:92-103`), and its test name pins a disable behavior that doesn't exist (W9). Fix: `crypto.randomUUID()` per composer draft, send the key, gate Enter on `!busy`.

### W-B5 · Push deep links are dead on arrival — the M7 route has no web consumer (W10, W1)

`services/push.py:111-113` builds `/app?screen=approvals&run={id}&card={id}` and the fixed test suite asserts it — but **nothing in the web reads query params** (zero `useSearchParams`/`URLSearchParams` hits), `screen=approvals` isn't a valid `Screen`, and the login redirect drops the params entirely. Every notification tap — the PWA's core mobile approval flow — lands on the generic inbox. Fix: parse `run`/`card` at `/app`, `openRun(run)` + focus the card, carry `?next=` through login.

### W-B6 · "Single-sourced seq" is false — the new unique constraint silently eats one worker event per user message (W7, W2) — a D2 remediation miss

The backend allocates user-message seqs from `thread.next_seq` (`api/runs.py:48-50`) while the worker keeps its own seq file (`engine/events.py:41-61`), never re-seeded. `bus.py:233-235` ratchets `next_seq = worker_seq + 1`, so in steady state the backend counter *equals* the worker's next seq: every user message steals exactly the seq of the worker's next event, which then hits `uq_events_run_thread_seq`, is acked as a "duplicate" (`bus.py:223-232`) and **never stored or relayed**. The agent's first step after every user message vanishes from the feed, live *and* in replay — worse on resume (N messages while stopped → N collisions). This is a backend/worker regression the web makes visible; the web's missing seq-dedupe/gap detection (`stores/run.ts:82-87`) means it couldn't even be detected client-side. Fix: route backend-originated events through the same allocator (or re-seed the worker from `thread.next_seq` at turn start).

---

## High findings (deduplicated)

| # | Finding | Evidence | Agents |
|---|---|---|---|
| W-H1 | **`input_required` renders as a dead thread everywhere** — missing from `LaneStatus` (`types/index.ts:24-26`), blind-cast (`stores/run.ts:93`), LED maps fall back to `led--off`, `ThreadOverlay` hides stop/kill from a thread the backend considers alive | worker `runner.py:242,474` → `heartbeats.py:199` → DB constraint `thread.py:71` | **W1, W2, W3, W4, W5, W8, W10** |
| W-H2 | **No WS reconnect resync** — `relay.py:78-84` and `ws/events.py:60-65` explicitly assume "the client resyncs on reconnect"; `ws.ts:29-32` never does. A missed `run_stage` (e.g. reaper death) strands the UI on "running" forever — defeats E3 death visibility | both sides | W2, W7, W10 |
| W-H3 | **VERIFYING is wedged** — `agentWorking` hides the only two actions the backend offers there (`review_evidence`, `create_pr`); the run can never reach `pr_ready` from the UI | `runMachine.ts:41-43` vs `services/runs.py:21` | W3 |
| W-H4 | **Approvals: "always allow" lies on destructive cards** — offered unconditionally (`ApprovalQueue.tsx:149-156`), post-G4 it degrades to allow-once-unpersisted with 200 OK and a misleading audit row | `engine/approvals.py:96-108`, `graph.py:509-517` | W4 |
| W-H5 | **Approvals: stop/abandon leaves zombie cards** — pending Approval rows are never stamped on terminal transitions and no `approval_resolved` fans out; deciding one 200s into a dead BLPOP key | `run_manager.py:363-516` vs `services/approvals.py:151,283` | W4 |
| W-H6 | **Approvals: `edited_allow` is plumbed end-to-end but unreachable** — no edit UI exists; the safety-valve feature doesn't exist for humans | `api/approvals.py:35,42-43` → `graph.py:497-507` | W4 |
| W-H7 | **Queued swarm threads are invisible** — `spawn_many` announces "queued" with fake `thread_hint` ids the store silently drops (the L-22 anti-pattern still live at `thread_manager.py:277-278`); no row, no position, no toast | both sides | W2, W5, W8 |
| W-H8 | **kill_replace failure escapes as 500 after the tile is stamped `replaced`** — `RuntimeError` at `run_manager.py:619` isn't converted like the `ValueError`→422 path | web toast + backend | W5 |
| W-H9 | **Tz-naive backend ISO parsed as browser-local** — east of UTC, `isStaleThread`'s age goes negative and the stale-heartbeat watchdog *never fires*; approval countdowns inflate by the offset; live vs replay timestamps disagree | serializers (`api/runs.py:93-94,184-187`) vs `time.ts:9,20`, `runMachine.ts:71` | W1 |
| W-H10 | **Every tool call double-renders** (call + result events; edits mis-kinded `command` vs `file_edit`) — violates the contract's "emitted ONCE, COMPLETE, at step end" | `graph.py:336-337,694-702`, `events.py:203-211` | W7 |
| W-H11 | **Replay silently truncates to the oldest 500 events** — `openRun` never passes `after_seq`; long runs lose their tail on reload | `stores/run.ts:65` vs `services/sessions.py:54-68` | W2, W5, W7 |
| W-H12 | **Legacy SDK path streams unredacted** — D5 parity is custom-engine-only; `normalize.py` has no redactor and is still flag-selectable | `normalize.py:111-194` vs `graph.py:409-419` | W2, W7 |
| W-H13 | **Run failures surface only as transient toasts** — spawn-failure notes ride ephemeral `publish_note`; miss the toast and a failed run shows "failed" with zero explanation | `run_manager.py:291-295` (its "the event stream renders it inline" comment is false) | W6 |
| W-H14 | **Messages sent on interrupted/terminal runs black-hole** — persisted and broadcast so they *look* delivered; no agent ever receives them | `api/runs.py:339-353`, `run_manager.py:487-490` | W6 |
| W-H15 | **First-login onboarding unreachable** — `/auth/first-login` exists, no web screen calls it; `Me.must_change_pin` is a phantom field the backend never returns | `api/auth.py:74-115` vs `types/index.ts:119` | W1 |
| W-H16 | **Team deactivate/regen fail silently** — new self-deactivate 422 and 404s never surface (no try/catch, no toast) | `TeamScreen.tsx:93-102` vs `api/team.py:83-91` | W9 |
| W-H17 | **Palette "New run" navigates to the public landing page**; ⌘N is browser-reserved and dead | `CommandPalette.tsx:88-91` vs `routes.ts:5-9` | W6 |

## Medium findings (selected, all CONFIRMED unless noted)

- **Approvals:** expired decide returns 200 `decision:"timeout"` the UI discards — human thinks they allowed, the tool was denied (W4). Knowledge approval cards leak into the session queue and wedge their linkage (W4). 409 cross-device re-drive resurrects cards with no explanation (W4).
- **Lifecycle:** no in-flight disable on now-slow ack-gated actions (stop blocks on 10s/thread acks) → double-clicks double-POST (W3). Runs list never refetches — background-run death shows a live LED until remount (W3). Illegal-intent `ValueError` escapes as 500 instead of 409/422 (W3).
- **Feed:** engine's `blocked`/`nudge_deferred` status events are filtered as plumbing — a nudge during an approval wait appears to vanish (W7). Ghost "typing…" bubble persists on terminal stage (W2, W7). Live vs replay cross-thread ordering drift; unbounded O(N)-per-token fold (W7). PROverlay's sha256 is recomputed per fetch over `generated_at` — never matches the PR-body hash (W5).
- **Screens:** proposal decide errors swallowed (J4 retry works but unexplained); knowledge has no reject path and approve defaults to global scope with `proposed_scope` invisible; free-typed repo scope black-holes items; branch picker defaults to alphabetically-first branch (W9). Dashboard never live-refreshes while cost now settles on every terminal path (W8).
- **Infra:** sw.js never caches build assets (offline boot broken); one hardcoded notification tag collapses concurrent approval notifications; denied-permission subscribe throws unhandled (W10). 422 validation arrays stringify to "[object Object]"; login 401 misreports as "session expired" (W1). `Composer.tsx` is dead code with stale modes while the real composer is untested (W6).

## Corrections to the brief / prior claims (valuable per honesty protocol)

1. The backend does **not** reject always-allow on destructive tools with an error — it degrades silently to allow-once-unpersisted (W4).
2. There is no `file_delete` tool — destructive means destructive `terminal_exec` only (W4).
3. Concurrent duplicate repo onboards return **200 + winner's row**, not 409 (sequential duplicates do 409) (W9).
4. No runtime `schema_version` handshake exists anywhere despite M2 — only pins/tests (W2).
5. No queue-position field exists in any API/WS payload — "FIFO visible queue" is internal-only (W3, W8).
6. The hypothesized new stats fields (queue depth, dead letters, leaked keys) do not exist — dashboard schema matches exactly (W8).

---

## What the web already does well (verified-OK highlights)

Hand-mirrored types are otherwise in sync: `RunStage`/`StepKind` match contracts exactly; `Run`/`Thread`/`PlanPayload`/`Approval` serializers match field-for-field. WS message vocabulary, approval envelopes, and the delta envelope match the relay exactly; the L-22 note channel works end-to-end. Decision strings round-trip; concurrent double-decide and idempotent re-drive are handled. XSS posture is sound (react-markdown without rehype-raw, mermaid `securityLevel:"strict"`, no raw HTML). Store race hygiene is real (openRun generation token, H-61/M-80/M-81 guards). J4 proposal retry, L2 knowledge locking, and 409 surfacing all verified working. The test harness installs no fetch/WS mocks — nothing masks contract drift. **The web's bones are good; what fails is the renegotiated contract surface and a handful of dead affordances.**

## Audit coverage — 10 slices, ~10.7k lines

| Agent | Slice | Lines | Focus |
|---|---|---|---|
| W1 | types, api client, router, auth shell, stores, i18n, ErrorBoundary | ~900 | contract drift, auth, deep links |
| W2 | ws.ts, EventStream, run store + tests | ~885 | WS ingest, dedupe, resync |
| W3 | runMachine, SessionsScreen, SessionResume/Tabs, PipelineBar | ~1,270 | lifecycle actions, stage machine |
| W4 | ApprovalQueue, ActionCard + tests (+ deep backend/worker read) | ~373 | approval vocabulary round-trip |
| W5 | ThreadSidebar/Tile/Chips, PR/Plan/Thread overlays, Viewer | ~657 | thread status, delivery links |
| W6 | Composer, MentionTextarea, CommandPalette, OverlayShell | ~774 | run creation, idempotency, intents |
| W7 | Feed, cards, Markdown/Mermaid/CodeView + tests | ~1,259 | event rendering, XSS, replay |
| W8 | Dashboard, Landing, SwarmView, SideRail, MobileTabBar | ~990 | stats shape, swarm visibility |
| W9 | Repos/Team/Proposals/Ideas/Knowledge screens + tests | ~1,408 | admin flows vs service guards |
| W10 | push + sw.js, 24 ui primitives, test/build harness | ~2,400 | push deep links, status primitives |

Cross-validation: `input_required` triangulated by 7 agents; Stop-unreachable by 3; reconnect-resync, replay-truncation, queued-invisibility, edit-and-resend each by 2-3 — independent agents reaching the same root cause from different slices.

---

## Decision rule — fix order (ripple-safe staging)

1. **Pure web fixes, zero backend ripple** — add `input_required` to `LaneStatus` + LED/live maps (W-H1); WS reconnect resync via `after_seq` (W-H2); send `idempotency_key` + gate Enter (W-B4); parse push deep-link params (W-B5); in-flight disables; error surfacing for 409/422/500; tz-aware timestamp parsing (W-H9); un-wedge VERIFYING (W-H3, one-line removal from `AGENT_WORKING`).
2. **Small backend additions the web contract already assumes** — a `resume_run` branch in `post_intent` (W-B2); publish `thread_status` on `finish_thread` and route queued notices through `publish_note` (W-H7); convert `kill_replace`'s `RuntimeError` to 422 (W-H8); stamp/resolve pending approvals on terminal transitions (W-H5).
3. **Remediation regressions to close upstream** — single-source seq for real (W-B6); reconcile `agent-rnd` in `thread_env` (W-B3); hardcode Stop/Abandon in the UI *or* ship them in `available_actions` — pick one side of the assumption (W-B1).
4. **Then ergonomics** — replay pagination, feed windowing, dashboard refresh, SDK-path redaction parity, dead-code removal (`Composer.tsx`, `Feed`/`Viewer`, `ThreadSidebar`, `pagination.tsx`).

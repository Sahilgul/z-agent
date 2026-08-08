# Web Harness Diagnosis — 10 Agents

10 web parallel agents diagnosis

agentW1:

# Agent W1 audit — types / API client / routing / auth shell

## Findings

### HIGH

**H1. Push approval deep-links land nowhere — router never reads the params**
Status: CONFIRMED.
- Backend/worker evidence: `backend/app/services/push.py:111-113` builds `approval_deep_link` as `/app?screen=approvals&run={run_id}&card={approval_id}`, with the stated intent "a tap lands on the approval, not the inbox" (`push.py:8-9`); `apps/web/public/sw.js:68-82` navigates/focuses that URL on `notificationclick`.
- Web evidence: no `useSearchParams`, `URLSearchParams`, or `location.search` anywhere in `apps/web/src` (grep, zero hits); `apps/web/src/router.tsx:167` renders `SessionsScreen` at the `/app` index regardless of query; `screen=approvals` isn't even a valid `Screen` (`types/index.ts:124-126`, `lib/routes.ts:8-16`). If logged out, `RootLayout` redirects with `Navigate to="/login" replace` (`router.tsx:120`) and the params die with it — no return-url handling.
- What breaks: every push notification tap (the PWA's core mobile approval flow) lands on the generic inbox with no run opened and no card focused; after a login redirect the deep link is unrecoverable.
- Fix direction: parse `run`/`card` search params in the sessions screen (or a dedicated `/app/approvals` route), `openRun(run)` + focus the card, and carry a `?next=` through the login redirect.

**H2. Backend timestamps are tz-naive ISO; the web parses them as browser-local — watchdog silently dead east of UTC**
Status: CONFIRMED at code level (magnitude RISK by DB, but both SQLite and the PG columns as declared are naive).
- Backend evidence: serializers emit `.isoformat()` on naive datetimes — runs (`backend/app/api/runs.py:93-94`), threads incl. `heartbeat_at` (`runs.py:184-187`), replay events `ts` (`backend/app/services/sessions.py:72`), approvals `expires_at` (`backend/app/api/approvals.py:75-76`). The codebase itself acknowledges naive round-trips (`sessions.py:21-26`, `api/approvals.py:86-87`); models never use `DateTime(timezone=True)` (`db/models/run.py:60-63`, `db/models/event.py:48`).
- Web evidence: `new Date(iso)` / `Date.parse` on suffix-less strings parses as local — `lib/time.ts:9,20`, `lib/runMachine.ts:71` (`Date.parse(thread.heartbeat_at)`), `components/ApprovalQueue.tsx:48` (`new Date(expiresAt)`).
- What breaks: (a) all replayed/list times display shifted by the user's UTC offset, while live WS events are correct (pydantic `model_dump` includes `+00:00`) — live vs reload disagree; (b) `isStaleThread` computes `now - Date.parse(heartbeat_at)` — east of UTC the parsed beat is in the future, the age is negative, and the "heartbeat stale — nudge" watchdog NEVER fires, right when remediation made liveness visible; (c) the approval countdown `auto-denies in Nm` overstates by the offset (~315m shown for a 15m TTL in UTC+5).
- Fix direction: append `+00:00`/`Z` at the backend serializer edge (or store timezone-aware columns), never emit naive ISO.

**H3. `LaneStatus` union is missing `input_required`, which the post-remediation backend now emits**
Status: CONFIRMED.
- Worker/backend evidence: the engine parks at `self.status = "input_required"` and heartbeats it (`worker/worker/engine/runner.py:242-243`, `runner.py:474-477`); the heartbeat persister writes that status into the row (`backend/app/services/heartbeats.py:199-200`); it's treated as live/capacity-holding across `orchestrator/semaphores.py:35`, `orchestrator/run_manager.py:213,320,375`, and the DB partial index (`db/models/thread.py:71`). It reaches the web via `GET /runs/{id}/threads` (`api/runs.py:180-188`) and `thread_status` WS (`events/relay.py:56-57`).
- Web evidence: union lacks it (`apps/web/src/types/index.ts:24-26`); the WS handler casts blindly (`stores/run.ts:93`); `Record<LaneStatus, string>` LED maps in `components/ThreadChips.tsx:8` and `components/ThreadTile.tsx:8` return `undefined` for it.
- What breaks: an approval-parked thread loses its status styling (no crash — `clsx` swallows undefined), and any status-equality logic misclassifies it; the hand-mirrored type now lies about a live value, so TS can't catch the next consumer.
- Fix direction: add `"input_required"` to the union and give it LED/pulse entries (it is parked-but-alive, like `idle`).

**H4. First-login onboarding has no web UI; `Me.must_change_pin` is a phantom field**
Status: CONFIRMED.
- Backend evidence: `POST /auth/first-login` redeems a setup code + sets PIN (`backend/app/api/auth.py:74-115`); `/auth/me` returns only `id/username/display_name/role` (`auth.py:124-129`); `must_change_pin` appears NOWHERE in the backend (grep, zero hits).
- Web evidence: no `first-login`/`first_login` reference in `apps/web/src` (grep); no setup screen in `src/features/*` (glob — only LoginScreen, which is username+PIN only, `features/login/LoginScreen.tsx:78-152`); meanwhile `TeamScreen` mints one-time setup codes to "send via Slack" (`features/team/TeamScreen.tsx:77-82,124`) — the recipient has no in-app redemption path. `Me.must_change_pin` is required in the type (`types/index.ts:119`) and is only ever set by tests and the dev bypass (`stores/session.ts:46`).
- What breaks: new-teammate onboarding dead-ends (API-only redemption); any future guard on `me.must_change_pin` silently never fires (`undefined`).
- Fix direction: add a first-login screen/route calling `/auth/first-login`; either return `must_change_pin` from `/me` or delete it from `Me`.

### MEDIUM

**M1. A 401 on the login call itself is reported as "session expired"**
Status: CONFIRMED.
- Web: `lib/api.ts:30-33` throws `ApiError(401, "session expired")` before reading the body, for every 401; `LoginScreen.tsx:57-59` renders that message.
- Backend: login failures carry real detail — "invalid credentials" / "account inactive" (`backend/app/api/auth.py:60-66`).
- What breaks: a wrong PIN tells the user "session expired"; lockout (429, `core/security.py:63`) is fine, which makes the 401 case stand out.
- Fix direction: skip the 401 shortcut for `/auth/login` (or parse `body.detail` before throwing).

**M2. FastAPI 422 validation bodies render as "[object Object]"**
Status: CONFIRMED.
- Web: `lib/api.ts:38-42` does `body.detail ?? statusText` then `String(detail)`; pydantic `RequestValidationError` detail is a LIST of `{loc, msg, type}` objects → `String([...])` = "[object Object]".
- Backend: model-level 422s now reachable post-remediation — e.g. `edited_allow` without `edited_args` (`backend/app/api/approvals.py:37-44`), missing `task` on POST /runs (`api/runs.py:66-68`). (Hand-raised `HTTPException(422, detail=str)` cases are fine.)
- What breaks: validation failures toast garbage instead of the reason.
- Fix direction: if `detail` is an array, join `msg` fields.

### LOW

**L1. `repo_added`/global WS messages are absent from `WsMessage` and silently dropped** — CONFIRMED. Relay fans out `{"type":"repo_added",...}` with the stated intent "invalidates the repo-list query, no refresh" (`backend/app/events/relay.py:126-144`, `services/repos.py:219`); `types/index.ts:60-73` lacks the variant and `stores/run.ts:78-127` has no branch, so the intended invalidation never happens (the adder self-refreshes at `ReposScreen.tsx:86`, so impact is limited). Fix: add the variant + invalidate `qk.repos`.

**L2. WS reconnect loop is blind to 4401/4404** — RISK. Backend closes with 4401 (expired/revoked session) or 4404 (foreign run) (`backend/app/ws/events.py:44,50`); `RunSocket` retries forever at 10s cap without inspecting `event.code` (`apps/web/src/lib/ws.ts:40-56`). Self-corrects only when some REST poll 401s and nulls the session; an idle screen churns reconnects against a dead session. Fix: treat 4401 as terminal → trigger the unauthorized handler; 4404 → no retry.

**L3. Web `StepEvent` lacks `context_id`/`task_id`** — UNVERIFIED impact. Contracts carry both as optional v1 fields (`packages/contracts/src/collegium_contracts/events.py:54-63`); the web type (`types/index.ts:47-58`) omits them, so subagent-context attribution is unavailable to the UI. Additive-only; no current break.

**L4. Dead `/lanes` config + stale sw.js API-prefix list** — CONFIRMED as drift, no live break. `vite.config.ts:8` and `public/sw.js:24` still list `/lanes` though the backend mounts `threads.router` (`backend/app/main.py:213-218`); sw.js's bypass list omits several prefixes the app calls (`/knowledge`, `/ideas`, `/proposals`, `/push`, `/stats`, `/deliveries`) — harmless today because that branch never `put`s API GETs, but the "API is always network" invariant rests on that omission. Fix: prune `/lanes`, sync both lists with `API_PREFIXES`.

## Verified OK

- `RunStage` union identical to contracts enum (`types/index.ts:3-6` vs `intents.py:16-28`); `StepKind` identical (`types/index.ts:43-45` vs `events.py:37-47`).
- `Run`, `Thread`, `PlanPayload`/`PlanStep`, `Approval`, `ResumableThread` all field-for-field match their serializers (`api/runs.py:87-95,180-188,210-221`; `api/approvals.py:73-77`; `api/sessions.py:66-72`). No new backend fields to mirror: no queue position, no `idempotency_key`, no settled-cost field is serialized — the brief's implied expectations there are corrected.
- `WsMessage` step/thread_status/run_stage/delta/approval_card/approval_resolved/note payloads match relay + approvals fanouts exactly (`relay.py:53-71,124`; `services/approvals.py:151-153,161-166,195,283-285`).
- Decide vocabulary: web sends `allow_once`/`always_allow`/`deny` (`ApprovalQueue.tsx:146-164`), all in backend `_VALID_DECISIONS` (`api/approvals.py:28-29`); 409 conflict and idempotent re-drive handled by mutation rollback + refetch (`ApprovalQueue.tsx:99-117`).
- 401 → logout flow can't loop: `boot()` is once-per-load guarded (`stores/session.ts:21`), `onUnauthorized` nulls `me` → `/login`; lockout 429 and logout-without-cookie are safe.
- Cookie assumptions consistent: `credentials:"include"` + same-origin Vite proxy incl. `/ws` upgrade (`vite.config.ts:20-36`), backend cookie `samesite=lax` (`api/auth.py:46-51`); no CORS middleware exists and none is needed same-origin.
- ErrorBoundary: query/WS errors can't reach it (TanStack `throwOnError` off, `lib/queryClient.ts:6-14`; socket lives outside React), render crashes reset on pathname change (C-18, `router.tsx:98`, `ErrorBoundary.tsx:29-37`) with a reload escape — no wedge path found.
- i18n keys used by LoginScreen/ErrorBoundary all exist; `Screen`↔`SCREEN_PATHS`↔route table are consistent; `resume`/`resumable` round-trip matches post-remediation `resume_run` contract (`api/sessions.py:77-92`).

## Summary

agentW1 (types/api/auth shell): four HIGH findings — push approval deep-links (`/app?screen=approvals&run=…&card=…`) are dropped by the router with no search-param handling anywhere; tz-naive backend ISO is parsed as browser-local, which shifts all replayed timestamps and, east of UTC, permanently disables the stale-heartbeat watchdog and inflates approval countdowns; `LaneStatus` omits the now-live `input_required` thread status heartbeated from worker through backend to the UI; and first-login is unreachable from the web while `Me.must_change_pin` exists only in the type. MEDIUM: login 401s misreport as "session expired"; FastAPI 422 arrays stringify to "[object Object]". LOW: unhandled `repo_added` WS variant, reconnect loops blind to 4401/4404, missing `context_id`/`task_id`, dead `/lanes` proxy/SW config. All other hand-mirrored types verified in sync.

agentW2:

# agentW2 audit — WebSocket ingest / event stream / run store

## Findings (ranked)

### BLOCKER 1 — Run-level Stop/Abandon unreachable from the web; tests mask it
**Status: CONFIRMED (both sides read).** Direction of drift (which side changed) UNVERIFIED, but the seam is broken now and it defeats remediation pillar (1) "stop/kill/resume real + acked".
- **Backend:** `backend/app/services/runs.py:11-12` — "The ONE control always visible (Stop) is hardcoded by the UI — never in this list"; `ACTIONS_BY_STAGE` (`runs.py:12-28`) contains no `stop_run`/`abandon_run` in any stage. The intents themselves work: `backend/app/api/runs.py:287-290`, proven by `backend/tests/test_api.py:569`.
- **Web:** `apps/web/src/features/sessions/SessionsScreen.tsx:322-327` passes `actions={current.available_actions}` verbatim into `ActionCard`, which renders only list members (`apps/web/src/components/ActionCard.tsx:70-71` — the `isStop` branch is dead code since `stop_run` never arrives). Grep over `apps/web/src` finds no other sender of `stop_run`/`abandon_run`. `visibleActions` (`apps/web/src/lib/runMachine.ts:46-49`), the function designed to keep Stop visible, is referenced only by tests.
- **Impact:** a runaway run cannot be stopped or abandoned from the product's primary surface — API/CLI only.
- **Fix:** hardcode Stop/Abandon controls (e.g. session header) firing `sendIntent("stop_run")` / `sendIntent("abandon_run", {confirmed:true})` independent of `available_actions`; fix `actionCard.test.tsx:27` and `runMachine.test.ts:30-35`, which pin `stop_run` inside `available_actions` — a state the backend never produces.

### HIGH 2 — No resync on WS reconnect; backend design explicitly assumes the client resyncs
**Status: CONFIRMED.**
- **Backend:** `backend/app/events/relay.py:78-84` and `backend/app/ws/events.py:60-65` both state "the client resyncs on reconnect" (including the DROP_SENTINEL slow-consumer eviction, which closes the socket). Fanout is ephemeral — no replay buffer.
- **Web:** `apps/web/src/lib/ws.ts:29-32` — `onopen` only resets attempts and flips state; `apps/web/src/stores/run.ts:129-131` — the state handler sets only `socketConnected`. Events/threads/run are fetched exactly once in `openRun` (`run.ts:62-66`). Nothing ever refetches the run row, so a missed `run_stage` (e.g. E3 reaper death → `run_manager.py:313` publishes FAILED) strands the UI on a stale non-terminal stage forever; missed `step`s are permanent holes until re-open; missed dead-letter `note` toasts vanish. Partial self-heal: threads via 15s poll (`SessionsScreen.tsx:97-100`), approvals via 15s `refetchInterval` (`ApprovalQueue.tsx:92`). Minor sub-points: no jitter (lockstep reconnects after a backend restart) and no give-up/surfaced "offline" state beyond the "…" LED.
- **Impact:** any blip or eviction silently holes the live stream and can leave the pipeline saying "running" after the run died — exactly the death-visibility the remediation built.
- **Fix:** on reconnect, refetch `/runs/{id}` + `/events?after_seq=<max-seq>` (supported at `runs.py:144`, `sessions.py:66-67`) + threads, and invalidate approvals.

### HIGH 3 — `input_required` thread status unknown to the web
**Status: CONFIRMED.**
- **Backend:** set by the worker (`worker/worker/engine/runner.py:242,474`, heartbeat at `:243`), persisted by `backend/app/services/heartbeats.py:199`, treated as alive/nudgeable (`run_manager.py:470-476`) and capacity-consuming (`semaphores.py:35`). Reaches the web only via REST threads serialization (`runs.py:180-188`) — heartbeats.py never publishes WS `thread_status`.
- **Web:** absent from `LaneStatus` (`apps/web/src/types/index.ts:24-26`); blind cast at `stores/run.ts:93`; LED maps fall back to "off" (`ThreadChips.tsx:8-18,56`, `ThreadTile.tsx:8-24,51`) so a parked-but-alive thread renders as a dead gray LED with no pulse; `ThreadOverlay.tsx:12` excludes it from `live`, hiding stop/kill for a thread the backend considers nudgeable; `isStaleThread`/`criticalThreadIds` (`runMachine.ts:69,95`) ignore it (consistent with backend `ACTIVE_STATUSES`, but a parked thread whose card was missed via finding 2 has no watchdog and no visible affordance).
- **Fix:** add `input_required` to `LaneStatus`, give it a warn LED + pulse, include it in overlay `live`, and refetch approvals when a thread enters it.

### MEDIUM 4 — No dedupe/ordering guard in the store; replay silently capped at 500
**Status: CONFIRMED (code); exploitation today is latent → RISK.**
- **Web:** `stores/run.ts:82-87` appends `msg.event` unconditionally — no `(thread_id, seq)` dedupe, no gap detection; `foldStream` (`runMachine.ts:172-184`) renders in array order with keys `e-{thread}-{seq}-{role}` (a duplicate would double-render AND collide React keys). `openRun` uses the server default `limit=500` (`sessions.py:55,68`) with no pagination — runs past 500 stored events render first-500 + live tail with a silent middle hole.
- **Backend:** idempotent ingest (`bus.py:220-232`) + relay-before-ack (`bus.py:271-277`) make backend duplicate fanout unlikely. But two seq allocators still exist (worker seq store, `worker/engine/events.py:44-61`; backend `thread.next_seq`, `runs.py:48-50` ratcheted at `bus.py:234-235`): a user message racing a worker event can collide on `(run_id,thread_id,seq)`, and the WORKER event is then deduped-and-acked without store or relay — a permanent, undetectable hole.
- **Fix:** upsert by `(thread_id, seq, role)`, detect seq gaps and trigger `after_seq` catch-up, paginate replay. (Must land with or before finding 2's resync.)

### MEDIUM 5 — Ghost "typing…" bubble forever on hard worker death
**Status: CONFIRMED.**
- **Web:** deltas clear only when the matching stored `step` lands (`run.ts:82-87`) or on open/closeRun; the `run_stage` handler (`run.ts:100-120`) does not clear them; `foldStream` marks them `live:true` (`runMachine.ts:201-222`) and `Thinking` renders `open` while live (`EventStream.tsx:76-79`).
- **Backend:** on heartbeat-timeout death the reaper stamps failed (`heartbeats.py:96-101`) and terminal stage is published (`run_manager.py:313`) — the in-flight turn's stored event never arrives.
- **Impact:** failed run + permanently-open "thinking…" bubble simultaneously.
- **Fix:** clear `deltas` on terminal `run_stage` (and on socket drop when REST stage is terminal).

### MEDIUM 6 — Spawn lifecycle invisible: queued notice uses a fake thread_id; new threads wait for the 15s poll
**Status: CONFIRMED.**
- **Backend:** `thread_manager.py:277-278` publishes `thread_status` "queued" keyed by `spec.get("thread_hint", spec["persona"])` — a persona hint, not a thread id (the L-22 fake-id pattern, still alive here; the comment at `:257` claims "the UI says so"). Real spawns publish `thread_status` for the NEW id (`:193`) with no accompanying `run_stage`; `spawn_done` is control-channel only (`spawn_bridge.py:216`); no queue-position message exists despite the FIFO-visibility note (`spawn_bridge.py:18-20`).
- **Web:** `stores/run.ts:90-95` updates only EXISTING threads — unknown ids dropped silently; new threads surface via the 15s `refreshLanes` poll or a coincidental `run_stage` refetch (`run.ts:118-120`).
- **Fix:** refetch threads on unknown-`thread_id` `thread_status`; backend should send queued notices via `publish_note`.

### LOW 7 — `repo_added` global broadcast silently dropped
**Status: CONFIRMED.** `services/repos.py:219` + relay comment `relay.py:126` ("invalidates the repo-list query") vs no handler in `run.ts:78-127` and no global socket (only per-run, `ws.ts:26`). Masked by the ReposScreen 4s poll (`ReposScreen.tsx:55`). **Fix:** handle it with `invalidateQueries(qk.repos)` or delete the claim.

### LOW 8 — No `schema_version` guard / fail-loud on the WS leg; the "runtime handshake" does not exist
**Status: CONFIRMED for web; brief corrected.** Grep finds NO runtime schema handshake anywhere in worker or backend (only pins/tests), and the Event model (`db/models/event.py:44-52`) has no `schema_version` column despite `contracts/events.py:16`. Web: `schema_version` optional and never read (`types/index.ts:48-49`); malformed frames swallowed (`ws.ts:36-38`); unknown message types fall through with no `else` (`run.ts:78-127`) — a v2 frame degrades silently, violating the contracts' consumer-guard rule (`events.py:15-16`). **Fix:** log/surface unknown types; guard `schema_version !== 1`.

### LOW 9 — D5 redaction parity holds only on the custom engine; legacy SDK path streams unredacted
**Status: CONFIRMED (code); worker-side, flagged because it ripples into the web delta leg.** `worker/engine/graph.py:409-418` redacts deltas (the D5 test pins only this path — `worker/tests/test_event_integrity.py:50-58`); `worker/normalize.py:193-194` does NOT redact and imports no redactor, yet `worker/main.py:45,77` still wires `Normalizer` as the flag-gated fallback. Web renders the `«REDACTED»` marker (`security.py:28`) as inline text sanely; the "redacted chip" the docstring promises (`security.py:9`) is unimplemented (cosmetic).

### LOW 10 — Tests pin stale/fictional contracts; new vocabulary unpinned
`ws.test.ts` (backoff/close) and `eventStream.test.tsx` (rendering) remain accurate. But `actionCard.test.tsx:27` / `runMachine.test.ts:30-35` pin the fictional `stop_run`-in-actions contract (masks BLOCKER 1); nothing tests `note`, approval WS messages, `input_required`, unknown-status degradation, reconnect resync, or step dedupe — the store's entire WS handler (`run.ts:74-131`) has zero direct tests.

### LOW 11 — Minor store/API gaps
`createRun` never sends `idempotency_key` (`run.ts:194-207`) though the backend supports it (`runs.py:73-75,106`) — a retried submit can double-mint a run. Unbounded `events`/`deltas` arrays + O(n) `foldStream` per token (`runMachine.ts:161-224`) grow memory/CPU on long runs.

## Verified OK
- **Vocabulary:** relay emits exactly `step`/`thread_status`/`run_stage`/`delta`/`note` (`relay.py:53-71,124`) + `approval_card`/`approval_resolved` (`approvals.py:151-153,195,283-285`); all seven handled in `run.ts`. No new post-remediation WS types exist — dead-letter alerts reuse `note` → toast works (`bus.py:96` → `run.ts:121-126`); `spawn_done` is control-channel only.
- **Approval shapes:** card `{id,kind,payload,thread_id|null}` (`approvals.py:161-166`) and resolved `{approval_id,decision}` match `WsMessage` (`types/index.ts:66-70`); REST `expires_at` matches (`api/approvals.py:76`); 15s polling self-heals missed cards.
- **Stages:** `RunStage` identical both sides (`contracts/intents.py:16-28` vs `types/index.ts:3-6`); unknown stage degrades safely (`runMachine.ts:30-31`). `available_actions` trusted from server (`run.ts:106-116`); post-decide stage re-publish (`approvals.py:286-298`) keeps UI/DB agreement.
- **Delta leg:** envelope `{type:"delta", delta:{run_id,thread_id,kind,text,…}}` (`relay.py:124`, `forwarder.py:83`) matches; extras ignored; step-supersedes-delta filter (`run.ts:82-87`) correct.
- **Store race hygiene:** openRun generation token (`run.ts:37,58,69,77`), closeRun cancel (`:143-148`), H-61/M-81 stale-run guards (`:161,184`), M-80 functional updater (`:106-116`) all correct.
- **Ingest idempotency** (`event.py:40-41`, `bus.py:220-232`) means the backend won't double-fanout — missing web dedupe is latent, not active. User messages persist + push live once (`runs.py:339-351`) — no double-render.

## Summary
agentW2 (ws/event stream/store): 11 findings. BLOCKER: run-level Stop/Abandon are unreachable — the backend never ships `stop_run`/`abandon_run` in `available_actions` because it assumes the UI hardcodes Stop (`services/runs.py:11`), but `ActionCard` renders only server-sent actions, and tests pin the fictional contract. HIGH: (1) no resync on WS reconnect although `relay.py:79` and `ws/events.py:63` explicitly assume "the client resyncs on reconnect" — missed steps/stages/notes become permanent holes, defeating E3 death visibility; (2) `input_required` is absent from `LaneStatus` and every LED/live map, so approval-parked threads render dead and lose overlay controls. MEDIUM: no seq dedupe/gap detection plus a silent 500-event replay cap; ghost "typing…" deltas never cleared on terminal stage; spawn lifecycle invisible (fake-id queued notice dropped, new threads wait on the 15s poll). LOW: `repo_added` dropped, no `schema_version` guard (no runtime handshake exists anywhere), D5 redaction missing on the legacy SDK path, stale tests, no idempotency key on `createRun`. Message vocabulary, approval payloads, stage vocabulary, delta envelope, and store race hygiene verified OK.

Now write the full report as my final message.

agentW3:

# agentW3 audit — run lifecycle controls & sessions screen

## Findings (ranked)

### BLOCKER 1 — Run-level Stop and Abandon are unreachable from the web UI
**Status:** CONFIRMED
**Web evidence:** `ActionCard.tsx:70-87` renders only the `actions` prop; `SessionsScreen.tsx:322-327` passes `current.available_actions` verbatim. No component hardcodes a Stop/Abandon button (grep for `stop_run`/`abandon_run` in `apps/web/src` hits only `ActionCard` labels + tests). The typed-text fallback ("stop" → STOP_RUN) is unreachable because the composer always sends `intent="send_message"` with text (`stores/run.ts:171-178`), so `classify_text` never runs (`runs.py:255` requires `body.text and not body.intent`).
**Backend evidence:** `services/runs.py:11` — "The ONE control always visible (Stop) is hardcoded by the UI — never in this list"; `ACTIONS_BY_STAGE` (`runs.py:12-28`) contains no `stop_run`/`abandon_run`; `gate_intent` makes both always-legal (`intents.py:76-80`), so the backend expects the UI to offer them.
**Impact:** The remediated, robust stop/abandon paths cannot be invoked at all from the web. A runaway run can only be stopped per-thread via `ThreadOverlay.tsx:18`.
**Fix:** Hardcode `stop_run` (always) and `abandon_run` (overflow, two-tap) into `ActionCard`'s rendered set, as the backend contract assumes.

### BLOCKER 2 — VERIFYING stage is wedged: its only actions are hidden
**Status:** CONFIRMED
**Web evidence:** `runMachine.ts:41-43` includes `"verifying"` in `AGENT_WORKING`; `ActionCard.tsx:57,72-73` hides every non-Stop button while `busy`; the header (`SessionsScreen.tsx:192-202`) only maps plan/merge_pr buttons — nothing renders `review_evidence`/`create_pr`.
**Backend evidence:** `services/runs.py:21` — VERIFYING offers exactly `[review_evidence, create_pr]`; post-remediation VERIFYING is human-parked and explicitly excluded from the boot sweep (`run_manager.py:842-845`), so it persists.
**Impact:** A run parked at VERIFYING shows zero action buttons; "Open PR" is unreachable, so the run can never advance to `pr_ready` from the UI — it looks (and is) stuck.
**Fix:** Remove `"verifying"` from `AGENT_WORKING` (it is human-parked, not agent-working).

### BLOCKER 3 — ActionCard "Resume" button is a silent no-op
**Status:** CONFIRMED
**Web evidence:** `ActionCard.tsx:25,70-87` renders "Resume" whenever `resume_run` is in `available_actions`; `SessionsScreen.tsx:326` fires `sendIntent("resume_run")`; `sendIntent` refetches and shows no error on 200 (`stores/run.ts:179-185`).
**Backend evidence:** `resume_run` is advertised for INTERRUPTED and FAILED (`services/runs.py:24,26`) and passes the gate (`intents.py:76-82`), but `post_intent` (`api/runs.py:287-490`) has **no RESUME_RUN branch** — it falls through to `return {"status": "ok"}` at `runs.py:490`. The real resume lives at POST `/sessions/{id}/resume` (`api/sessions.py:77-92`).
**Impact:** On interrupted/failed runs the user clicks "Resume", gets a success-shaped response, and nothing happens — the refetched stage is unchanged. Wedged with no feedback.
**Fix:** Route `resume_run` to `/sessions/{id}/resume` in the UI (or add a backend intent handler that calls `run_manager.resume_run`).

### HIGH 4 — "Edit & resend" always fails 422
**Status:** CONFIRMED
**Web evidence:** `ActionCard.tsx:26,59-66` fires `edit_and_resend` with no text; `onFire` (`SessionsScreen.tsx:326`) only forwards `confirmed`.
**Backend evidence:** `runs.py:470-472` raises 422 "edit_and_resend needs the edited text".
**Impact:** The second interrupted-stage action also always fails — the user gets a `edit_and_resend failed` toast on every click. Both advertised INTERRUPTED actions are broken in the UI.
**Fix:** Wire an edit affordance (prefill composer, send as `text`) or drop the button.

### HIGH 5 — Resume banner gated on `!working`, not on real resumability + stage; silent no-ops; no error path
**Status:** CONFIRMED
**Web evidence:** `SessionResume.tsx:24` (`enabled: !working`), `:28` (any thread `resumable`), `:37-47`; `working = agentWorking(stage)` (`SessionsScreen.tsx:144`) excludes queued/awaiting_user/pr_ready. `resume()` (`SessionResume.tsx:30-33`) has no try/catch and the button (`:39`) has no in-flight disable.
**Backend evidence:** Resume is stage-guarded — a non-terminal/non-interrupted run is refused by returning the run unchanged (200, no-op) (`run_manager.py:200-204`); a survived-container abort raises `RuntimeError` (`run_manager.py:231-234`) which `api/sessions.py:77-92` does not catch → 500.
**Impact:** (a) "continue session" shows on queued/awaiting_user/pr_ready runs where the click is a silent 200 no-op; (b) a brand-new queued run with zero threads shows "replay only — this session can't be resumed (its workspace expired)" — nonsense on the main screen; (c) a real resume failure (500) surfaces as an unhandled promise rejection with no toast.
**Fix:** Gate the banner on `stage ∈ {interrupted, completed, failed, abandoned}` plus `resumable`, and toast on error.

### MEDIUM 6 — No in-flight disable on action buttons against now-slow ack-gated actions
**Status:** CONFIRMED
**Web evidence:** `ActionCard.tsx:76-86` — no `disabled`/pending state; `SessionsScreen.tsx:326` fire-and-forget; no optimistic flip (good) but also no spinner.
**Backend evidence:** stop/replace now block on ack (10s/thread, `control.py:36-54`) plus container-exit verification, sequentially per thread (`run_manager.py:381-382`); kill/replace fails 422 when the old container won't die (`runs.py:447-455` catching `ValueError` from `run_manager.py:569-571`; note the `RuntimeError` at `run_manager.py:618-621` escapes as 500).
**Impact:** Double-click during a multi-second stop/kill-replace double-POSTs; second kill_replace → 422 toast, stop re-runs harmlessly — but the user gets zero feedback while the request hangs.
**Fix:** Per-button pending state until `sendIntent` resolves.

### MEDIUM 7 — Runs list never refetches: background-run death shows "running" forever
**Status:** CONFIRMED
**Web evidence:** `loadRuns` only on mount (`SessionsScreen.tsx:81-83`) and after `createRun` (`stores/run.ts:197`); WS `run_stage` updates only `current` (`stores/run.ts:100-120`); the 15s tick refreshes only threads (`SessionsScreen.tsx:95-102`).
**Backend evidence:** Hard death is real post-remediation — reaper stamps the thread failed (`heartbeats.py:99-101`) and blueprints flip the run to FAILED + republish (`ask.py:127-138`).
**Impact:** Tab-strip/history LEDs (`SessionTabs.tsx:69,141`) for any non-open run are stale indefinitely — a dead run shows a live lamp until full remount.
**Fix:** Refetch `/runs` on any `run_stage` WS message or on a slow poll.

### MEDIUM 8 — `input_required` thread status unknown to the web; parked threads render as dead
**Status:** CONFIRMED
**Web evidence:** `LaneStatus` lacks it (`types/index.ts:24-26`); LED maps fall back to `led--off` (`ThreadChips.tsx:56`, `ThreadTile.tsx:51`); `ThreadOverlay.tsx:12` excludes it from `live`, hiding stop/kill-replace; `isStaleThread` ignores it (`runMachine.ts:69` — correct).
**Backend evidence:** The worker sets it (`worker/worker/engine/runner.py:242,474`) and the heartbeat persister mirrors it to the DB (`heartbeats.py:195-200`); backend treats it as live/stoppable/nudgeable (`run_manager.py:375,476`; `semaphores.py:35`). Note: no WS `thread_status` is published for it — the browser learns only via the 15s lane poll.
**Impact:** An approval-parked thread shows an "off" LED (looks dead) and loses its overlay controls, though the backend considers it alive.
**Fix:** Add `input_required` to `LaneStatus`, LED/pulse maps, and the overlay `live` set.

### LOW 9 — Illegal-intent `ValueError` escapes as 500, not 409/422
**Status:** CONFIRMED
**Web evidence:** `sendIntent` toasts `err.message` (`stores/run.ts:186-191`) — for a 500 that's "Internal Server Error".
**Backend evidence:** `post_intent` catches only `IntentNeedsConfirmation` (`runs.py:280-284`); `gate_intent`'s `ValueError` (`intents.py:81-82`) propagates → 500.
**Impact:** A button clicked from a stale stage snapshot (e.g. Approve after the run already advanced) yields an opaque 500 toast. UI recovers; message is useless.
**Fix:** Catch `ValueError` in `post_intent` → 409/422 with the detail.

### LOW 10 — `createRun` never sends the new idempotency key
**Status:** CONFIRMED
**Web evidence:** `stores/run.ts:28,194-207` — body type and POST lack `idempotency_key`; composer double-click is busy-guarded (`SessionsScreen.tsx:376`).
**Backend evidence:** Key accepted + honored (`runs.py:75`; `run_manager.py:135-166`).
**Impact:** Network retries / double-tabs can still mint duplicate runs.
**Fix:** Generate a UUID per composer draft and send it.

### LOW 11 — Tests pin pre-remediation fictions
**Status:** CONFIRMED
**Web evidence:** `runMachine.test.ts:30-35` feeds `stop_run` inside `available` (the backend never sends it) and `:37-41` pins `agentWorking("verifying") === true` (the BLOCKER 2 wedge); `actionCard.test.tsx:25-32` same `stop_run` fiction; `visibleActions` (`runMachine.ts:46-49`) is dead production code — only the tests import it. `consoleParity.test.tsx` pins card kinds only — no stage drift.
**Impact:** Tests lock in the wedged VERIFYING behavior and a Stop-button contract that doesn't exist, masking BLOCKERs 1-2 in CI.
**Fix:** Rewrite fixtures to backend-real `available_actions`; update or delete `visibleActions`.

## Verified OK

- **Stage vocabulary 1:1** — web `RunStage` (`types/index.ts:3-6`) == contracts (`intents.py:16-28`) == `ACTIONS_BY_STAGE` keys (`services/runs.py:12-28`); unknown stages degrade gracefully (`runMachine.ts:30-31`, `PipelineBar.tsx:14` — no crash, all-todo rail).
- **IRREVERSIBLE sets match exactly** (`ActionCard.tsx:9` vs `intents.py:77-79`); two-tap confirm sends `confirmed=true`; the backend `{"status":"confirm"}` card path is unreachable from the web (composer always sends typed `send_message`).
- **No optimistic stage flip** — `sendIntent` refetches after the POST resolves (`stores/run.ts:179-184`); WS `run_stage` applies authoritatively with the M-80 functional updater.
- **Cost display null-safe** — `cost_usd` non-nullable end-to-end (`run.py:57`, `runs.py:92`, `types/index.ts:18`); `.toFixed(2)` safe (`SessionTabs.tsx:159`, `ThreadTile.tsx:62`); settled-cost-on-terminal needs no UI change.
- **Double-resume safe server-side** — row lock + stage guard → idempotent 200 no-op (`run_manager.py:192-204`); `openRun` generation token absorbs double `onResumed` (`stores/run.ts:37,57-69`).
- **Terminal-stage stop/abandon rejected server-side** (`run_manager.py:370-371,419-420`; `services/runs.py:43-45`) — UI cannot resurrect a terminal run.
- **Hard worker death reaches the open run** — blueprint flips FAILED + republishes `run_stage` (`ask.py:127-138`); WS handler updates stage + refreshes threads (`stores/run.ts:100-120`).
- **Watchdog terminal suppression** matches backend semantics (`runMachine.ts:62-64,80`).
- **Queue position: nothing ignored** — no position field exists in any API/WS payload (`runs.py:87-95,180-188`; `relay.py:56-71`); spawn-queue position is internal-only (`spawn_bridge.py:19`). Brief's H6 expectation is not actionable; queued renders on-rail with info tone.

## Summary

agentW3 (run lifecycle UI): the web is badly stale against the remediated lifecycle. Stop/Abandon are unreachable (backend expects a UI-hardcoded Stop that doesn't exist); VERIFYING wedges with its only actions hidden by `agentWorking`; ActionCard's Resume is a no-op (no backend intent handler) and Edit & resend always 422s; the resume banner uses `!working` heuristics → silent no-op clicks and a nonsense "workspace expired" line on fresh queued runs, with resume 500s unhandled. Medium: no in-flight disable on now-slow ack-gated actions, stale run list, `input_required` rendered as dead. Stage vocabulary, cost display, confirm flow, and double-resume handling verified sound.

Now write the full report.

agentW4:

# agentW4 — Approvals UI audit

## Findings (ranked)

### HIGH-1 — "always allow" is offered on destructive cards and silently does less than it says
**Status: CONFIRMED**
- Web: `apps/web/src/components/ApprovalQueue.tsx:149-156` renders "always allow" unconditionally; `:135-137` already has `payload.destructive` in hand (badge) and the engine sends `always_allowable: false`, but neither gates the button.
- Worker/backend: post-G4, `always_allow` for a destructive `terminal_exec` is honored **for this call only, never persisted** — `worker/worker/engine/graph.py:509-517`, `worker/worker/engine/approvals.py:96-108` (`via: always_allow_unpersisted`), `worker/worker/approvals.py:106-120`. The API accepts it and records `always_allow` in the audit row (`backend/app/api/approvals.py:28-29`, `services/approvals.py:272-275`) — 200 OK, no signal back.
- **Impact:** the human believes they whitelisted the tool class; the next identical destructive command re-asks. Silent intent shortfall, and the audit row says `always_allow` for something that was allow-once. (Brief correction: the backend does **not** reject with an error — it degrades silently; and there is no `file_delete` tool — `MUTATING_TOOLS = [file_edit, file_write, terminal_exec]`, `worker/worker/engine/tools/mutating.py:236`. Destructive = destructive `terminal_exec` only.)
- **Fix:** hide/disable "always allow" when `payload.always_allowable === false` (fall back to `payload.destructive`), with a "never auto-allowed" hint.

### HIGH-2 — Stop/abandon/fail leaves zombie approval cards clickable for up to 15 min; deciding them 200s into a void
**Status: CONFIRMED**
- Web: `ApprovalQueue.tsx:119` renders purely on list length (no stage check); `GET /approvals` only excludes expired rows (`backend/app/api/approvals.py:72-77`).
- Backend: `stop_run` (`backend/app/orchestrator/run_manager.py:363-408`), `abandon_run` (`:410-458`), `stop_thread` (`:506-516`), and the kill_replace old-thread path (`:588-598`) stamp **threads** only — no Approval row is stamped and no `approval_resolved` is fanned out (the only fanout sites are `services/approvals.py:151,283`). G5 wakes the worker's BLPOP (`worker/worker/engine/runner.py:139-143,392-397`) but the DB card + UI are never told; `expires_at` is creation + 900s (`services/approvals.py:182`, `core/config.py:120`).
- **Impact:** after Stop, a card sits in the queue looking live; clicking "allow once" returns 200, RPUSHes to a dead BLPOP key, nothing executes. Kill_replace is fine (replacement re-waits on the same approval_id, `runner.py:226-244`).
- **Fix:** on terminal thread/run transitions, stamp pending approvals `denied`/`stopped` and fan out `approval_resolved`.

### MED-1 — `edited_allow` is fully wired backend→worker but unreachable from the UI (dead feature)
**Status: CONFIRMED**
- Web: no occurrence of `edited_allow`/`edited_args` anywhere in `apps/web/src` except a display-only test fixture (`__tests__/consoleParity.test.tsx:50-52`); the card is read-only (`ApprovalQueue.tsx:59-85`), decision type is `"allow_once" | "always_allow" | "deny"` (`:97`).
- Backend/worker: end-to-end support exists — `api/approvals.py:28,35,42-43` (422 without `edited_args`), `services/approvals.py:235,281-282`, `events/control.py:82-87`, `worker/worker/engine/approvals.py:219`, and the graph executes the edited args (`worker/worker/engine/graph.py:497-507`).
- **Impact:** G1 reconciled the vocabulary but the console can't send it; "edit args before allowing" — the safety-valve feature — doesn't exist for humans.
- **Fix:** add an edit affordance on command cards posting `{decision: "edited_allow", edited_args}`.

### MED-2 — Decide-after-expiry returns 200 `decision:"timeout"`; the UI shows success and never tells the human
**Status: CONFIRMED**
- Web: mutation discards the response (`ApprovalQueue.tsx:96-98`), optimistically removes the card (`:99-104`), no toast on success or error (`:105-115` re-inserts silently). Buttons are never disabled on expiry; `expiryLabel` (`:46-53`) only recomputes on the 15s refetch — no ticking countdown.
- Backend: expired decide stamps `timeout` and returns it as a normal 200 (`services/approvals.py:264-271`, `api/approvals.py:110-112`).
- **Impact:** human clicks "allow once" on a just-expired card → UI behaves as if allowed; the tool was actually denied. Window is bounded by the 15s poll (GET filters expired), but the misleading-success path is real.
- **Fix:** read `decision` from the decide response and toast when it isn't what was clicked.

### MED-3 — Knowledge approval cards leak into the session queue; tool vocabulary wedges them
**Status: CONFIRMED (latent web→backend ripple)**
- Backend: knowledge drafts create `Approval` rows with `kind="knowledge"`, real `run_id` when sourced from a run (`services/knowledge.py:105-111`); `GET /approvals` does not filter by kind (`api/approvals.py:60-77`).
- Web: queue renders them as a JSON dump with allow once/always allow/deny. Clicking stamps the row `allow_once`, RPUSHes to a BLPOP nobody listens on (`services/approvals.py:278-282`), and the linked `KnowledgeItem` stays `draft` — real approval requires scope via `knowledge.approve` (`services/knowledge.py:205-229`); the card is now decided so `_resolve_linked_approval` (`:194-202`) can never match it.
- **Impact:** human thinks they approved knowledge; nothing happened; the card's linkage is burned.
- **Fix:** filter non-tool kinds out of the session queue (or route them to the knowledge inbox).

### MED-4 — `input_required` thread status is now mirrored backend-side but missing from web `LaneStatus`; parked threads render with a dead LED
**Status: CONFIRMED**
- Backend/worker: runner heartbeats `input_required` during approval waits (`worker/worker/engine/runner.py:242-243`); heartbeat service persists it (`backend/app/services/heartbeats.py:195-200`); it's in the DB active-status index (`db/models/thread.py:71`).
- Web: `LaneStatus` omits it (`apps/web/src/types/index.ts:24-26`); WS handler casts blindly (`stores/run.ts:93`); `ThreadTile.tsx:51` and `ThreadChips.tsx:42,56` fall back to `"led led--off"` with no pulse.
- **Impact:** exactly while a thread waits on the human, its tile/chip looks stopped/dead — attention misdirected away from the blocker.
- **Fix:** add `input_required` to `LaneStatus` + LED maps (warn tone).

### LOW-1 — 409 conflict on cross-device decide resurrects the card with zero explanation
**Status: CONFIRMED**
Different-decision re-drive → 409 (`services/approvals.py:259-260`, `api/approvals.py:108-109`); UI `onError` re-inserts the card silently (`ApprovalQueue.tsx:105-115`) until `onSettled` refetch removes it. Transient and self-healing, but no error is ever surfaced (contrast `sendIntent` toast, `stores/run.ts:187-190`). Same-decision re-drive is correctly idempotent-200 (G6 holds).

### LOW-2 — Legacy SDK bridge cards (`{tool, input}` payload) render as raw JSON wire dump
**Status: RISK (flag-gated path)**
`worker/worker/approvals.py:69` XADDs `{"tool", "input"}` — `ApprovalPayload` checks `args`/`command`/`cmd`/`preview` but not `input.command` (`ApprovalQueue.tsx:59-85`). The SDK runtime is flag-gated but still wired (`worker/worker/main.py:38-42,80`). These cards also lack `destructive`/`always_allowable`, compounding HIGH-1.

### LOW-3 — Empty ActionCard strip while `awaiting_user`
**Status: CONFIRMED (cosmetic)**
Card creation publishes actions `["allow_once","always_allow","deny_tool"]` (`services/approvals.py:189-190`); `ActionCard.tsx:14,70` filters all three, rendering an empty bordered strip. Also note the offered action is `deny_tool` while the queue posts `deny` (audit cosmetic only).

## Verified OK

- **Decision vocabulary round-trips:** UI's three strings are all in `_VALID_DECISIONS` (`api/approvals.py:28-29`); `_WORKER_DECISION` mapping (`services/approvals.py:231-240`) lands in the broker's accepted set (`worker/worker/engine/approvals.py:219-220`) and the gate (`graph.py:503`). No 422 on any UI-sent decision.
- **Concurrent double-decide:** row lock (`services/approvals.py:249-250`) + idempotent same-decision return; the UI's optimistic removal + re-insert is compatible.
- **Expiry listing:** `GET /approvals` filters expired rows (`api/approvals.py:72-77`) and the sweep fans out `approval_resolved` (`services/approvals.py:149-153`) — expired cards vanish within ~15s even without WS.
- **Re-driven cards dedupe:** M-34 PK skip (`services/approvals.py:174-184`) + WS invalidate → refetch (`stores/run.ts:96-99`) — no duplicate cards after container replacement/backend restart (G7 XAUTOCLAIM path is backend-internal, UI-safe).
- **Cross-device resolve:** `approval_resolved` invalidates the query; GET excludes decided rows — decided-elsewhere cards vanish.
- **`awaiting_user`:** WS paint only (relay `publish_run_stage`, `events/relay.py:68-71`), never persisted; G8 restores real stage from DB post-decide (`services/approvals.py:286-298`); `RunStage` type, RAIL and `stageMeta` all include it (`types/index.ts:4`, `runMachine.ts:14,28-29`).
- **Tests:** `approvalQueue.test.tsx:39` pins `allow_once` (still valid); `actionCard.test.tsx:8` uses a valid stage; `consoleParity.test.tsx:45-52` pins the current emitter fields (`worker/worker/engine/events.py:88-97`). No test pins stale vocabulary — but none covers destructive/always-allow or expiry either (coverage gap).

## Summary

agentW4 (approvals UI): decision strings round-trip cleanly, but the UI is stale against four remediation shifts. HIGH: "always allow" is offered on destructive cards and silently degrades to allow-once-unpersisted (200 OK, misleading audit row); stop/abandon never resolves pending cards, leaving clickable zombies whose decide RPUSHes into a void. MEDIUM: `edited_allow` is fully plumbed backend→worker yet has no UI; expired decide returns 200 `timeout` the UI ignores; knowledge cards leak into the tool queue and wedge; `input_required` is missing from web types so parked threads show a dead LED. Tests pin only the happy-path vocabulary.

agentW5:

# agentW5 audit — thread sidebar / tiles / chips / overlays

## Findings

### BLOCKER

**B1. `edit_and_resend` is advertised but can never succeed — dead button**
- Status: CONFIRMED
- Web evidence: `apps/web/src/components/ActionCard.tsx:26` (label "Edit & resend"), fired via `apps/web/src/features/sessions/SessionsScreen.tsx:326` — `onFire={(intent, confirmed) => void sendIntent(intent, { confirmed })}` sends **no text**. No edit UI exists anywhere in the web app.
- Backend evidence: M6 wired it end-to-end — `backend/app/api/runs.py:465-485` handles `EDIT_AND_RESEND` but 422s without `intent.text` (`runs.py:470-472`); advertised for INTERRUPTED runs at `backend/app/services/runs.py:24`. It is not in `IRREVERSIBLE_INTENTS` (`packages/contracts/src/collegium_contracts/intents.py:77-79`), so no confirm gate saves it.
- Impact: every interrupted run shows a button that always fails with a 422 toast ("edit_and_resend needs the edited text").
- Fix: add an edit box pre-filled with the last user message that fires `sendIntent("edit_and_resend", { text })`, or drop the action from the card.

### HIGH

**H1. `input_required` is a live backend status; the web renders it as dead and strips thread controls**
- Status: CONFIRMED
- Web evidence: `LaneStatus` union lacks it (`apps/web/src/types/index.ts:24-26`). `ThreadTile.tsx:8-18` LED map has no key → runtime fallback `"led led--off"` (line 51; `.led--off` = gray/dead, `theme/index.css:274-278`) and no river pulse (`PULSE`, lines 20-24). Same fallback in `ThreadChips.tsx:42,56`. `ThreadOverlay.tsx:12` computes `live` as `["running","queued","idle"]` → stop / kill & replace buttons hidden while parked. `isStaleThread`/`criticalThreadIds` (`lib/runMachine.ts:69,95`) also exclude it.
- Backend evidence: worker sets `status = "input_required"` (`worker/worker/engine/runner.py:242,474`), heartbeat-mirrored into the Thread row (`backend/app/services/heartbeats.py:199-200`); the row reaches the web via `/runs/{id}/threads` (`backend/app/api/runs.py:182`) on the 15s `refreshLanes` poll (`SessionsScreen.tsx:95-102`). Backend treats it as alive/nudgeable/stoppable (`run_manager.py:375,476`; `semaphores.py:35`).
- Impact: a thread parked on an approval card looks like a dead gray tile and loses its controls precisely while it waits on a human.
- Fix: add `input_required` to `LaneStatus`, map it to a warn/blue LED + pulse, and include it in ThreadOverlay's `live` set.

**H2. kill & replace failure path: opaque 500, tile already dark, no replacement**
- Status: CONFIRMED (backend-primary, web-visible)
- Web evidence: `ThreadOverlay.tsx:30-39` fires `kill_replace` with `confirmed: true` on a single click, no busy disable; failure surfaces only as the store toast (`stores/run.ts:187-190`).
- Backend evidence: `run_manager.py:586` stamps the old thread `"replaced"` **before** the container-exit check; if the old container survives, `run_manager.py:619` raises `RuntimeError`, but `runs.py:452` only converts `ValueError` → 422, so it escapes as a 500 with no JSON detail (web shows "kill_replace failed: Internal Server Error", `lib/api.ts:34-42`).
- Impact: the tile flips to `replaced` (LED off, `ThreadTile.tsx:16`), no replacement chip appears, and the user gets an unexplained 500 — a wedged-looking swarm.
- Fix: raise `ValueError` (or stamp `replaced` only after verified exit) backend-side; disable the button in flight web-side.

**H3. Capacity-queued swarm threads are completely invisible — no row, no position, signal dropped**
- Status: CONFIRMED
- Web evidence: store's `thread_status` handler requires an id match (`stores/run.ts:92-94`); the web `Thread` type and `/threads` payload have no queue position (`types/index.ts:28-41`, `runs.py:180-188`).
- Backend evidence: `thread_manager.py:87-89` raises before any Thread row exists, and the retry loop's only signal is `publish_thread_status(run.id, thread_hint/persona, "queued")` (`thread_manager.py:277-278`) with a fake id — exactly the misuse the relay docstring says the UI silently drops (`events/relay.py:59-66`). The L-22 note channel is used only for fanout capping (`blueprints/swarm.py:122`), not capacity queuing.
- Impact: during the queue wait the sidebar/tiles show fewer threads than requested with zero explanation — indistinguishable from a spawn bug.
- Fix: publish a run note (or create the row as `queued` before the wait) so queued threads render with a queued state.

### MEDIUM

**M1. PROverlay's sha256 never matches the PR-body hash and changes every fetch**
- Status: CONFIRMED
- Web evidence: `PROverlay.tsx:61` displays `pkg.sha256` from `/runs/{id}/evidence`.
- Backend evidence: the hashed package includes `"generated_at": datetime.now()` (`services/delivery.py:113`); `open_pr` hashes at PR time (`delivery.py:234`) while `run_evidence` rebuilds and rehashes per request (`api/runs.py:236-241`).
- Impact: the "tamper-proof" hash shown in the overlay cannot be verified against the PR body and is unstable across reloads — the audit display is misleading.
- Fix: serve the stored `PrLink.evidence` hash instead of rehashing a fresh build.

**M2. ThreadOverlay destructive controls: no confirm, no in-flight disable**
- Status: CONFIRMED
- Web evidence: `ThreadOverlay.tsx:18-38` — `stop_thread`/`kill_replace` fire on single click (`kill_replace` pre-confirmed), `void sendIntent(...)` with no busy state; ActionCard's two-tap pattern (`ActionCard.tsx:59-66`) is not applied here.
- Backend evidence: double-fire is rejected with `ValueError("thread already terminal…")` → 422 (`run_manager.py:569-571`, `runs.py:452-453`), surfaced as a toast.
- Impact: accidental one-click kill of a worker; double-click produces a confusing 422.
- Fix: two-tap confirm + disable while the intent is in flight.

### LOW

- **L1. `ThreadSidebar` is dead code with a stale vocabulary** — CONFIRMED. Referenced only by its own test (grep: no app importer). Its status union (`ThreadSidebar.tsx:12`) omits `replaced`/`pinned`/`input_required`, and `STATUS_LED[t.status]` (line 70) has no fallback, so an unknown status renders the base `.led` = green/ON (`theme/index.css:256-263`). `__tests__/threadSidebar.test.tsx` pins this stale union. Fix: delete the component+test or align it with `LaneStatus`.
- **L2. "pinned" is a ≤15s cosmetic flash** — CONFIRMED. `pin_finding` publishes WS status `"pinned"` (`run_manager.py:542`) but never stamps the row, so the next `refreshLanes` poll reverts the LED. Harmless but misleading.
- **L3. Replay silently truncates long runs** — CONFIRMED. `replay_events` caps at 500 (`services/sessions.py:54`) in `(thread_id, seq)` order; `openRun` (`stores/run.ts:65`) never pages `after_seq`, so ThreadOverlay transcripts of long runs show only the earliest events. Pre-existing.
- **L4. PROverlay merge button stays live after a successful service-account merge** — CONFIRMED. `handoff_url` is null on that path (`run_manager.py:818-819`), so an open overlay keeps the merge button; re-click 422s ("no open PR", `delivery.py:296`). The header button does disappear via the `run_stage` update (`SessionsScreen.tsx:198`).
- **L5. Test coverage gap** — CONFIRMED. `threadChips.test.tsx`/`swarmView.test.tsx` exercise only `running`; nothing pins the `input_required` fallback that H1 describes.

## Verified OK

- **PlanOverlay payload**: `/runs/{id}/plan` serialization (`api/runs.py:210-221`) matches `PlanPayload`/`PlanStep` (`types/index.ts:75-95`) field-for-field; approve/reject ride the ActionCard into `runs.py:400-416`; step tones degrade to neutral for `pending`.
- **PROverlay evidence shape**: `EvidencePackage` (`PROverlay.tsx:10-19`) is a subset of `build_evidence_package` (`delivery.py:110-128`); extra fields harmless. PR deep link is the backend-supplied `handoff_url` (`pr_web_url`, `delivery.py:270-275`) — no client-built ADO URL, no golden-clone reference, no branch-name display anywhere in the slice.
- **Thread wire shape**: `/runs/{id}/threads` (`runs.py:180-188`) matches the web `Thread` exactly, including `heartbeat_at`/`has_container`/`finished_at`; `cost_usd` now populated on stopped/failed flows straight through.
- **lane_id→thread_id rename**: zero `lane_id`/`laneId` matches in `apps/web/src` (only cosmetic names like `refreshLanes`/`laneFilter`).
- **Transcript hydration**: `foldStream` (`lib/runMachine.ts:161-224`) assumes neither seq-origin-0 nor gaplessness; role-suffixed keys tolerate the dual seq allocators; replay rows omit `schema_version` and the type marks it optional (`types/index.ts:49`). Ingest dedupe by (run, thread, seq) (`events/bus.py:223-232`) prevents duplicate agent events.
- **Liveness**: hard-dead threads flip to `failed` via the reaper (`heartbeats.py:99`) and reach the UI within one 15s poll; the watchdog banner is suppressed on terminal runs (`runMachine.ts:77-82`); no stale-cache "running" persists beyond a poll interval.
- **IRREVERSIBLE parity**: `ActionCard.tsx:9` exactly matches contracts `intents.py:77-79`.
- **Feed/Viewer**: `Viewer` is reachable only via `Feed`, and `Feed` is used only in tests — dead but harmless (its `CARD_META[kind]` unknown-kind crash is unreachable in the live app).

## Summary

agentW5 (threads/sidebar/overlays): 1 BLOCKER — `edit_and_resend` is advertised on interrupted runs but the web never sends text, so it always 422s (backend wired it; UI never got the edit box). 3 HIGH — `input_required` is absent from `LaneStatus`, so approval-parked threads render as dead gray tiles and lose stop/kill controls; kill_replace's container-survival failure escapes as a 500 after the tile is already stamped `replaced`; capacity-queued swarm threads are invisible (no row, fake-id status dropped by the store, no queue position). MEDIUM: PROverlay's sha256 is recomputed per fetch over `generated_at`, so it never matches the PR-body hash; thread controls lack confirm/busy states. LOW: dead ThreadSidebar with stale vocabulary, transient "pinned" flash, 500-event replay truncation, stale tests. Plan/evidence/thread wire shapes, deep links, and the thread_id rename verified clean.

Write the final report.

agentW6:

# agentW6 audit — composer / mentions / command palette

## Findings (ranked)

### BLOCKER 1 — Swarm mode (`agent-rnd`) is offered by the UI but every swarm run fails at thread spawn (post-C3 regression)
- **Status:** CONFIRMED
- **Web evidence:** `apps/web/src/features/sessions/SessionsScreen.tsx:38` (`{ value: "agent-rnd", label: "swarm" }` in MODE_OPTIONS), `:52` (swarm suggestion card), `:357-374` (fanout input).
- **Backend evidence:** `backend/app/sandbox/manager.py:128` (`WORKER_MODES = {"ask","plan","development","debug","goal"}` — no `agent-rnd`), `:144-149` (`thread_env` raises `InvalidModeError` on `run.mode`); `backend/app/orchestrator/blueprints/swarm.py:136-140` spawns the Lead with `ctx.run` (mode `"agent-rnd"`); `backend/app/orchestrator/thread_manager.py:167-178` wraps it as `ThreadSpawnError("container start failed: …")`; `backend/app/orchestrator/run_manager.py:287-313` marks the run FAILED. The `Mode` row (`backend/app/auth/seed_users.py:170-178`) has no engine-mode reconciliation field. Mid-session `switch_mode` → `agent-rnd` hits the same wall (`backend/app/api/runs.py:337`).
- **Impact:** The UI prominently offers a mode that can never complete a single thread; user sees "routing…" then a failed run.
- **Fix:** Reconcile `agent-rnd` → a worker-vocabulary mode (e.g. `ask`) in `thread_env`, or add it to the worker `Mode` enum (`worker/worker/engine/state.py:30-35`).

### BLOCKER 2 — No idempotency key sent; Enter key bypasses the busy guard → duplicate runs
- **Status:** CONFIRMED
- **Web evidence:** `apps/web/src/stores/run.ts:28` (createRun body type: `mode/task/repo/fanout` only), `:196` (POST body has no `idempotency_key`); `apps/web/src/features/sessions/SessionsScreen.tsx:336-341` (Enter handler checks only `task.trim()` — never `busy`), `:298` (ticket buttons call `start()` with no busy disable), `:376` (only the *button* is `disabled={busy …}`).
- **Backend evidence:** `backend/app/api/runs.py:75,106` accepts and forwards `idempotency_key`; `backend/app/orchestrator/run_manager.py:135-166` dedupes/replays by it; `backend/app/db/models/run.py:35-38` partial unique index. All keyed on a field the web never sends — H5 protection is inert for the UI.
- **Impact:** Double-Enter (or double-clicking an ADO ticket) on a slow network mints two runs, double-spending budget — exactly what H5 was meant to kill.
- **Fix:** Generate `crypto.randomUUID()` per composer session, send it as `idempotency_key`, and add `!busy` to the Enter handler.

### BLOCKER 3 — "Resume" action-card button is a silent no-op
- **Status:** CONFIRMED
- **Web evidence:** `apps/web/src/components/ActionCard.tsx:25` (`resume_run: "Resume"`), fired via `apps/web/src/features/sessions/SessionsScreen.tsx:322-327` → `sendIntent("resume_run")`.
- **Backend evidence:** `backend/app/services/runs.py:24,26` advertise `resume_run` for INTERRUPTED/FAILED; `packages/contracts/src/collegium_contracts/intents.py:64` defines it; but `backend/app/api/runs.py:286-490` has **no `elif kind == ActionKind.RESUME_RUN`** — it falls through to `return {"status": "ok"}` (:490) doing nothing. The working resume is a different endpoint, `POST /sessions/{id}/resume` (`backend/app/api/sessions.py:77-90`), used by `SessionResume.tsx:31`.
- **Impact:** On an interrupted/failed run the most obvious recovery button returns "ok" and does nothing.
- **Fix:** Handle `RESUME_RUN` in `post_intent` via `run_manager.resume_run`, or strip it from `available_actions`.

### BLOCKER 4 — No Stop control for a running run anywhere in the web UI
- **Status:** CONFIRMED
- **Web evidence:** `apps/web/src/components/ActionCard.tsx:70` renders only the `actions` prop; `apps/web/src/features/sessions/SessionsScreen.tsx:324` passes `current.available_actions` verbatim. `apps/web/src/lib/runMachine.ts:44-49` has `ALWAYS_SHOW = {"stop_run"}` / `visibleActions()` but nothing calls it. `apps/web/src/__tests__/actionCard.test.tsx:27` passes `stop_run` in `actions` — a state the backend never produces (false confidence).
- **Backend evidence:** `backend/app/services/runs.py:11-12` — "The ONE control always visible (Stop) is hardcoded by the UI — never in this list"; `ACTIONS_BY_STAGE` (:12-28) omits `stop_run` for every stage; `gate_intent` always allows it (`backend/app/services/intents.py:76-80`).
- **Impact:** Both sides assume the other provides Stop; a wedged or runaway run cannot be stopped from the UI (only per-thread stop inside `ThreadOverlay.tsx:18`).
- **Fix:** Union `stop_run` into ActionCard's list while the run is non-terminal (use `visibleActions`).

### HIGH 5 — Command palette "New run" navigates to the public landing page; ⌘N is browser-reserved
- **Status:** CONFIRMED
- **Web evidence:** `apps/web/src/components/CommandPalette.tsx:88-91` (`go("/")`) vs `apps/web/src/lib/routes.ts:5-9` ("`/` is the public landing page"; sessions = `/app`). The ⌘N handler (:56-60) targets the right path but browsers never deliver Cmd/Ctrl+N to pages, so it's dead; the palette hint `⌘N` (:95) advertises it anyway.
- **Impact:** The palette's only working "new run" affordance kicks the user out of the console to the marketing page.
- **Fix:** `go(SCREEN_PATHS.sessions)`; pick a deliverable shortcut (e.g. ⌘J) or drop the hint.

### HIGH 6 — Run-creation failure reasons (unknown @mention, cap, repo conflict) surface only as a transient toast
- **Status:** CONFIRMED — also **corrects the brief**: POST `/runs` never returns 409/queued; the run is created QUEUED (`run_manager.py:152`) and the conflict fails *asynchronously* at spawn (`thread_manager.py:87-89` → `run_manager.py:287-313`).
- **Web evidence:** `apps/web/src/stores/run.ts:121-127` handles WS `note` as `toast()` only; `runMachine.ts:161-224` / EventStream never render notes.
- **Backend evidence:** `run_manager.py:294-295` publishes the failure text via `relay.publish_note`; `backend/app/events/relay.py:59-66` fans out to live sockets only — nothing is persisted. The comment at `run_manager.py:291-293` ("the event stream renders it inline") is false for the web.
- **Impact:** Miss the toast (or reload) and a failed run shows "failed" with zero explanation; cap/conflict rejections are indistinguishable from crashes.
- **Fix:** Persist notes as run events (or store a `failure_reason` on the run) and render an inline banner.

### HIGH 7 — Sending a message on an interrupted/completed/failed run black-holes silently
- **Status:** CONFIRMED
- **Web evidence:** composer is always live (`SessionsScreen.tsx:330-343`); `send()` (:126-142) fires `send_message` regardless of stage.
- **Backend evidence:** `nudge_thread` refuses terminal threads and only logs (`run_manager.py:487-490`); the user message is still persisted and WS-broadcast (`backend/app/api/runs.py:339-353`), so it *looks* delivered; response is `{"status":"ok"}`. On INTERRUPTED runs the only real paths are `edit_and_resend`/`resume_run` — both broken from the web (BLOCKER 3, MEDIUM 8).
- **Impact:** User's follow-up appears in the transcript but no agent ever receives it; the run stays wedged with no signal.
- **Fix:** Disable/route the composer by stage, or have the backend 422/409 so the UI can say so.

### MEDIUM 8 — "Edit & resend" fires without text → guaranteed 422 (M6 half-wired)
- **Status:** CONFIRMED
- **Web evidence:** `ActionCard.tsx:26` label; `SessionsScreen.tsx:326` `onFire` sends no `text`; no edit UI exists.
- **Backend evidence:** `backend/app/api/runs.py:465-472` requires `intent.text` → 422 "edit_and_resend needs the edited text" → toast "edit_and_resend failed" (`stores/run.ts:186-191`).
- **Fix:** Wire an edit box prefilled with the last user message, or remove the button.

### MEDIUM 9 — `Composer.tsx` is dead code with stale vocabulary; the real composer is untested
- **Status:** CONFIRMED
- **Web evidence:** `Composer.tsx` is imported only by `apps/web/src/__tests__/composer.test.tsx:3` (no production import anywhere). Its modes (`Composer.tsx:32-37`) lack `debug`/`agent-rnd`; its payload fields `model`/`budgetUsd` (:24-30) have no counterpart in `CreateRunBody` (`backend/app/api/runs.py:66-75`) and would be silently dropped. `composer.test.tsx:74` asserts the stale 4-mode list. No `sessionsScreen.test.tsx` exists — the live composer (Enter/bypass/mode chips) has zero coverage.
- **Fix:** Delete `Composer.tsx` + its test, or wire it in and reconcile modes/payload.

### MEDIUM 10 — Typed-intent classification is bypassed; "approve"/"stop" typed in the composer become agent chit-chat
- **Status:** CONFIRMED (backend acknowledges it — `runs.py:264` — but the UI gives no hint)
- **Web evidence:** `SessionsScreen.tsx:137` always sends `intent: "send_message"`; `stores/run.ts:171-178` always pre-types the intent.
- **Backend evidence:** `classify_text` runs only when `intent` is absent (`backend/app/api/runs.py:255-262`; `intents.py:38-61`).
- **Impact:** Typing "approve" on an `awaiting_user` run nudges the Lead instead of approving; users must know to use the buttons.
- **Fix:** Send text-only (let the backend classify) or add a UI hint.

### LOW 11 — Mention dropdown offers non-usable repos
- **Status:** RISK
- **Web evidence:** `MentionTextarea.tsx:70-83` — no status filter.
- **Backend evidence:** `GET /repos` returns all non-archived (`backend/app/api/repos.py:49-58`), including `validating`/`cloning`/`error` (`backend/app/db/models/repo.py:24-37`); `resolve_run_repos` accepts any registered name (`backend/app/services/mentions.py:48-50`) → mentioning a still-cloning repo mounts a missing golden dir → container start failure.
- **Fix:** Filter the dropdown to `ready`/`ready-no-map` (field already in the payload).

### LOW 12 — Nudge placeholder over-promises; bare `@word` falls back to conversation with no warning
- **Status:** CONFIRMED (minor)
- **Web evidence:** `SessionsScreen.tsx:148` — "nudge the lead — it hears you mid-work…".
- **Backend evidence:** nudges inject at the **turn boundary**, not mid-turn (`worker/worker/engine/runner.py:24-27`); bare `@word` is conversation, only backtick-wrapped counts (`backend/app/services/mentions.py:16-18`). The dropdown enforces the backtick form on selection, but hand-typed `@Repo` silently does nothing scope-wise.
- **Fix:** Reword to "queued for the next turn"; optionally warn when a bare `@token` matches a repo name.

## Verified OK
- **Mention wire format:** `MentionTextarea.tsx:123` inserts `` `@Name` ``, matching `MENTION_RE` (`mentions.py:18`) exactly; dedupe/order handled backend-side (:21-29); unknown mention on a *follow-up* returns a surfaced 422 (`runs.py:384-389` → toast via `stores/run.ts:186-191`). Dropdown keyboard nav/Esc/keyup-resync tested and consistent (`mentionTextarea.test.tsx`).
- **Creation-time mode names:** all six UI modes have seeded enabled `Mode` rows (`seed_users.py:141-191`) — no unknown-mode 422 from POST /runs.
- **Error surfacing plumbing:** `api.ts:34-42` extracts `detail`; `createRun`/`sendIntent` toast it and reset `busy` in `finally` — no silent spinners.
- **Command palette** (except new-run): nav targets match `SCREEN_PATHS`; ⌘K toggle and logout wired correctly.
- **OverlayShell:** focus trap, ESC, and scroll-lock come from the base-ui Dialog (`OverlayShell.tsx:13-16`); LIFO pop (`stores/ui.ts:58`); stacking can't occur (no `pushOverlay` inside overlays).
- **Fanout NaN guard** (`SessionsScreen.tsx:363-371`) round-trips the `fanout: int | None` contract correctly.
- **openRun race guard** (H-60 generation token, `stores/run.ts:57-69`) intact.

## Summary
agentW6 (composer/mentions/palette): 4 BLOCKERs — swarm/agent-rnd mode fails at spawn post-C3 (`thread_env` rejects the mode the UI offers); no idempotency key sent and Enter bypasses the busy guard, so double-submit mints duplicate runs; ActionCard "Resume" is a silent no-op (`post_intent` never handles `resume_run`); no Stop button exists for a running run despite the backend contract assuming the UI hardcodes one. HIGH: palette "New run" navigates to the public landing page; run failures surface only as transient toasts; messages on interrupted/terminal runs black-hole. MEDIUM: edit_and_resend fires without text (guaranteed 422); `Composer.tsx` is dead code with stale modes while the real composer is untested; typed-intent classification is bypassed. Mention parsing, OverlayShell, and error-toast plumbing verified OK.

agentW7:

# agentW7 — feed & event renderers audit

## Findings

### BLOCKER

**B1. "Single-sourced seq" is false: dual allocators + the new unique constraint silently drop one worker event per user interaction — the feed loses it live AND in replay** — CONFIRMED
- Web evidence: `apps/web/src/lib/runMachine.ts:165-171` — the foldStream key comment admits, present tense, "the worker and the backend each run their own seq allocator".
- Backend evidence: `backend/app/api/runs.py:48-50` (`_persist_user_message` allocates `thread.next_seq`), also `run_manager.py:530-538` (pin), `blueprints/goal.py:575`, `blueprints/development.py:208`; worker evidence: `worker/worker/engine/events.py:41-61` + `runner.py:106-110` (own seq file, never re-seeded from the backend); drop path: `backend/app/events/bus.py:223-232` — IntegrityError on `uq_events_run_thread_seq` (`backend/app/db/models/event.py:40-41`) → acked and **returned before the relay at `bus.py:276`**.
- Impact: `bus.py:233-235` keeps `next_seq = worker_seq+1`, so in steady state the backend counter equals the worker's next seq; every backend-side event (user message, nudge, pin) steals exactly that seq → the worker's next event collides, is discarded as a "duplicate", never stored, never pushed over WS. The agent's first step after each user message vanishes from the feed permanently (worse on resume: N messages sent while stopped → N collisions). The remediation's D2 claim ("seq is single-sourced PER THREAD") does not hold wherever the backend writes events directly.
- Fix: route backend-originated events through the same allocator (or have the worker re-seed from `thread.next_seq` at turn start / include a per-event UUID the DB dedupes on instead of seq alone).

### HIGH

**H1. Every tool call renders as two feed cards (call + result), and edit calls are mis-kinded** — CONFIRMED
- Worker evidence: `worker/worker/engine/graph.py:336-337` publishes `from_assistant` events, which store one event per tool_call with detail `{tool, input}` (`events.py:155-162`); `graph.py:694-702` then stores a second event with `{tool, input, output, ok}`. `from_tool_result` pairing "is never called in production" (`graph.py:703-707`). This violates the contract's "emitted ONCE, COMPLETE, at step end" (`packages/contracts/src/collegium_contracts/events.py:5`).
- Compounding drift: `events.py:203-211` `_tool_kind` lacks `file_edit`/`file_write` → the call row is kind `command`; `graph.py:800-810` maps them to `file_edit` → the result row is kind `file_edit`. Identical titles, different rails.
- Web evidence: `apps/web/src/lib/runMachine.ts:172-184` — no pairing key exists in either payload (no `tool_call_id`), so the web cannot collapse the pair even if it wanted to.
- Fix: stop publishing the call-time event (or mark it transient), and unify the two `_tool_kind` maps.

**H2. Replay silently truncates long runs to the OLDEST 500 events** — CONFIRMED
- Web evidence: `apps/web/src/stores/run.ts:65` — `api.get("/runs/{id}/events")` with no params.
- Backend evidence: `backend/app/services/sessions.py:54-68` — `limit: int = 500`, `order_by(thread_id, seq).limit(500)` → ascending = oldest first; `after_seq` exists (`backend/app/api/runs.py:143-144`) but the client never uses it.
- Impact: after a reload, any run with >500 events shows only its beginning; the most recent work is invisible.
- Fix: pass `after_seq`/paginate (or raise the limit and fetch newest-first), and add catch-up on WS reconnect using the same parameter.

**H3. SDK legacy runtime has zero redaction — secrets stream live into the feed (D5 parity is custom-engine-only)** — CONFIRMED
- Worker evidence: `worker/worker/normalize.py:111, 117, 134, 176, 186-188, 193-194` — events and TypingDeltas carry raw `block.thinking`/`block.text`/tool output; no `redact` import. Contrast `graph.py:409-419` (D5 redacted deltas) and `events.py:133-185` (redacted events). The SDK path is still selectable: `backend/app/core/config.py:46-48` (`engine_runtime` default `"custom"`, `"sdk"` "kept alive"), wired at `worker/worker/main.py:45, 77`.
- Web evidence: deltas render verbatim — `apps/web/src/stores/run.ts:88-89` → `runMachine.ts:202-222` → `EventStream.tsx` Bubble/Thinking.
- Bonus same-path bug: `normalize.py:180-181` upgrades the *stored* event to `test_run` while the delta kept the pre-upgrade kind (`normalize.py:125`); the store's delta-clear keys on exact kind match (`run.ts:84-86`), so a pytest-via-Bash run leaves a stale "typing…" bubble forever.
- Fix: apply `redact` in `normalize.py` `_delta`/`_next` (or retire the SDK path); clear deltas on kind-upgrade.

**H4. No reconnect resync: a dropped/evicted socket permanently wedges the live feed until reopen** — CONFIRMED
- Web evidence: `apps/web/src/lib/ws.ts:29-32` — `onopen` resets backoff and flips a flag; no event refetch. `stores/run.ts:62-72, 133` — hydration fetch happens once, before `socket.connect()`, leaving a subscribe gap too.
- Backend evidence: `backend/app/ws/events.py:60-65` closes on `DROP_SENTINEL` with the comment "the client resyncs on reconnect" — the client never does. Slow-consumer eviction: `relay.py:77-95`. Additionally, a relay failure inside `_process` dead-letters *after* the DB commit (`bus.py:186-201, 271-277`), so the event is stored but never relayed — contradicting the D6 comment's "redelivery re-relays it".
- Fix: on WS open, refetch `/events?after_seq=<max seen>` (per thread) before marking connected.

### MEDIUM

**M1. The feed hides lifecycle events the engine emits specifically to be seen** — CONFIRMED
- Worker evidence: `runner.py:557-569` emits "nudge queued behind pending approval" (`detail.kind="nudge_deferred"`) with the explicit comment "Say so in the stream so the queued delivery is visible instead of silent"; `graph.py:1026-1030` emits "blocked-escalation" (`detail.kind="blocked"`).
- Web evidence: `apps/web/src/lib/runMachine.ts:128-155` — `isPlumbing` drops every status whose `detail.kind` is not in `DETAIL_KIND_MAP`; neither `blocked` nor `nudge_deferred` is mapped.
- Impact: a nudge sent during an approval wait appears to vanish; a critic-blocked run shows no terminal signal in the feed.
- Fix: map `blocked` → warning card and `nudge_deferred` → status/note card in `DETAIL_KIND_MAP`.

**M2. `input_required` missing from the web status vocabulary** — CONFIRMED
- Backend/worker evidence: worker heartbeats it (`runner.py:242, 474`), backend persists and relies on it (`heartbeats.py:199-200`, `run_manager.py:375`, `thread.py:71`).
- Web evidence: `apps/web/src/types/index.ts:24-26` — `LaneStatus` lacks it; `ThreadTile.tsx:51` degrades via `?? "led led--off"`, so a live approval-parked thread reads as dead/off; `isStaleThread`/`criticalThreadIds` (`runMachine.ts:68-95`) ignore it (benign but unexamined). ("stopped-acked" from the brief: no such status exists — stop acks stay internal; verified N/A.)
- Fix: add `"input_required"` to `LaneStatus` with an explicit LED/pulse (e.g. warn tone).

**M3. Unbounded feed growth: O(N) fold + O(N) tick per streamed token, no virtualization** — CONFIRMED
- Web evidence: `stores/run.ts:83` (append-only, never capped); `EventStream.tsx:223-229` (`foldStream` re-runs per delta), `EventStream.tsx:248` (`streamTick` reduce per render), full-list render at `294-301`; `aria-live="polite"` on the whole log (`284-286`).
- Mitigations present: per-item memo comparator (`EventStream.tsx:189-201`), so rows don't re-render — but the fold/reduce and DOM size grow linearly with no windowing; a multi-thousand-event run will jank and the initial mount parses/highlight everything at once.
- Fix: cap/window the rendered tail (or virtualize), and memoize `streamTick` per item.

**M4. Live vs replay ordering can disagree across threads** — RISK
- Backend evidence: replay merges lanes chronologically by ts (`sessions.py:44-51, 68-70`); live WS appends in ingest/arrival order.
- Web evidence: `runMachine.ts:172-223` never sorts by seq/ts; `stores/run.ts:83` appends.
- Impact: cross-thread interleave can reshuffle between a live session and its reload; within a thread, a late/out-of-order delivery renders at the tail (no seq sort anywhere on the client).
- Fix: insert by `(thread_id, seq)` instead of blind append.

### LOW

**L1. `Feed`/`Viewer` are dead production code carrying latent crashes** — CONFIRMED
- Evidence: only tests import them (`apps/web/src/__tests__/feed.test.tsx:3`, `consoleParity.test.tsx:9`); production uses `EventStream` (`SessionsScreen.tsx:236`, `ThreadOverlay.tsx:41`). An unknown kind crashes `Feed.tsx:80/95` (`CARD_META[kind]` undefined → `.rail` throws) and `Viewer.tsx:31/50`; `clipPreview` char-clips a >600-char single line with `more=0` and no marker (`cardTypes.ts:95-103`); `Feed.tsx:164` still says "11 card types" (15 exist); `file_write` is never emitted (`graph.py:806` maps it to `file_edit`) yet `cardTypes.test.ts:43-45, 79-81` enshrines it.
- Fix: delete or reintegrate Feed/Viewer; if kept, guard `CARD_META[kind]` with a fallback like EventStream's.

**L2. Stale fixtures: the diff-highlighting path for `file_edit` is dead** — CONFIRMED
- Web evidence: `eventStream.test.tsx:139-146` feeds a unified diff as `detail.text`; `EventStream.tsx:66-70` `codeLang` diff branch; `cardTypes.ts:88-93` comment claims "edit-preview hunks ≤10 lines".
- Worker evidence: `mutating.py:120-121` — the current `file_edit` output is `"edited … (N -> M chars). new hash: …"`; no diff is ever emitted in `detail`.
- Impact: edit cards render as one-line summaries; the test passes against a payload shape production never sends. Cosmetic drift, not a crash.
- Fix: either emit an edit-preview diff in `detail` (restoring the documented contract) or drop the diff branch + fixture.

## Verified OK

- **StepKind union matches the contract exactly** (10 kinds, no renames): `types/index.ts:43-45` vs `contracts/events.py:37-47`. `notebook` is never emitted by either engine path (dead but harmless); backend `pin` events (`run_manager.py:533-537`) bypass the enum and replay as `kind:"pin"` (`sessions.py:73`) — tolerated by the generic fallback (`EventStream.tsx:161`), noted as a type lie, not a break.
- **Detail payloads match current emitters**: todo-checklist (`graph.py:696-698` + `extended.py` artifact/tracker ↔ `cards.tsx:18-39`), compaction (`graph.py:890-903`), warning (`graph.py:384-395, 792-797, 1018-1022`), recap (`graph.py:1054-1065`), approval card/decision incl. `action_id`/`edited`/`destructive` (`events.py:82-116`); `consoleParity.test.tsx` fixtures are current.
- **Approval WS envelope + queue contract**: `approvals.py:161-166, 195` ↔ `types/index.ts:66-69`; decision vocabulary translation documented (`approvals.py:231-240`); card args are raw *by design* (verbatim approval, `cards.tsx:86-90`).
- **`schema_version`**: live WS carries it (`relay.py:54`), replay omits it (`sessions.py:71-75`), web marks it optional and reads only v1 fields (`types/index.ts:48-49`) — consistent with the contract guard rule.
- **Redaction (custom engine)**: events and deltas redacted (`events.py:133-185`, `graph.py:414-418`); marker is `«REDACTED»` (`security.py:28`) — renders as plain text, no `***` ambiguity; no raw-payload dump path in the feed.
- **XSS posture**: react-markdown ^9.0.1 (no rehype-raw; default `urlTransform` blocks `javascript:`), mermaid ^11.16.1 at `securityLevel: "strict"` (`MermaidRenderer.tsx:24`), sole `dangerouslySetInnerHTML` confined to that strict SVG (`MermaidRenderer.tsx:154`); no code-copy buttons exist to leak content; CodeView can't crash on unknown languages (`CodeView.tsx:173-181`, covered by `codeView.test.tsx`).
- **Unknown kinds in production degrade gracefully** (generic hairline rail, title+body): `EventStream.tsx:161, 175-183`.
- **Truncation honesty**: K10 markers (`[truncated — …]`, `[N lines; M total]`, `... N lines omitted ...`) are plain text and render as-is; CodeView's own `maxLines` footer is explicit (`CodeView.tsx:162-166`).
- **No "turn complete" coupling in the web**: untyped status events are deliberately hidden (`runMachine.ts:146-155`); the backend's identity capture keys on detail fields first (`bus.py:241-250`).
- **No duplicate WS delivery from dedupe itself**: redeliveries ack before relay (`bus.py:230-232`).
- **Scroll/memo behavior**: stick-to-bottom, own-message jump, ResizeObserver re-pin, and the field-wise memo comparator are correct (`EventStream.tsx:189-201, 250-276`).

## Summary

agentW7 (feed/renderers): the post-remediation backend moved; the feed did not. BLOCKER: dual seq allocators (backend `next_seq` vs worker seq-file) plus the new `(run_id, thread_id, seq)` unique constraint silently drop the agent's first event after every user message — missing live and in replay. HIGH: every tool call double-renders (call+result, mis-kinded for edits); replay truncates to the oldest 500 events so long runs lose their tail on reload; the still-selectable SDK path streams unredacted events/deltas (D5 gap) and strands a typing bubble on test-run upgrade; WS reconnect never resyncs, so evictions/gaps wedge the live feed. MEDIUM: engine's blocked/nudge-deferred cards are filtered as plumbing; `input_required` missing from LaneStatus; unbounded O(N)-per-token fold; live/replay ordering drift. LOW: Feed/Viewer are dead code with latent unknown-kind crashes; stale diff fixtures. Redaction parity (custom engine), approval payloads, schema_version handling, mermaid/markdown XSS posture, and truncation-marker honesty all verified OK.

Write the full report now.

agentW8:

## W8 Audit — dashboard / landing / swarm / nav

### Findings (ranked)

**MEDIUM 1 — `input_required` thread status is unknown to the web; parked threads render as dead**
Status: CONFIRMED
- Web evidence: `apps/web/src/types/index.ts:24-26` — `LaneStatus` omits `input_required`; `apps/web/src/components/ThreadTile.tsx:8-18` — `LED` map has no entry, so `LED[thread.status] ?? "led led--off"` (line 51) shows a parked-for-approval thread with the "off" LED; zero `input_required` matches anywhere under `apps/web/src`.
- Backend evidence: `backend/app/db/models/thread.py:71-74` (check constraint includes `input_required`), `backend/app/orchestrator/semaphores.py:35` (A4: parked threads still hold capacity/write lock), `backend/app/orchestrator/blueprints/swarm.py:303-305` (H-38+A4: blueprint await ends on `input_required`).
- Impact: post-hardening, an approval-parked swarm thread is a durable first-class state, but its tile looks terminated — exactly the "fleet health truth" drift the brief asks about. Watchdog/critical-path accidentally do the right thing (`runMachine.ts:69,95` only treat running/queued as active).
- Fix: add `input_required` to `LaneStatus` and give it a warn-tone LED/pulse in `ThreadTile`.

**MEDIUM 2 — Over-cap queued swarm slices are invisible: backend announces "queued" with a fake thread id the socket handler silently drops**
Status: CONFIRMED
- Web evidence: `apps/web/src/stores/run.ts:90-95` — `thread_status` updates match `l.id === msg.thread_id`; the announced id is a `thread_hint` like `explorer-0`, not a real Thread id, so the message is a no-op. No Thread row exists yet, so the 15s `refreshLanes` poll (`SessionsScreen.tsx:95-102`) can't see it either. `SwarmView` shows no queued tile, no queue position, no toast.
- Backend evidence: `backend/app/orchestrator/thread_manager.py:275-279` — `spawn_many` publishes `thread_status(run.id, thread_hint, "queued")` once per waiting spec, then retries in a `while True` loop against the 100 cap (`semaphores.py:63-66`). This is the same fake-id misuse remediation L-22 fixed for fanout-clamp notes (`swarm.py:117-124`, which correctly uses `publish_note` → toast via `run.ts:121-127`), still live in this path.
- Impact: when the global cap is saturated by other runs, a swarm fan-out just… hangs visually. The brief's "FIFO visible queue" is not visible in the web; no queue-position API exists to call either (spawn_bridge docstring points at XRANGE/XPENDING, redis-CLI only).
- Fix: re-publish queued notices via `publish_note` (or add a real queued placeholder Thread row) so the existing toast path surfaces them.

**LOW 1 — Newly spawned (post-C1) child threads don't appear in SwarmView until the 15s lanes poll**
Status: CONFIRMED
- Web evidence: `run.ts:90-95` drops `thread_status` for unknown ids; threads enter the store only via `openRun` fetch (`run.ts:62-66`), `run_stage`-triggered refetch (`run.ts:118-120`), or the 15s interval (`SessionsScreen.tsx:97-100`).
- Backend evidence: `thread_manager.py:193` publishes `"running"` for the new child id immediately on spawn; swarm spawns mid-run produce no `run_stage` transition.
- Impact: tiles pop in up to 15s late; cosmetically fine, no phantom-registry assumptions remain web-side.
- Fix: on an unmatched `thread_status`, fire one silent lanes refetch.

**LOW 2 — `finish_thread` never publishes `thread_status`; terminal flips ride the 15s poll**
Status: CONFIRMED
- Backend evidence: `thread_manager.py:352-382` stamps terminal with no relay publish; grep shows publishes only for running/failed/stopped/pinned/replaced/queued. E4/F1 now finish+settle explorers at run end (`swarm.py:275-289`), so several tiles go terminal simultaneously with no push.
- Web evidence: `run.ts:90-95` + 15s poll; watchdog correctly suppressed on terminal stage (`runMachine.ts:77-82`), so the worst case is a stale river-pulse for ≤15s.
- Fix: publish the final status inside `finish_thread`.

**LOW 3 — Dashboard never live-refreshes; cost-settle-everywhere makes it silently age**
Status: CONFIRMED
- Web evidence: `DashboardScreen.tsx:105-113` — no `refetchInterval`; `queryClient.ts:9-11` — `staleTime: 15_000`, `refetchOnWindowFocus: false`; grep shows no `invalidateQueries` for `qk.costStats`/`qk.deliveries` anywhere, and no WS handler touches them (the run socket only exists on the sessions screen).
- Backend evidence: `stats.py:14-54` unchanged shape; remediation settles cost on every terminal path (e.g. `swarm.py:279-289`, `thread_manager.py:284+`), so totals now move after the dashboard's only fetch.
- Impact: an open costs tab goes stale until remount; navigations back after >15s do refetch (default `refetchOnMount`), so self-heals on nav.
- Fix: a modest `refetchInterval` or invalidation from global bus events.

### Verified OK
- `/stats/cost` shape matches the web `Dashboard` interface field-for-field (`stats.py:46-54` vs `DashboardScreen.tsx:18-25`) — no renamed/removed/added fields; the brief's hypothesized new monitoring fields (queue depth, dead letters, leaked keys) do **not** exist in `stats.py`, so nothing is missed. Cost "jumps" are data-level, not schema breaks; `toFixed(2)` formatting is unaffected.
- `/deliveries` shape matches (`campaigns.py:116-123` vs `DashboardScreen.tsx:27-34`); stage buckets are arbitrary `Run.stage` strings and `Tag` tones degrade safely for any stage (`DashboardScreen.tsx:182`).
- Thread REST serialization matches web `Thread` exactly (`runs.py:180-188` vs `types/index.ts:28-41`); web `RunStage` union matches contracts exactly (`intents.py:16-28`).
- L-22 note channel fully wired end-to-end: `relay.py:59-66` → `swarm.py:122-124` clamp note → `WsMessage.note` (`types/index.ts:73`) → toast (`run.ts:121-127`).
- SwarmView consumes **real** DB child threads post-C1 (`spawn_bridge.py:148-151` → real rows in `/runs/{id}/threads`) — no phantom-registry assumptions web-side. `spawn_done` is correctly not a UI concern (parent control channel, `spawn_bridge.py:216`); vetoed spawns surface only worker-side (gap folded into MEDIUM 2's class).
- Landing: "Six blueprints" matches 6 registered blueprints (`mode_engine.py:25-31`); CTAs `/login` + `/app` exist (`router.tsx:162-183`); `#blueprints` anchor exists (`LandingScreen.tsx:221`); no parallelism claim contradicts cap 100 ("parallel lanes" is unquantified).
- SideRail/MobileTabBar have **no** approval/run-count badges (brief assumption corrected) — no stale-count risk here; approvals pending lives in `ApprovalQueue` with a 15s `refetchInterval` (`ApprovalQueue.tsx:92`), outside this slice.
- Tests: dashboard fixtures match live backend shapes (`dashboardScreen.test.tsx:11-26`); swarmView tests match current `runMachine` terminal-suppression semantics; landing tests match router reality. No stale fixtures found.
- Zero-total guards in cost bars/per-run math are correct (`DashboardScreen.tsx:70,74,116`).

### Summary
agentW8 (dashboard/landing/swarm/nav): no blockers — stats, deliveries, and thread schemas match the post-remediation backend exactly, and landing/nav claims hold (6 blueprints, valid CTA routes, no badge counts in the rail to decay). Two real gaps: `input_required` parked threads are absent from web types and render as dead LEDs, and over-cap queued swarm slices are announced via fake thread ids the socket handler silently drops (the L-22 anti-pattern still live in `spawn_many`). Three LOW refresh-latency items: new children, terminal flips, and dashboard totals all wait on polls/remount rather than pushes.

agentW9:

# agentW9 — admin/fleet screens audit

## Findings (ranked)

### HIGH

**H1. Idea → run "promote to plan" has no idempotency anywhere — double-click mints two plan runs** — CONFIRMED
- Web: `apps/web/src/features/ideas/IdeasScreen.tsx:145-147` — promote button has no `disabled` guard and never reads `send.isPending`; the mutation (`IdeasScreen.tsx:57-63`) sends `{}` with no dedupe key. Nothing changes after promotion either — the button still reads "promote to plan" on a `status: "promoted"` thread.
- Backend: `backend/app/api/ideas.py:92-103` — `promote()` calls `run_manager.create_run(...)` with no `idempotency_key` and no status guard; `backend/app/services/ideas.py:260-271` — `mark_promoted` unconditionally overwrites `status`/`promoted_run_id`. Contrast `backend/app/orchestrator/run_manager.py:135-141`, where idempotency only engages when a key is passed, and proposals' atomic claim (`backend/app/services/proposals.py:128-137`). The POST `/runs` idempotency the brief mentions (`backend/app/api/runs.py:75,106`) is bypassed entirely on this path.
- Stale test: `apps/web/src/__tests__/ideasScreen.test.tsx:67` is named "promote to plan posts and disables after promotion" but asserts only the POST — pinning a behavior that doesn't exist.
- Impact: every double-click / retried tap burns a second plan-mode run; budget double-spend.
- Fix: disable promote while pending and when `detail.status === "promoted"`, and add a claim guard (or idempotency key) in `api/ideas.py:promote`.

**H2. TeamScreen deactivate/regenerate failures are completely silent — new self-deactivate 422 never surfaces** — CONFIRMED
- Web: `apps/web/src/features/team/TeamScreen.tsx:99-102` (`deactivate`) and `93-97` (`regen`) have no try/catch; invoked via `void deactivate(u.id)` a rejection is an unhandled promise rejection — no toast, no inline error, no refetch. The deactivate button renders for the actor's own row too (`TeamScreen.tsx:186-195` checks only `status !== "deactivated"`), with no confirm dialog (unlike repo archive, `ReposScreen.tsx:96`).
- Backend: `backend/app/api/team.py:83-87` — L-14 self-deactivate now 422s with "cannot deactivate your own account"; missing user 404s (`team.py:88-91`); regen 404s (`team.py:72-78`).
- Impact: admin clicks "deactivate" on themselves, backend rejects, UI shows nothing — looks like a dead button; not wedged state, but zero feedback on an admin safety guard.
- Fix: wrap both mutations and surface `e.message` (a self-guard using the session user would be better).

### MEDIUM

**M3. ProposalsScreen swallows decide failures — spend-ceiling and already-decided 422s vanish** — CONFIRMED
- Web: `apps/web/src/features/proposals/ProposalsScreen.tsx:81` — `onError` only restores the cached list; no toast/inline error. The row silently reappears (when `!showAll`) with no explanation.
- Backend: `backend/app/services/proposals.py:150-151` ("weekly proposal spend ceiling reached ($X >= $Y)") and `119-120/136-137` ("proposal already decided") → 422 via `api/proposals.py:29-31`.
- Note: post-J4 the *state* handling is correct (see Verified OK #2) — this is purely the missing error surface.
- Fix: `toast.error` in `onError`, as KnowledgeScreen already does.

**M4. KnowledgeScreen has no reject path — the PHI inbox is approve-or-ignore, drafts pile up forever** — CONFIRMED
- Web: `apps/web/src/features/knowledge/KnowledgeScreen.tsx:67-97` renders only "approve"; a repo-wide grep finds zero web callers of `/knowledge/{id}/reject`. The per-run `ApprovalQueue` (`apps/web/src/components/ApprovalQueue.tsx:87-93`) can't pick these up either — it queries `/approvals?run_id=…`, and user-authored draft cards carry `run_id=NULL` (`backend/app/services/knowledge.py:98-111`).
- Backend: `POST /knowledge/{id}/reject` exists (`backend/app/api/knowledge.py:64-69`, `services/knowledge.py:234-248`) and row-locks correctly — dead-ended from the UI.
- Fix: add a reject button calling the endpoint (toast + invalidate like approve).

**M5. Knowledge approve defaults to the widest scope and can't show the proposed scope** — CONFIRMED
- Web: `KnowledgeScreen.tsx:35` — `useState("global")`; the draft card never displays what the distiller/user proposed.
- Backend: `services/knowledge.py:60-67` — `_serialize` omits `proposed_scope`; it exists only inside the Approval payload (`services/knowledge.py:105-111`), which `pending()` doesn't return. For a PHI checkpoint, defaulting to "global" with the proposal invisible is a real share-too-widely risk.
- Fix: serialize `proposed_scope` and initialize the selector from it.

**M6. Knowledge repo-scope repo name is free-typed; backend never validates it against the registry** — CONFIRMED (latent web→backend ripple)
- Web: `KnowledgeScreen.tsx:79-85` — bare `<Input placeholder="repo name">` (contrast ReposScreen's fetched branch picker).
- Backend: `services/knowledge.py:218-219` only checks non-empty; retrieval then filters exact repo equality (`services/knowledge.py:262`). A typo'd repo silently black-holes the item from every run's search space.
- Fix: backend-validate against `Repo` registry (or feed `qk.repos` into a picker).

**M7. Branch picker default is `branches[0]` — alphabetically first from `git ls-remote`, not the repo's default branch** — CONFIRMED
- Web: `ReposScreen.tsx:70` — `setBranch(data.branches[0] ?? "")`.
- Backend: `api/repos.py:61-70` / `services/repos.py:74-83` return raw ls-remote order (refname-sorted), so e.g. `19601-jwt-httponly` sorts before `develop`/`main`. The branch always passes onboard validation (it *is* on the remote), so a hasty click onboards a feature branch as `integration_branch` — and post-J5, re-registering the same name 409s (`api/repos.py:81-89`) until you archive, shredding the golden clone (`services/repos.py:226-244`). The test even pins the mock's unsorted order (`reposScreen.test.tsx:22-24, 47-48`).
- Fix: prefer `main`/`master`/`develop` when present, else `[0]`.

### LOW

- **L8 (brief correction). Concurrent duplicate onboard does NOT 409 — it 200s with the winner's row and spawns a second onboard task** — CONFIRMED. Both POSTs pass the pre-check (TOCTOU), the loser's `IntegrityError` returns the winner (`services/repos.py:109-121`), and `api/repos.py:92-97` still fires `onboard()` for both (lock-serialized, `services/repos.py:131-148`, clone guarded by `dest.exists()`). The sequential case does 409 as advertised. Web's `busy` guard (`ReposScreen.tsx:147`) covers one client only. Benign but contradicts "duplicate onboard → 409."
- **L9. 409 pre-check uses `.one_or_none()` on an OR filter** — RISK. `api/repos.py:81-84` can raise `MultipleResultsFound` → 500 instead of 409 if name-match and URL-match ever land on different rows (practically unreachable since the URL derives from the name, `api/repos.py:76`; legacy empty-`remote_url` rows are the edge). The service layer already fixed the same pattern (`services/repos.py:94-99`, L-17).
- **L10. Web never consumes the `repo_added` WS event the backend publishes** — CONFIRMED. Backend publishes it (`services/repos.py:209-223`); the web has only per-run sockets (`apps/web/src/lib/ws.ts:26`) and no global listener; with `refetchOnWindowFocus: false` and `staleTime: 15s` (`apps/web/src/lib/queryClient.ts:6-13`), another user's onboard/archive never appears until route remount. ReposScreen's 4s poll (`ReposScreen.tsx:52-59`) only runs while a repo it already sees is unsettled.
- **L11. IdeasScreen mislabels every mutation failure as "comment failed"** — CONFIRMED. `IdeasScreen.tsx:69-73` — ask-counsel 502/422 (`api/ideas.py:65-73`) and promote failures toast "comment failed".
- **L12. No pending state on ask-counsel (up to 30s LLM call, `services/ideas.py:179`) or promote** — CONFIRMED. `IdeasScreen.tsx:137-147`; the screen looks dead while the gateway thinks.
- **L13. ReposScreen shares one `error` state between add-form and archive** — CONFIRMED. An archive failure renders inside the open add-repo form (`ReposScreen.tsx:162` vs `166`).
- **L14. `reposScreen.test.tsx` mock lacks `api.patch`** — CONFIRMED. Mock at `reposScreen.test.tsx:8-10` provides only get/post; `ReposScreen.tsx:267` uses `api.patch` — the branch-edit flow is untested and would TypeError if exercised.
- **L15. Residual "accepting" strand on process crash** — UNVERIFIED/RISK. J4's rollback covers exceptions and the ceiling (`services/proposals.py:140-167`) but not a crash between claim and `create_run`; no reaper exists. Symptom: `showAll` renders the row as decided (`ProposalsScreen.tsx:113`) with no retry. Low probability, real wedge.
- **L16. Knowledge list fetch failures render as a silently empty inbox** — CONFIRMED. `KnowledgeScreen.tsx:105-112` — query errors leave `drafts = []`/`corpus = []` with no error UI (ReposScreen at least catches to `[]` deliberately at `ReposScreen.tsx:44`).

## Verified OK

1. **Duplicate-onboard 409 content surfaces** — `api.ts:34-42` maps `body.detail` into `ApiError.message`; `ReposScreen.tsx:87-89` displays "X is already registered on Y". Not a *distinct* 409 UX (no focus-the-existing-row), but functional.
2. **J4 retryability works** — failed accept: optimistic rollback (`ProposalsScreen.tsx:81`) + invalidation (`:82`) + server-side claim release (`services/proposals.py:156-167`) → row returns as actionable `proposed`. No local "accepting" wedge.
3. **Proposal vocabulary & show-all param** — `proposed/accepting/accepted/dismissed` consistent; `/proposals?status=` empty-string correctly disables the filter (`api/proposals.py:21`, `services/proposals.py:74`).
4. **PR-link rewrite (J1/J2) doesn't stale this screen** — proposals never had PR URLs; `promoted_run_id` still serialized (`services/proposals.py:39`) and rendered as text (`ProposalsScreen.tsx:161-165`). Test pins the accept response shape correctly (`proposalsScreen.test.tsx:31`).
5. **TeamScreen admin-gate detection matches backend** — regex `/403|forbidden|admin/i` (`TeamScreen.tsx:63-69`) vs detail "admin only" (`backend/app/core/security.py:117-121`); M-86 fix verified sound.
6. **Add-teammate payload shape** — `{username, display_name, ado_email}` matches `AddTeammateBody` (`api/team.py:30-33`); 422/502 identity-binding failures surface via `setError` (`TeamScreen.tsx:88-90`). Role vocab `admin|member` consistent (`services/team.py:47`).
7. **L2 concurrent knowledge decide handled gracefully** — `isPending` double-submit guard (`KnowledgeScreen.tsx:92`, M-85), toast with server detail on 422 (`:42-46`), and dual invalidation of drafts + corpus (`:47-50`) refetches post-M-37 truth.
8. **L5 corpus noise** — backend excludes rejected/mining markers (`services/knowledge.py:167-175`); UI badge shapes unaffected.
9. **Ideas comment M-84** — text cleared only on success, failure toasted (`IdeasScreen.tsx:68-73`); ask-counsel/promote routes and payloads match (`api/ideas.py:51-73, 92-103`).
10. **No pagination drift** — all five backends return full lists; every screen renders all; no limit/offset assumptions to break.

## Summary

agentW9 (admin/fleet screens): no BLOCKERs. Two HIGH: idea→run promote has no idempotency client- or server-side (double-click double-spends; the test name pins a disable behavior that doesn't exist), and team deactivate/regen fail silently against the new 422/404 guards. Six MEDIUM: proposal decide errors swallowed (J4 retry works but unexplained), knowledge reject endpoint dead-ended, approve defaults to global with proposed_scope invisible, free-typed repo scope black-holes items, branch picker defaults to alphabetically-first branch. Eight LOW including a brief correction: concurrent duplicate onboards 200+winner, not 409. J4, L2, L5, and 409 surfacing verified OK.

agentW10:

All verification complete. Here is my report.

---

# agentW10 — push / UI primitives / build-test harness

## Findings (ranked)

### BLOCKER

**1. Push deep links are dead on arrival — the M7 route has no web consumer** — CONFIRMED
- Web evidence: `apps/web/src/router.tsx:161-185` (no `approvals` route; `/app` index renders `SessionsScreen`); zero hits for `useSearchParams`/`URLSearchParams`/`location.search` anywhere in `apps/web/src`; `apps/web/src/lib/routes.ts:8-16` (`SCREEN_PATHS` has no `approvals`); `apps/web/public/sw.js:68-81` (notificationclick opens the payload URL blindly).
- Backend evidence: `backend/app/services/push.py:111-113` (`approval_deep_link` → `/app?screen=approvals&run={id}&card={id}`), `backend/app/services/approvals.py:219-222` (push sent on every approval card), `backend/tests/test_services_push_autonomy.py:33,49-50` (suite now green asserting `/app?…`).
- Impact: every notification tap opens `/app` → sessions screen; `screen`/`run`/`card` params are silently ignored. The "tap lands on the approval, not the inbox" design (push.py:8-9) and the opt-in copy "notifications deep-link straight to each card" (`ApprovalQueue.tsx:25`) never happen. M7 fixed the backend string; the web was never taught to parse it.
- Fix: read `screen/run/card` from the URL at `/app` (e.g. in `RootLayout`/`SessionsScreen`), call `openRun(run)` and highlight card `card`.

### HIGH

**2. `input_required` thread status reaches the UI and every renderer mislabels it** — CONFIRMED
- Web evidence: `apps/web/src/types/index.ts:24-26` (`LaneStatus` omits `input_required`); blind cast at `apps/web/src/stores/run.ts:93` (`msg.status as Thread["status"]`); `ThreadChips.tsx:8-18` + `ThreadTile.tsx:8-24` LED maps lack the key and fall back to `"led led--off"` (lines 42/56 and 51) — grey "dead" LED identical to `completed`/`replaced`; `ThreadTile` `PULSE` (20-24) gives it no pulse; `ThreadOverlay.tsx:12` treats it as not-live; dead-but-tested `ThreadSidebar.tsx:19-27,70` has NO fallback — `cn("led", undefined)` = base `.led` = green pulsing (`theme/index.css:256-264`).
- Backend evidence: worker emits it (`worker/worker/engine/runner.py:242,474`), it's persisted post-remediation (`backend/app/services/heartbeats.py:195-199`), relayed verbatim (`backend/app/events/relay.py:56-57`), DB-whitelisted (`backend/app/db/models/thread.py:71-74`).
- Impact: the one status meaning "a human must act now" renders as a dead thread in the swarm strip and composer chips — the approvals-hardening visibility win is lost on the primary monitoring surface (the docked ApprovalQueue card still shows, so not Blocker).
- Fix: add `input_required` to `LaneStatus` and give it a warn/amber LED + label in `ThreadChips`/`ThreadTile` (and a fallback in `ThreadSidebar`).

### MEDIUM

**3. Service worker never caches build assets — offline boot is broken, and the cache-first comment is false** — CONFIRMED
- `apps/web/public/sw.js:45-47`: the "cache-first" branch is `caches.match(...).then(hit => hit || fetch(...))` — on miss it fetches but never `cache.put`s. Only install-time `ASSETS` (line 4: manifest + icon) and navigations (line 36) are ever stored. Offline, the cached `index.html` (line 39) references `/assets/index-<hash>.js` which is not cached → blank page. No vite-plugin-pwa (`package.json`); SW registered prod-only (`main.tsx:20-21`).
- Impact: the header claim "app-shell cache for offline boot" (sw.js:1-2) doesn't hold; repeat-load acceleration claim (43-44) is inert.
- Fix: in the asset branch, `caches.open(SHELL).then(c => c.put(req, res.clone()))` on successful GETs of `/assets/*`.

**4. All push notifications share one tag — concurrent approval cards collapse** — CONFIRMED
- `apps/web/public/sw.js:62-63`: `tag: "collegium-ask"` hardcoded; backend payload (`services/push.py:86`) carries no tag. A second approval notification *replaces* the first in the shade; its distinct deep link is lost.
- Fix: derive the tag from the payload, e.g. `tag: data.url || "collegium-ask"`.

**5. `subscribeToPush` rejects unhandled when permission is denied / subscribe fails** — CONFIRMED
- `apps/web/src/lib/push.ts:36-39`: `reg.pushManager.subscribe(...)` sits outside the try/catch (which only covers the POST, lines 41-60). Call site `ApprovalQueue.tsx:30` does `void subscribeToPush().then(...)` with no `.catch`. Denied permission → DOMException → unhandled rejection, ask bar frozen in "ask", no user feedback. Same for a failed `/push/vapid-public-key` fetch (line 30).
- Fix: wrap the whole body in try/catch returning false, and/or `.catch(() => setState("hidden"))` at the call site.

**6. WS reconnect never resyncs — dropped steps stay missing until reopen** — CONFIRMED
- Web: `apps/web/src/lib/ws.ts:23-56` — reconnect only re-opens the socket; no `/runs/{id}/events` refetch on `onopen`; `stores/run.ts:129-131` onState only flips `socketConnected`.
- Backend: `relay.py:77-84` + M-53 sentinel design explicitly assume "the client resyncs on reconnect (steps are durable in the DB)" — the web doesn't.
- Impact: after any network flap or slow-consumer eviction, the live stream has permanent silent gaps.
- Fix: on reconnect `onopen`, refetch events (and threads) like `openRun` does.

### LOW

**7. sw.js API-prefix list drifted from the real mount set** — RISK (latent). `sw.js:22-25` omits `/push`, `/me`, `/knowledge`, `/ideas`, `/proposals`, `/campaigns`, `/deliveries`, `/stats`, `/bench`, `/webhooks` that `vite.config.ts:7-11` proxies. Harmless today only because misses are never cached (finding 3); if anyone "fixes" caching, these GETs become stale-cache-first. Fix: share one prefix list.

**8. No unsubscribe path** — CONFIRMED. Backend `DELETE /push/subscriptions` exists (`api/push.py:37-39`) but `apps/web/src/lib/api.ts:47-53` has no `delete` method and `push.ts` exports no unsubscribe; a user who opted in can never revoke from the UI (browser settings only). Fix: add `api.delete` + a toggle-off path.

**9. `repo_added` WS event unhandled** — CONFIRMED. Sent tenant-scoped post-D7 (`services/repos.py:219-220`); web `WsMessage` union (`types/index.ts:60-73`) doesn't model it and the handler (`stores/run.ts:78-127`) has no case; sockets exist only while a run is open. The "no refresh, no restart" intent (repos.py:2-4) is unmet; mitigated by ReposScreen's 4s polling and manual `refetch()`. Fix: handle `repo_added` → `invalidateQueries(qk.repos)`.

**10. RunSocket reconnects forever after 4401/4404** — CONFIRMED. `ws.ts:40-43` ignores `event.code`; post-logout the module-level socket (`stores/run.ts:31`, never closed on unmount) reconnect-loops against a dead session every ≤10s. Fix: don't reconnect on 4401/4404; close socket on session teardown.

**11. BranchPicker defaults to the alphabetically-first branch, not the remote HEAD** — CONFIRMED. `ReposScreen.tsx:70` `setBranch(data.branches[0] ?? "")`; backend `services/repos.py:83` returns raw `git ls-remote` order (refname-sorted) with no main/HEAD prioritization. `develop` beats `main` alphabetically → silent wrong default integration branch on registration. Fix: backend should sort HEAD/`main`/`master` first, or web should prefer them.

**12. History dialog overpromises vs the 100-run cap** — CONFIRMED. `SessionTabs.tsx:134` "every run you have started"; backend `api/runs.py:128` `limit(100)`, no cursor (and `ui/pagination.tsx` is dead code). Runs 101+ are unreachable in the UI. Fix: adjust copy or paginate.

## Verified OK

- **push.ts contract**: POST payload `{endpoint, keys}` matches `SubscriptionBody` (`api/push.py:18-20`); VAPID response `{public_key, enabled}` matches (push.ts:30-33 vs api/push.py:23-26); orphan-unsubscribe (G-27) correct and genuinely tested (`push.test.ts:69-84` exercises the real module).
- **RunStage**: web union (`types/index.ts:3-6`) is exactly the contracts enum (`intents.py:16-28`); `stageMeta` (`runMachine.ts:18-33`) covers all 12 incl. off-rail terminals; PipelineBar safe for unknown stages (`index === -1` guard).
- **filter-chips**: disabled-values, aria-pressed, tests fine; mode vocab matches seeded modes (`seed_users.py` ask/plan/development/debug/agent-rnd/goal); unknown `current.mode` degrades to no-highlight, no crash.
- **branch-picker**: a11y issues L-35/L-37 fixed; empty-match Enter guarded; repos API shape + `RepoStatus` vocabulary match `repoTone`/`SETTLED`.
- **data-table**: both live consumers (DashboardScreen, PlanOverlay) sort only non-null columns and use stable unique row keys; M-73/74/75 fixes sound; null-sort risk is latent only.
- **Approvals roundtrip**: web decision vocabulary ⊂ `_VALID_DECISIONS` (`api/approvals.py:28-29`); `Approval` type matches serialization incl. `expires_at`; H-59 rollback correct.
- **WS contract**: relay message shapes match `WsMessage` union; contracts StepEvent is a superset of the web type (`context_id`/`task_id` harmlessly ignored).
- **Harness**: `setup.ts`/`render.tsx` install no fetch/WS mocks — nothing masks contract drift; vite proxy prefixes cover all backend mounts incl. `/push`, `/me`; navigation bypass correct; `components.json` css path exists; `package.json` has no contract/client pins.
- **shadcn primitives** (select, command, dialog, input-group, tooltip, button, badge, tag, card, input, textarea, skeleton, page-head, file-icon, table, sonner, empty-state): no real defects; file-icon has a `generic` fallback. `pagination.tsx` + `ThreadSidebar.tsx` are dead code.

## Summary

agentW10 (push/primitives/harness): one BLOCKER — the M7-remediated push deep link `/app?screen=approvals&run=…&card=…` has no consumer: no route, and nothing in the app reads query params, so every notification tap lands on the generic sessions screen. One HIGH — the newly-persisted `input_required` thread status flows through a blind cast into LED maps that lack it, rendering "waiting on a human" as a dead grey dot. Mediums: sw.js never caches build assets (offline boot broken), one hardcoded notification tag collapses concurrent approvals, denied-permission subscribe throws unhandled, WS reconnect never resyncs. Lows cover prefix drift, missing unsubscribe, unhandled repo_added, and more. Harness is clean; no contract-masking mocks.

Confirmed: base `.led` = green pulsing (index.css:256-264). So in ThreadSidebar, an unknown status (`input_required`) → `cn("led", undefined)` → "led" → green pulsing "running" look. Latent (component unused in prod), but the ThreadChips/ThreadTile fallback to `led--off` (grey, dead-looking) is LIVE in production via SwarmView and the composer chips.

I have everything I need. Let me also double-check one thing about the push deep-link: is there any chance the SW notification URL is handled by a service-worker-level redirect... no. And confirm that SessionsScreen has no hash/query deep-link. Confirmed — no useSearchParams/URLSearchParams/location.search anywhere in src.

Also the diagnosis doc's claim in 30-agents-diagnosis.md:1508-1511 about red push tests appears FIXED now (test_services_push_autonomy.py:33 asserts /app?... matching push.py:113). Good — remediation completed backend-side; web side never consumed. That sharpens the BLOCKER.

Let me also verify the approvalCard push trigger: services/approvals.py:219-222 sends push on approval creation with deep link. Confirmed earlier.

Severity calls:
1. BLOCKER — Push deep links dead on arrival (M7 route has no web consumer).
2. HIGH — `input_required` thread status invisible/misrendered across thread primitives.
3. MEDIUM — sw.js never caches build assets → offline boot broken + comment lies about cache-first.
4. MEDIUM — sw.js hardcoded notification tag collapses concurrent approval notifications.
5. MEDIUM — subscribeToPush unhandled rejection on denied permission / subscribe failure (no try/catch, no .catch at call site).
6. MEDIUM — RunSocket reconnect never resyncs events (M-53's design premise false on web).
7. LOW — sw.js isApi prefix list drifted from vite.config API_PREFIXES (latent trap, harmless today).
8. LOW — No unsubscribe path: api lacks delete(); DELETE /push/subscriptions unreachable from UI.
9. LOW — repo_added WS event unhandled (design intent unmet; polling mitigates).
10. LOW — RunSocket reconnects forever on 4401/4404 (post-logout loop).
11. LOW — BranchPicker default = alphabetically-first branch (ls-remote order), not remote HEAD.
12. LOW — Session history copy says "every run you have started" but backend caps at 100.

Verified OK list:
- push.ts payload vs SubscriptionBody; VAPID endpoint shape; orphan-unsubscribe G-27 handling + test coverage.
- RunStage union matches contracts RunStage exactly; stageMeta covers all 12 with off-rail terminals.
- FilterChips: disabledValues, aria-pressed, test coverage; mode vocab matches seeded modes.
- Repo shape, remote-branches, repoTone/SETTLED vs RepoStatus vocabulary.
- Approvals: decision vocabulary within _VALID_DECISIONS; Approval type matches serialization incl. expires_at; optimistic rollback H-59 sound.
- DataTable consumers: no nullable sort columns live; rowKeys stable; M-73/M-74/M-75 fixes sound.
- WsMessage union vs relay message shapes (step/delta/thread_status/run_stage/note/approval_card/approval_resolved); contracts StepEvent ⊃ web StepEvent (extra context_id/task_id harmless).
- Test harness: no global fetch/WS mocks to mask drift; per-test api mocks; push.test.ts exercises real subscribeToPush.
- vite proxy prefixes ⊃ backend mount prefixes incl /push, /me; navigation bypass correct.
- select/command/dialog/tooltip/button/badge/tag/card/input/textarea/skeleton/page-head/file-icon/input-group/table: no real defects (file-icon has generic fallback; badge/tag tone maps have defaults).
- Pagination: unused component (dead code, no bug).
- capacitor.config: push-on-native documented as pending; not a drift.
- package.json: no contract/client version pins to drift.
- components.json: css path exists.

Word count for summary ≤120.

One correction to the brief worth noting: the brief said "push deep-link route was a fixed red suite (M7)" — verified: the backend test now asserts `/app?...` and passes; the failure is now purely web-side (no consumer). Also "status-lamp" itself takes tone+label props (no internal status map) — the status→tone coupling lives in runMachine.ts and the thread components, not the primitive.

-----

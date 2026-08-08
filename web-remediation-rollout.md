# Web Remediation Rollout Notes — backend↔web contract renegotiation

Companion to `web-harness-diagnosis.md` / `web_diagnosis.md`. All 8 waves implemented; gates below were run against the final tree.

## Gate results (this session)

| Gate | Result |
|---|---|
| Web `npm run test` (vitest) | 179 passed / 27 files (includes new: runStore WS suite, reconnect-resync, approval destructive/timeout/edited paths, deep-link parsing, idempotency-key emission, first-login, team guards, push sub/unsub, real-composer suite, feed windowing) |
| Web `npm run build` (tsc + vite) | clean |
| Backend pytest (unit) | 900 passed, 5 skipped |
| Backend `COLLEGIUM_INTEGRATION=1` tier (real pg 16 + redis) | 5 passed (`tests/integration`: pg races, redis redelivery) |
| Worker + contracts pytest | 297 passed, 1 skipped |
| `ruff check backend worker packages/contracts` | clean (24 remaining findings are pre-existing `packages/maps`, untouched) |
| CI | `azure-pipelines.yml` runs `npm run test && npm run build` for web — new suites are picked up automatically (single vitest glob); backend integration tier already wired |

## Fixture-honesty pass

Canonical regressions fixed: `actionCard.test.tsx` / `runMachine.test.ts` fixtures rewritten to backend-real `available_actions` (stop/abandon hardcoded in UI per W-B1, not pinned in fixtures); `Me.must_change_pin` removed from all fixtures; `pinned` removed from LaneStatus everywhere; the diff-grammar `file_edit` fixture dropped (W7-L2) with a guard test that diff-shaped summaries do NOT switch grammars. Remaining test-side contracts (`event_uid` dedupe, `approval_resolved`, `repo_added`) are all backend-produced.

**Review checklist note for future PRs:** a test may never pin a field in `available_actions`, `LaneStatus`, `Me`, or `WsMessage` that the backend does not serialize — verify against `backend/app/api/*` serializers, not against another test.

## Deploy order

Waves are independently deployable **except W4**, which must go: Alembic migration (`r2f3a4b5c6d7_events_event_uid`, dedupe-then-index) → backend → worker → web. The web dedupe from W0 (upsert by `(thread_id, seq, role)`) must already be live before the W4 web half ships.

1. **W0** (pure web) — deploy alone, any time.
2. **W1, W2, W3** (web + additive backend) — any order; backend halves are backward-compatible (new intent branch, new columns nullable, new endpoints).
3. **W4** — migration → backend → worker → web (see above).
4. **W5a, W5b** — any order after W0; dead-code deletion (Composer/ThreadSidebar/Feed/Viewer/cardTypes/pagination) has no runtime dependency.

No new feature flags. SDK-path redaction (W-H12) touches only the flag-gated runtime.

## Manual evidence checklist (the 6 blocker flows)

Run these against a stacked environment before declaring done:

- [ ] **Stop/Abandon from the UI** — Stop visible on every non-terminal run; Abandon requires two taps; pending approval cards stamp `stopped` and leave the queue (W-B1 + W-H5).
- [ ] **Resume + Edit-&-resend** on an interrupted run — resume banner gated on `resumable`; edit-&-resend prefills the composer with the last user message and respawns the thread (W-B2).
- [ ] **Swarm run reaches spawn** — `agent-rnd` mode from the UI yields `MODE=goal` in `thread_env` and containers actually spawn (W-B3).
- [ ] **Double-Enter mints one run** — hammer Enter on a fresh draft; exactly one POST `/runs` with one `idempotency_key` (W-B4).
- [ ] **Push-notification tap lands on the card** — distinct `tag` per card (no collapsing), deep link `?run=&card=` opens the run and scrolls/focuses the card, params strip after consumption (W-B5).
- [ ] **User message + agent's first step both render** — no swallowed event on the user-message turn (W-B6 DB-authoritative seq); no doubled tool-call card (W-H10).

# Goal: rebuild apps/web as an elite, launch-grade SaaS console

I'm treating collegium as a serious product. The current frontend of `apps/web`
is poor — it does not look like a real, shippable SaaS product. I want an
elite, aesthetically deliberate frontend that could be launched after
maturing. This is a full redesign, not a tweak.

## Product (read the code first, don't guess)
- Path: `apps/web` (React 18 + Vite + TypeScript + Zustand,
  react-resizable-panels, react-markdown, Capacitor for mobile).
- It's an AI-agent fleet console: "a legion of swarm agents, moving as one".
  Operator-facing, data-dense, live-updating. Not a marketing site.
- Screens today: inbox, monitor, approvals, knowledge, ideas,
  patrol (proposals), costs (dashboard), repos, team.
- State: Zustand stores in `src/stores`. API/WS in `src/lib`.
- Styling is currently scattered inline `<style>` blocks + tokens in
  `src/theme/tokens.css`. That's part of the problem — fix it.

## Non-negotiable brand constraints (locked by me — do not change)
- Keep the blue and green. Tokens:
  --blue: #5B82AD; --blue-bright: #8FB4D9;
  --green: #5FA777; --green-bright: #7FCB9A;
- Keep the dark slate base family (--bg-base #202A35, --bg-panel #29343F,
  --bg-module #323F4B) and the hairline/jack neutrals.
- Keep the type stack: IBM Plex Sans (UI), IBM Plex Mono (data/labels),
  Fraunces (display, used with restraint).
- Keep the "patch bay / mission-control" spirit — LEDs, mono labels,
  dot-grid — but elevate it from hobbyist to elite.

## Aesthetic direction (commit fully — do not split the difference)
Pick the **Stripe / Linear hybrid** archetype and execute it completely:
- Stripe-density data tables and dashboards: tight rows, aligned numerics,
  sticky headers, filter chips, server-side-feeling pagination.
- Linear-density chrome: floating card-on-stage, restrained whitespace,
  hairline borders, one accent move per surface, no decorative noise.
- Motion budget: TWO moments only — page/overlay entrance and primary
  state change. Everything else is static or 120–180ms ease. Respect
  `prefers-reduced-motion`.
- Signature element: one memorable, on-brand thing per screen (e.g. the
  live monitor's streaming lane view) — not a generic gradient.

## Hard bans — do NOT ship any of these (anti-slop)
- No Inter / Roboto / Space Grotesk defaults. Use the locked Plex/Fraunces.
- No purple-on-white or blue→purple gradients. No bg-clip-text gradient
  headlines. No glassmorphism orbs, no aurora blobs.
- No generic 3-card feature grids, no emoji feature icons, no "Powered by
  AI" / "10x faster" copy, no fake testimonials, no rocket emojis.
- No spinners inside content areas — use skeleton loaders that match the
  real layout shape.
- No icon-only buttons without `title=""` + visible label on hover/focus.
- No inline `<style>` blocks scattered across components. Move to a real
  token + layer system (CSS variables in `src/theme/`, shared primitives,
  one source of truth). Keep the existing tokens.css as the seed.
- No `useEffect + fetch` without caching; use a consistent data hook
  pattern with optimistic update + rollback where mutations exist.

## Speed and execution mode
- Move fast. Write code in bulk, not one tentative file at a time. Batch
  multiple file writes per turn and keep momentum — don't stop to ask
  trivial questions; make a sensible decision from the locked constraints
  and keep going.
- Do NOT run tests, lint, or the dev server after every file. That kills
  velocity. Write all the files for a screen end-to-end first.
- Only the explicit approval gates below are real stops. Everything else
  is "go".

## Process — work in two passes, do not jump to code
Pass 1 — write `DESIGN.md` at `apps/web/DESIGN.md` (max ~3 paragraphs +
a token table):
  - Color: 4–6 named hexes (the locked blues/greens + neutrals + one
    danger). Define usage rules, not just values.
  - Type: roles for Plex Sans / Plex Mono / Fraunces; size + weight scale.
  - Layout: one-sentence concept per archetype surface + ASCII wireframes
    for the shell, a list screen, a detail/overlay, and a dashboard.
  - Signature: the one element per screen that's memorable.
  - Spacing/radius/shadow/density scale as numeric tokens.
  Stop and wait for my approval before Pass 2. (This is the only stop
  before the end.)

Pass 2 — implement fast, screen by screen, behind the existing Zustand
stores and API layer (don't rewrite data logic unless needed):
  - Start with the app shell (topbar → sidebar or top-nav, decide and
    justify), then one representative list screen (inbox), one live screen
    (monitor), one dashboard (costs), one overlay (OverlayShell).
  - Replace inline `<style>` with the token system as you go.
  - Keep it responsive (it ships via Capacitor to mobile too) and
    accessible (WCAG 2.1 AA contrast on the locked palette — verify, don't
    assume; if a locked pair fails contrast, propose a tint adjustment
    inside the same family and ask me).
  - Write ALL files for a screen before moving on. Don't ping-pong between
    code and verification mid-screen.

## Tests — write at the END, then run once
- After every screen is implemented and all files are written, write unit
  tests for the new/changed frontend logic: Zustand store transitions,
  pure helpers, component rendering for the redesigned screens (use the
  existing vitest + @testing-library/react setup already in package.json).
  - Put tests next to source under `__tests__/` folders, matching the
    existing pattern in `src/__tests__`.
  - Only AFTER all test files are written, run the full suite once:
    `npm test` (which runs `vitest run`) from `apps/web`.
  - If anything fails, fix in bulk and re-run once. Don't get into a
  one-test-at-a-time loop.

## Audit loop — required before declaring done
- Run a visual layout audit on each finished screen: check alignment
  drift (chrome left edges, text anchor line, cross-seam sidebar/main),
  grid complexity, contrast, and density consistency.
- Take screenshots (or render to a canvas) and self-review against the
  DESIGN.md before showing me. If a screen fails the audit, fix it before
  reporting done.
- Show me: before/after for the shell + 4 representative screens, with a
  one-line note on which archetype/density each follows, plus the final
  `npm test` result (pass count / fail count).

## Out of scope this pass
- Backend, worker, infra, fleet-config. Frontend only.
- Don't add new product features. Redesign what exists.

Start by reading `apps/web/src/App.tsx`, `src/theme/tokens.css`, and one
or two feature screens, then produce `DESIGN.md` and stop for my review.

# Goal: rebuild apps/web as an elite, launch-grade SaaS console (Tailwind track)

This is **version 2** of the redesign prompt. Pass 1 already produced
`apps/web/DESIGN.md` under the previous (custom-CSS) prompt. Your first job
is to **update that existing plan in place** to reflect the Tailwind v4
system below — do not throw it away and start over. The archetype, color,
type, layout, signature, and motion decisions in DESIGN.md are good and
stay; only the *styling-system* layer changes.

I'm treating zagent as a serious product. The current frontend of
`apps/web` is poor — it does not look like a real, shippable SaaS product.
I want an elite, aesthetically deliberate frontend that could be launched
after maturing. This is a full redesign, not a tweak.

## Product (read the code first, don't guess)
- Path: `apps/web` (React 18 + Vite + TypeScript + Zustand,
  react-resizable-panels, react-markdown, Capacitor for mobile).
- It's an AI-agent fleet console: "Route the fleet". Operator-facing,
  data-dense, live-updating. Not a marketing site.
- Screens today: inbox, monitor, approvals, knowledge, ideas,
  patrol (proposals), costs (dashboard), repos, team.
- State: Zustand stores in `src/stores`. API/WS in `src/lib`.
- Styling is currently scattered inline `<style>` blocks + tokens in
  `src/theme/tokens.css`. That's part of the problem — replace it with
  the Tailwind system below.

## Styling system — Tailwind v4 (this is the change from v1)
- Install and configure **Tailwind CSS v4** with the Vite plugin
  (`@tailwindcss/vite`). Use the v4 CSS-first config via `@theme` in a
  single `src/theme/index.css` — not a JS config file unless required.
- Map EVERY locked token from DESIGN.md into `@theme` so they become
  utility classes, not just CSS variables:
  - Colors: `--color-bg-base`, `--color-bg-panel`, `--color-bg-module`,
    `--color-jack`, `--color-ink-primary`, `--color-ink-secondary`,
    `--color-ink-faint`, `--color-ink-ghost`, `--color-blue`,
    `--color-blue-bright`, `--color-green`, `--color-green-bright`,
    `--color-danger`, `--color-danger-bright`, `--color-warn`,
    `--color-hairline` — using the exact hexes from DESIGN.md (including
    the two approved tints: `--ink-faint #7C8B92`, `--danger-bright #D96771`).
  - Spacing: `--spacing` scale on the 4pt system (`--s1..--s12` from DESIGN.md).
  - Radius: `--radius-sm/md/lg/pill`.
  - Density: `--row-sm:30px --row-md:36px --row-lg:44px` as utilities
    (e.g. `h-row-md`).
  - Fonts: `--font-ui` (IBM Plex Sans), `--font-mono` (IBM Plex Mono),
    `--font-display` (Fraunces).
  - Shadows, z-index, motion durations as theme tokens.
- Prefer **shadcn/ui** and **21st.dev** primitives as the component base
  (Button, Card, Dialog/Overlay, DataTable, Skeleton, Command palette,
  Chip/Badge, Tooltip). Style them with the locked tokens — do NOT
  accept shadcn's default neutral/zinc palette or default radius. The
  locked blue/green on dark slate wins.
- Keep `font-variant-numeric: tabular-nums` on all numeric/data cells
  (expose a `tabular` utility or a `data` component class).
- No inline `<style>` blocks in components. Component-specific styles
  go in CSS Modules or small `@layer components` rules in
  `src/theme/index.css` only when a utility composition is genuinely
  cleaner — default to utility classes in JSX.
- Keep the dot-grid stage background and LED glow (box-shadow) — these
  are the patch-bay instrumentation; express them as reusable utilities
  or a `@layer base` rule.

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
- Honor the two approved tints: `--ink-faint #7C8B92` (text) with the
  old `#5D6C73` demoted to `--ink-ghost` (decorative only), and
  `--danger-bright #D96771` (text/icons) with `#B23A45` for fills/borders.
- Honor the usage rule: `--blue #5B82AD` is borders/icons/large-glyphs
  only, never body text — `--blue-bright` is the text-safe blue.

## Aesthetic direction (commit fully — do not split the difference)
Keep the **Stripe / Linear hybrid** archetype already in DESIGN.md:
- Stripe-density data tables and dashboards: tight rows (34–38px),
  aligned tabular numerics, sticky headers, filter chips, server-side-
  feeling pagination.
- Linear-density chrome: floating card-on-stage, restrained whitespace,
  hairline borders, one accent move per surface, no decorative noise.
- Motion budget: TWO moments only — page/overlay entrance (220ms) and
  primary state change (180ms). Everything else static or 120–180ms ease.
  Respect `prefers-reduced-motion`.
- Signature element per screen — keep the ones in DESIGN.md (lane river
  for monitor, patch-cable composer for inbox, ledger bars for costs,
  etc.).

## Hard bans — do NOT ship any of these (anti-slop)
- No Inter / Roboto / Space Grotesk defaults. Use the locked Plex/Fraunces.
- No purple-on-white or blue→purple gradients. No bg-clip-text gradient
  headlines. No glassmorphism orbs, no aurora blobs.
- No generic 3-card feature grids, no emoji feature icons, no "Powered by
  AI" / "10x faster" copy, no fake testimonials, no rocket emojis.
- No spinners inside content areas — use skeleton loaders that match the
  real layout shape (use shadcn Skeleton, themed).
- No icon-only buttons without `title=""` + a visible label on hover/focus.
- No inline `<style>` blocks scattered across components (this is now
  enforced by the Tailwind system — don't regress).
- No `useEffect + fetch` without caching; introduce **React Query**
  (`@tanstack/react-query`) for server state with optimistic update +
  rollback where mutations exist. Zustand stays for UI/session state.
- Do NOT accept shadcn/21st.dev default themes (zinc, slate-default
  radius, default shadows). Every primitive must inherit the locked
  tokens from `@theme`.

## Speed and execution mode
- Move fast. Write code in bulk, not one tentative file at a time. Batch
  multiple file writes per turn and keep momentum — don't stop to ask
  trivial questions; make a sensible decision from the locked constraints
  and keep going.
- Do NOT run tests, lint, or the dev server after every file. That kills
  velocity. Write all the files for a screen end-to-end first.
- Only the explicit approval gates below are real stops. Everything else
  is "go".

## Process — update the plan, then implement
Step 1 — **update `apps/web/DESIGN.md` in place** (do not rewrite it):
  - Add a "Styling system" section: Tailwind v4 + `@theme` token mapping,
    shadcn/21st.dev primitive inventory, React Query for server state.
  - Add a **primitive inventory** list: Button, Chip, Card, DataTable,
    Skeleton, PageHead, Rail, Overlay, Tooltip, Command — each one line
    on which locked tokens it inherits.
  - Confirm the existing color/type/layout/signature/motion sections still
    hold (they do). Adjust only where the Tailwind system changes
    expression (e.g. note that spacing/radius now come from theme
    utilities, not raw CSS vars).
  - Stop and wait for my approval of the updated DESIGN.md before Step 2.
  (This is the only stop before the end.)

Step 2 — implement fast, screen by screen, behind the existing Zustand
stores and API layer (introduce React Query for server state; don't
rewrite business logic unless needed):
  - Order: install Tailwind v4 + shadcn + React Query → `src/theme/index.css`
    with `@theme` → shared primitives → app shell (left rail from DESIGN.md)
    → inbox (list) → monitor (live) → costs (dashboard) → OverlayShell.
  - Replace every inline `<style>` block with the Tailwind system as you
    go. Delete `tokens.css` content once it's fully migrated into `@theme`
    (or keep it as a thin `@layer base` import if cleaner).
  - Keep it responsive (it ships via Capacitor to mobile too — rail
    collapses to icons <1100px, top bar + overflow <700px per DESIGN.md)
    and accessible (WCAG 2.1 AA — the locked palette already passes with
    the two approved tints; re-verify any new text/bg pair you introduce).
  - Write ALL files for a screen before moving on. Don't ping-pong between
    code and verification mid-screen.

## Tests — write at the END, then run once
- After every screen is implemented and all files are written, write unit
  tests for the new/changed frontend logic: Zustand store transitions,
  React Query hooks, pure helpers, component rendering for the redesigned
  screens (use the existing vitest + @testing-library/react setup already
  in package.json).
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
  updated DESIGN.md before showing me. If a screen fails the audit, fix
  it before reporting done.
- Show me: before/after for the shell + 4 representative screens, with a
  one-line note on which archetype/density each follows, plus the final
  `npm test` result (pass count / fail count).

## Out of scope this pass
- Backend, worker, infra, fleet-config. Frontend only.
- Don't add new product features. Redesign what exists.

Start by reading `apps/web/DESIGN.md`, `apps/web/src/App.tsx`,
`src/theme/tokens.css`, and one feature screen, then update DESIGN.md in
place with the Tailwind v4 + shadcn + React Query layer and stop for my
review.

# collegium console — design contract

**Archetype: Stripe x Linear, committed.** Every surface is one of four archetypes — *list*, *live*, *dashboard*, *overlay* — and each commits fully: Stripe owns the data (tight rows, right-aligned numerics, sticky headers, filter chips, pagination that feels server-side), Linear owns the chrome (a slim rail around a card-on-stage main area, hairline borders, one accent move per surface, zero decorative noise). The patch-bay soul survives as *instrumentation*: LEDs that mean something, mono micro-labels in caps, the dot-grid stage. Fraunces appears at most once per screen, and never below 22px. If a surface can't justify an ornament, the ornament is deleted.

**Density is the brand.** This is an operator console, not a brochure. Rows are 34–38px, not 56px. Numbers are tabular, mono, and right-aligned. Section labels are 10.5px mono caps with 0.12em tracking. Whitespace is spent on *separation of regions*, never inside data regions. Every screen gets exactly one signature element — the thing you'd recognize in a screenshot with the logo cropped out — listed per screen below. Motion has a budget of two moments: overlay/page entrance (180–220ms, ease-out, opacity+translate only) and primary state change (a run card's stage lamp, an approval resolving). Everything else is static or a 120–180ms color/border ease; `prefers-reduced-motion` collapses all of it.

**Navigation: left rail, because the fleet metaphor is spatial.** Nine destinations plus an admin-gated tenth exceed a topbar's honest capacity, and grouping (OPERATE / INTELLIGENCE / FLEET) teaches the product's mental model. The rail also earns a status footer — socket LED, signed-in operator, sign-out — turning chrome into instrumentation. Below 1100px it collapses to icons with tooltips; below 700px it becomes a top bar with an overflow menu (Capacitor path). Known future work, out of scope this pass: URL routing/deep-links (push notifications currently can't target a screen), which the rail's information architecture is designed to survive unchanged.

## Styling system

- **Tailwind CSS v4**, CSS-first: one `src/theme/index.css` with `@import "tailwindcss"` and a single `@theme` block — no JS config. Every token below maps into `@theme` and is consumed as a utility (`--color-green-bright` → `text-green-bright`, `--s4` → `p-s4`, `--radius-md` → `rounded-md`, `--row-md` → `h-row-md`, `--font-display` → `font-display`, shadows/z/durations likewise). Raw CSS vars appear only inside `@layer base` rules.
- **shadcn/ui (+ 21st.dev) primitives** are the component base — restyled so every semantic value resolves to the locked tokens below. Default zinc palette, default radius, and default shadows are rejected at the theme layer, not patched per instance.
- **React Query** (`@tanstack/react-query`) owns server state: caching, dedupe, `placeholderData`, and optimistic mutations with rollback. Zustand keeps UI/session/run-websocket state.
- All numeric/data cells carry `font-variant-numeric: tabular-nums` via a `tabular` utility.
- Patch-bay instrumentation (dot-grid stage, LED glow) is expressed once in `@layer base` / as utilities — never as per-component CSS. No inline `<style>` blocks anywhere; component-specific CSS only as small `@layer components` rules in `index.css` when a utility composition is genuinely less clean.

### Primitive inventory

| Primitive | Inherits |
|---|---|
| Button | variants: primary = `--green` fill + `#0C1410` ink · ghost = `--hairline` border · danger = `--danger-bright` text on `--danger` border; `--r-sm`, 120ms ease |
| Chip / FilterChips | `--r-pill`, mono 11.5px; active = `--green` border + `--green-bright` text on 10% green |
| Card | `--bg-panel`, `--hairline`, `--r-lg`, `--sh-card` |
| DataTable | sticky header (mono caps, `--ink-faint`), `--row-md` rows, right-aligned tabular numerics, skeleton rows, pagination chrome |
| Skeleton | `--bg-module` sweep; geometry matches the final layout |
| PageHead | Fraunces 26px + mono-caps sub-label + actions slot — once per screen |
| Rail | `--rail-w` 224px (56px collapsed), `--bg-panel`, hairline edge, mono items, `--green-bright` active |
| Overlay (Dialog) | `--bg-panel` 80% panel, `--jack` backdrop blur, `--sh-overlay`, enter-rise 220ms, focus trap + ESC |
| Tooltip | `--bg-module`, mono 11px — satisfies the icon-only-button visible-label rule |
| Command | `--jack` input well, `--bg-panel` list, `--blue-bright` active row (ticket/repo pickers) |

## v2.4 — Neon spring-green accent + deep-black chrome + prose markdown

v2.3's warm charcoal + muted sage read as "vintage" / "color damaged in sunshine" against modern dev-tool peers. Direction now:

1. **Deep-black chrome** (one step above pure black, never `#000000` — keeps panel/module/jack layering headroom): `#0F110F` stage, `#161816` panels, `#1E211E` modules, `#0A0C0A` terminal/code (`--jack`). Neutral with a whisper of green, never blue (v2.3's blue-grey-mud warning stands).
2. **Neon spring-green ramp** (devswarm `#00ff96` family, applied by stroke weight so it shines without halation):
   - `--green-bright: #00FF96` — the neon core, thin strokes only: accent text, links, LEDs, focus rings, active nav, glow shadows (~13.6:1 on base, AA-clean).
   - `--green: #00D982` — same hue, slightly deepened for fills with dark ink: primary button, success badges (~9.7:1 vs `#06130B` ink).
   - Soft tints auto-derive via the existing `color-mix` tokens.
3. **Restrained shine**: new `--shadow-glow: 0 0 14px color-mix(in srgb, var(--green-bright) 32%, transparent)`, applied to the primary button + hover only. Glow budget: **two elements per screen max** (primary button + live LEDs). More = gaming-RGB, less = expensive. No gradients — the existing glow exception in the LED rule is the only other allowed glow.
4. **Markdown is prose, not titled cards.** Headings drop Fraunces (which is reserved for screen titles per the Type section) and use the UI face at 16/15/13.5px; sections separate by rhythm, not by looking like boxed sections. Tables lose vertical borders, zebra striping, and the boxed header fill — horizontal hairlines only, header stays mono-caps `--ink-faint` with a bottom rule. Inline code blends (`bg-module` + `green-bright` text) instead of a dark chip. Edge margins normalized so bubbles have clean top/bottom.
5. **Code-surface split: syntax speaks VS Code, chrome speaks Collegium.** `vscDarkPlus` token colors stay as-is (devs' muscle memory; they were tuned on `#1e1e1e`, so on deeper jack they read slightly brighter — acceptable). The block surface becomes `--jack` + hairline frame, with a slim VS Code-style header: file-type icon (new `FileIcon` set — TS blue, JS yellow, JSON orange, Python blue/yellow, shell green…) + filename or language label in mono micro `--ink-faint`. The terminal frame loses its macOS traffic-light dots (off-palette ornament; "if a surface can't justify an ornament, the ornament is deleted") and gains the same slim header.
6. **Mermaid diagrams render for real.** A ` ```mermaid ` fence renders as a themed SVG (lazy-loaded via `React.lazy` + `Suspense` so the ~300KB chunk never touches startup or non-diagram sessions; `securityLevel: "strict"` for untrusted agent content; graceful fallback to a code block + "diagram failed to parse" note on parse error). Three-stage UX: skeleton-sweep shimmer while the chunk loads → partial source as code while streaming → themed SVG when complete.

## v2.5 — Public landing: brochure brightness on console geometry

The console gains a public front door: `/` is now the landing page (the console moved under `/app`; the landing boots the session only to choose its primary CTA — *sign in* vs *open console* — and never gates or redirects). It is a fifth archetype, **brochure**, and it is allowed to break the density rule: section padding grows to `--s16`/`--s20` (new tokens), Fraunces scales to a `clamp(44px, 7vw, 78px)` headline, negative space is spent freely. What does *not* change: the geometry stays patch-bay (dot-grid stage, hairlines, jack wells, mono micro labels, LEDs), and every color resolves to the locked v2.4 tokens.

1. **Brochure glow budget** (supersedes "two elements per screen" for this surface only): the hero aura (two *static* radial washes of `--green-bright`/`--green` at 14%/10% alpha), the neon headline word (`text-shadow` on thin display strokes — the only place text may glow), the primary CTA's `--shadow-glow`, and the demo console's LEDs + river. Static brightness over motion: the aura never animates.
2. **Signature element: the self-playing session console.** A scripted typed feed (prompt → plan → three lanes → PR with evidence) plays in a jack well with the slim VS Code-style header, live LED, and a river track. One interval drives a char budget across the script; `prefers-reduced-motion` shows the full frame instantly. It demonstrates the product's grammar without a screenshot or a video.
3. **Blueprint jack-strip.** The six orchestrator blueprints (ask / plan / debug / development / swarm / goal) render as 1U bays on the bus — LED, mono name, ghost index, one honest contract line each. Feature modules reuse the same `lp-module` bay with a jack icon square.
4. **i18n:** all section copy lives under `landing.*`; the demo script is demo data (like a screenshot) and stays inline.

## Color

| Token | Hex | Usage rule | Contrast (on its bg) |
|---|---|---|---|
| `--bg-base` | `#0F110F` | App stage, dot-grid ground. Nothing sits directly on it except panels. | — |
| `--bg-panel` | `#161816` | Cards, rail, overlay panels. | — |
| `--bg-module` | `#1E211E` | Inset modules, hover fills, active chip ground, code headers. | — |
| `--jack` | `#0A0C0A` | Deep inset wells (textareas, code blocks, stream gutters, diagram bodies). Never a card. | — |
| `--ink-primary` | `#E8ECE8` | Titles, data, anything the operator must read. | 14.2:1 on base ✅ |
| `--ink-secondary` | `#9AA39A` | Descriptions, metadata, secondary labels. | 6.8:1 on base ✅ |
| `--ink-faint` | `#6F786F` | Timestamps, hints, placeholders, mono micro labels. | 4.6:1 on base ✅ |
| `--green-bright` | `#00FF96` | **v2.4 neon core:** accent text, links, LEDs, focus rings, active nav, glow. Thin strokes only. | 13.6:1 on base ✅ |
| `--green` | `#00D982` | Fills with dark ink — primary button, success badges. | 9.7:1 vs `#06130B` ✅ |
| `--danger` | `#B23A45` | Fills and 1px borders only (never text). | 2.2:1 as text ❌ |
| `--danger-bright` | `#D96771` | Danger *text/icons* (deny buttons, error labels). | 4.6:1 on panel ✅ |
| `--warn` | `#D9B36C` | Warn tone (stale lanes, watchdog). | 6.1:1 on panel ✅ |
| `--hairline` | `#282C29` | All borders. 1px, no heavier rule exists. | — |

**Rules (v2.4 — neon spring-green ramp):** green-bright = the neon accent on thin strokes (action, healthy, information, navigation — distinguished by weight/placement, not hue); green = the deeper fill for large areas. Warn = attention, danger = destructive. Everything else stays neutral ink. No gradients anywhere except the LED glow and the new `--shadow-glow` (both box-shadow).

## v2.3 — Neutral charcoal + unified terminal monitor (supersedes v2.2)

v2.2 (light chrome + green rail) was rejected in review: green-on-offwhite read as "2000s website". Direction now:

1. **Neutral charcoal chrome** (Cursor-agent class): `#1B1B1A` stage, `#232322` panels, `#2C2C2A` modules, `#121212` terminal (`--jack`). No blue tint anywhere in the greys — v1's failure was blue-grey mud, not darkness itself. Mono-green accent unchanged from v2.1.
2. **The monitor is one terminal.** The 50/50 split panes are gone: a single full-bleed charcoal column holds the swarm strip (horizontal lane tiles), the lead summary bubble, the full-height interleaved event stream, the action card, and the chat composer docked at the bottom of the same terminal. The traces get maximum space; chat lives inside the trace surface, Cursor-agent style. `ChatPane` is deleted — merged into `MonitorScreen`.

## Type

| Role | Face | Usage |
|---|---|---|
| Display | Fraunces 600 | Screen titles only, 22–26px. Once per screen, sentence case. |
| UI | IBM Plex Sans 400/600 | Body 13.5–15px, card titles 14–15px/600. |
| Data/labels | IBM Plex Mono 400/600 | Numerics (with `font-variant-numeric: tabular-nums`), section labels (10.5px caps, 0.12em tracking), timestamps, IDs, chips, tags. |

Scale (px): 10.5 / 11 / 12 / 13.5 / 15 / 17 / 22 / 26. Weights: 400 and 600 only. Line-height: 1.45 body, 1.2 display, 1.0 for data cells.

## Layout archetypes

**Shell** — 224px rail, hairline-separated; main is the dot-grid stage with one max-width-1280 canvas:

```
┌────────────┬──────────────────────────────────────────┐
│ ● collegium   │  screen title (Fraunces)        actions  │
│            │  ──────────────────────────────────────  │
│ OPERATE    │                                          │
│  ▸ inbox   │            canvas (stage)                │
│   monitor  │                                          │
│   approvals│                                          │
│ INTEL      │                                          │
│   knowledge│                                          │
│   ideas    │                                          │
│   patrol   │                                          │
│ FLEET      │                                          │
│   costs    │                                          │
│   repos    │                                          │
│   team     │                                          │
│            │                                          │
│ ● live     │                                          │
│ operator ⎋ │                                          │
└────────────┴──────────────────────────────────────────┘
```

**List (inbox, approvals, patrol, repos, team)** — PageHead + filter chips, then either a 400px rail + list column or full-width table:

```
 title (Fraunces)          [primary action]
 sub-label (mono caps)
 ┌ chips: [all] [running] [queued] ────────── ─┐
 │ ┌────────────┐ ┌──────────────────────────┐ │
 │ │ rail       │ │ row 34px ▏lamp title  $ │ │
 │ │ (composer/ │ │ row      ▏lamp title  $ │ │
 │ │  filters)  │ │ …        ▏              │ │
 │ └────────────┘ └──────────────────────────┘ │
 └─────────────────────────────────────────────┘
```

**Live (monitor)** — full-bleed console: header strip, resizable split, no page scroll:

```
┌ ← inbox ▏run title ▏pipeline ▸▸▸ ▏plan pr ● live ┐
├──────────────────────┬───────────────────────────┤
│ swarm lane grid      │ chat with the Lead        │
│ (signature element)  │                           │
├──────────────────────┤                           │
│ event stream (gutter)│                           │
└──────────────────────┴───────────────────────────┘
```

**Dashboard (costs)** — stat strip then ledger table, bars as table cells not a chart widget:

```
 title                      [7d] [30d] [90d]
 ┌─────────┬─────────┬─────────┬─────────┐
 │ $ total │ runs    │ $/run   │ top repo│  ← mono tabular, 22px
 └─────────┴─────────┴─────────┴─────────┘
 ┌ ledger ───────────────────────────────┐
 │ repo        runs    cost     ▓▓▓▓░ 42%│  ← bar lives in the row
 │ mode        runs    cost     ▓▓░    9%│
 └───────────────────────────────────────┘
```

**Overlay (lane/plan/pr)** — 80% dialog over a *live* monitor; entrance is motion moment #1:

```
        ┌─ overlay ─────────────────────────── ✕ ─┐
        │ title (mono caps)                        │
        │ ─────────────────────────────────────── │
        │   content (table / stream / evidence)    │
        └──────────────────────────────────────────┘
   (monitor keeps streaming underneath — never unmounted)
```

## Signature element per screen

| Screen | Signature |
|---|---|
| monitor | **Lane river** — swarm lanes as horizontal streaming channels with activity pulses flowing left→right; recognizable at a glance |
| inbox | **Patch-cable composer** — the mode chip row reads as a jack strip; routing a run "plugs in" (moment #2) |
| costs | **Ledger bars** — proportional bars rendered *inside* table rows, aligned to the cost column |
| approvals | **Decision cards** — tool-call JSON in a jack well with allow/deny as the only two accent moves |
| knowledge | Scope LEDs — corpus rows keyed by tri-color scope lamps |
| ideas | Counsel thread — Lead synthesis card with a Fraunces pull-quote first line |
| patrol | Impact/confidence twin tags on ranked proposal rows |
| repos | Rack rows — each repo a 1U module with HEAD hash in mono |
| team | Operator table with setup-code reveal |
| login | Wordmark + LED on the bare dot-grid stage; the only screen with negative space |
| landing | **Self-playing session console** — a scripted typed feed (prompt → plan → lanes → PR) in a jack well under the hero aura |

## Tokens (numeric)

| Group | Values |
|---|---|
| Spacing (4pt) | `--s1:4  --s2:8  --s3:12  --s4:16  --s5:20  --s6:24  --s8:32  --s10:40  --s12:48` · brochure-only: `--s16:64  --s20:80` |
| Radius | `--r-sm:6  --r-md:8  --r-lg:12  --r-pill:999` |
| Shadow | `--sh-card: 0 1px 2px rgba(12,16,19,.35)` · `--sh-pop: 0 12px 32px rgba(12,16,19,.45)` · `--sh-overlay: 0 24px 60px rgba(12,16,19,.6)` |
| Z-index | `--z-base:0  --z-sticky:10  --z-rail:20  --z-overlay:100  --z-toast:200` |
| Density | row: `--row-sm:30 --row-md:36 --row-lg:44` · canvas max-width `1280px` · rail `224px` / collapsed `56px` |
| Motion | `--dur-fast:120ms --dur-med:180ms --dur-enter:220ms` · `ease-out cubic-bezier(.2,.7,.3,1)` · entrance = opacity+8px translate only |
| Grid | dot-grid `26px`, 1px dots in `--hairline` at 40% on `--bg-base` only |

All values above are authored in the `@theme` block and consumed as Tailwind utilities (`p-s4`, `gap-s2`, `rounded-md`, `h-row-md`, `shadow-pop`, `z-overlay`, `duration-enter`) — raw vars only inside `@layer base`.

## Motion budget (exhaustive)

1. **Entrance** — overlay + page mount: 220ms ease-out, opacity + 8px rise.
2. **Primary state change** — stage lamp transitions, approval resolving, route-it success: 180ms color/glow ease.
3. Everything else: 120–180ms border/color/background ease, or nothing. No spring physics, no parallax, no layout animation. `prefers-reduced-motion` → all durations 0.01ms.

## Accessibility commitments

- AA 4.5:1 for all text per the contrast column above; the two approved tints (`--ink-faint #7C8B92`, `--danger-bright #D96771`) exist solely to make the locked family AA-legal. Any *new* text/bg pair introduced in implementation must be re-verified against 4.5:1.
- Focus: 2px `--blue-bright` ring, offset 2px, never removed.
- Every icon-only control carries `title` + a visible label on hover/focus; LEDs always paired with a text state.
- Skeletons match final layout geometry; no spinners in content areas.

import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  BookOpenIcon,
  CircleDollarSignIcon,
  CloudIcon,
  LightbulbIcon,
  RadarIcon,
  ShieldCheckIcon,
  UsersIcon,
  WavesIcon,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { FileIcon } from "@/components/ui/file-icon";
import { CONSOLE_HOME } from "@/lib/routes";
import { useSession } from "../../stores/session";
import { useAppTranslation } from "@/i18n";

/* Landing archetype (DESIGN.md v2.5): the public brochure at `/`. Same
 * tokens as the console, brighter budget — hero aura, neon headline word,
 * demo-console LEDs. Geometry stays patch-bay; scale goes editorial. */

type DemoLine = { kind: "user" | "lead" | "lane" | "ok"; label: string; text: string };

/** Scripted session for the hero console — demo data, not translated (like
 *  a screenshot). Shows the product's grammar: prompt → plan → lanes → PR. */
const SCRIPT: DemoLine[] = [
  { kind: "user", label: "you", text: "cache /trending — p99 is embarrassing" },
  { kind: "lead", label: "lead", text: "plan: redis in front of the query, a stampede lock, metrics before and after. three lanes." },
  { kind: "lane", label: "scout", text: "traced the handler — N+1 hiding in hydrate()" },
  { kind: "lane", label: "forge", text: "patch ready: cache-aside + singleflight · +142 −38" },
  { kind: "lane", label: "lens", text: "tests green · p99 480ms → 41ms on the bench" },
  { kind: "ok", label: "rack", text: "PR #219 opened with evidence — awaiting your merge." },
];

const CHAR_MS = 22;
const LINE_PAUSE_TICKS = 9;
const REST_TICKS = 170; // hold the finished frame ~3.7s before looping

function useReducedMotion() {
  return useMemo(
    () =>
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    [],
  );
}

/** Self-playing typed feed. A single tick budget walks the script char by
 *  char; line pauses and the end-of-loop rest are just dead ticks, so the
 *  whole animation is one interval and a pure function of tick. */
function LiveDemo() {
  const reduced = useReducedMotion();
  const total = useMemo(
    () => SCRIPT.reduce((sum, l) => sum + l.text.length + LINE_PAUSE_TICKS, REST_TICKS),
    [],
  );
  const [tick, setTick] = useState(reduced ? total : 0);

  useEffect(() => {
    if (reduced) return;
    const id = window.setInterval(() => setTick((t) => (t >= total ? 0 : t + 1)), CHAR_MS);
    return () => window.clearInterval(id);
  }, [reduced, total]);

  let budget = tick;
  const lines = SCRIPT.map((l) => {
    const shown = Math.max(0, Math.min(l.text.length, budget));
    budget -= l.text.length + LINE_PAUSE_TICKS;
    return { ...l, shown };
  });
  const typing = tick < total - REST_TICKS;
  let caretIdx = -1;
  if (typing) lines.forEach((l, i) => { if (l.shown > 0) caretIdx = i; });

  return (
    <div
      data-testid="landing-demo"
      className="overflow-hidden rounded-lg border border-hairline bg-jack shadow-overlay"
    >
      <div className="flex items-center gap-s2 border-b border-hairline bg-bg-module px-s3 py-s1.5">
        <FileIcon kind="bash" />
        <span className="truncate font-mono text-[10.5px] uppercase tracking-[0.12em] text-ink-faint">
          session — cache-the-trending-endpoint
        </span>
        <span className="ml-auto flex items-center gap-s2">
          <span className="led" aria-hidden="true" />
          <span className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-green-bright">
            live
          </span>
        </span>
      </div>
      <div className="h-[268px] px-s4 py-s3 font-mono text-[12.5px] leading-[1.55] max-[700px]:h-[332px]">
        {lines.map((l, i) =>
          l.shown === 0 ? null : (
            <div key={l.label} className="grid grid-cols-[52px_1fr] gap-s3 py-[3px]">
              <span
                className={
                  l.kind === "lead" || l.kind === "ok"
                    ? "text-micro pt-[3px] text-green-bright"
                    : "text-micro pt-[3px] text-ink-faint"
                }
              >
                {l.label}
              </span>
              <span className={l.kind === "user" ? "text-ink-secondary" : "text-ink-primary"}>
                {l.text.slice(0, l.shown)}
                {i === caretIdx && <span className="lp-caret" aria-hidden="true" />}
              </span>
            </div>
          ),
        )}
      </div>
      <div className="px-s4 pb-s3">
        <div className="river-track text-green-bright" aria-hidden="true">
          <span className="river-pulse" />
        </div>
      </div>
    </div>
  );
}

const BLUEPRINTS = [
  { name: "ask", copyKey: "landing.bpAsk" },
  { name: "plan", copyKey: "landing.bpPlan" },
  { name: "debug", copyKey: "landing.bpDebug" },
  { name: "development", copyKey: "landing.bpDevelopment" },
  { name: "swarm", copyKey: "landing.bpSwarm" },
  { name: "goal", copyKey: "landing.bpGoal" },
] as const;

const FEATURES: { icon: LucideIcon; name: string; copyKey: string }[] = [
  { icon: WavesIcon, name: "swarm agents", copyKey: "landing.ftSwarm" },
  { icon: LightbulbIcon, name: "ideas", copyKey: "landing.ftIdeas" },
  { icon: UsersIcon, name: "multiplayer", copyKey: "landing.ftTeam" },
  { icon: CloudIcon, name: "your cloud", copyKey: "landing.ftCloud" },
  { icon: ShieldCheckIcon, name: "approvals", copyKey: "landing.ftApprovals" },
  { icon: RadarIcon, name: "patrol", copyKey: "landing.ftPatrol" },
  { icon: BookOpenIcon, name: "knowledge", copyKey: "landing.ftKnowledge" },
  { icon: CircleDollarSignIcon, name: "cost ledger", copyKey: "landing.ftCosts" },
];

function Eyebrow({ children }: { children: string }) {
  return (
    <div className="mb-s4 flex items-center gap-s2">
      <span className="led" aria-hidden="true" />
      <span className="text-micro text-green-bright">{children}</span>
    </div>
  );
}

/** Public landing page. Boots the session (via LandingRoute) only to pick
 *  the primary CTA — signed-in operators get "open console", everyone else
 *  gets "sign in". Never gates, never redirects. */
export function LandingScreen() {
  const me = useSession((s) => s.me);
  const { t } = useAppTranslation();
  const primary = me
    ? { to: CONSOLE_HOME, label: t("landing.ctaConsole") }
    : { to: "/login", label: t("landing.ctaSignin") };

  const stats: [string, string][] = [
    ["6", t("landing.statBlueprints")],
    ["100%", t("landing.statSelfHosted")],
    ["0", t("landing.statBlackBoxes")],
    ["1", t("landing.statConsole")],
  ];

  return (
    <div className="lp-page" data-testid="landing">
      {/* topbar */}
      <header className="z-sticky sticky top-0 border-b border-hairline bg-bg-base/85 backdrop-blur">
        <div className="mx-auto flex h-[56px] max-w-[1120px] items-center gap-s2 px-s6">
          <span className="font-display text-[19px] font-semibold leading-none text-ok-bright" aria-hidden="true">
            ⌁
          </span>
          <span className="font-mono text-[13.5px] font-semibold tracking-[0.03em]">collegium</span>
          <span className="led" aria-hidden="true" />
          <span className="text-micro ml-s1 text-ink-ghost max-[700px]:hidden">v2.5</span>
          <nav className="ml-auto flex items-center gap-s3" aria-label="landing">
            <a
              href="#blueprints"
              className="rounded-md px-s3 py-s2 font-mono text-[12px] text-ink-secondary transition-colors duration-fast hover:text-ink-primary max-[700px]:hidden"
            >
              {t("landing.navTour")}
            </a>
            <Button render={<Link to={primary.to} data-testid="landing-primary-cta" />} nativeButton={false} size="sm">
              {primary.label}
            </Button>
          </nav>
        </div>
      </header>

      {/* hero */}
      <section className="lp-aura" data-testid="landing-hero">
        <div className="mx-auto max-w-[1120px] px-s6 pb-s16 pt-[88px] max-[700px]:pt-[56px]">
          <Eyebrow>{t("landing.eyebrow")}</Eyebrow>
          <h1 className="lp-h1">
            {t("landing.h1a")} <em className="lp-neon">{t("landing.h1b")}</em>
          </h1>
          <p className="mt-s6 max-w-[620px] text-[16px] leading-[1.6] text-ink-secondary">
            {t("landing.sub")}
          </p>
          <div className="mt-s8 flex flex-wrap items-center gap-s3">
            <Button render={<Link to={primary.to} />} nativeButton={false} size="lg" className="gap-s2 font-mono text-[13px]">
              <span className="size-2 rounded-full bg-current" aria-hidden="true" />
              {primary.label}
            </Button>
            <Button render={<a href="#blueprints" />} nativeButton={false} variant="outline" size="lg" className="font-mono text-[13px]">
              {t("landing.ctaTour")}
            </Button>
          </div>
          <div className="mt-s12">
            <LiveDemo />
          </div>
        </div>
      </section>

      {/* blueprint jack-strip */}
      <section id="blueprints" className="border-t border-hairline" data-testid="landing-blueprints">
        <div className="mx-auto max-w-[1120px] px-s6 py-s16">
          <Eyebrow>{t("landing.busEyebrow")}</Eyebrow>
          <h2 className="lp-h2">{t("landing.busH2")}</h2>
          <p className="mt-s4 max-w-[560px] text-[14px] leading-[1.6] text-ink-secondary">
            {t("landing.busSub")}
          </p>
          <div className="mt-s10 grid gap-s3 sm:grid-cols-2 lg:grid-cols-3">
            {BLUEPRINTS.map((b, i) => (
              <div key={b.name} className="lp-module p-s4">
                <div className="mb-s3 flex items-center gap-s2">
                  <span className="led" aria-hidden="true" />
                  <span className="font-mono text-[13px] font-semibold text-ink-primary">{b.name}</span>
                  <span className="text-micro ml-auto text-ink-ghost">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                </div>
                <p className="text-[13px] leading-[1.55] text-ink-secondary">{t(b.copyKey)}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* operator features */}
      <section className="border-t border-hairline">
        <div className="mx-auto max-w-[1120px] px-s6 py-s16">
          <Eyebrow>{t("landing.opsEyebrow")}</Eyebrow>
          <h2 className="lp-h2">{t("landing.opsH2")}</h2>
          <p className="mt-s4 max-w-[560px] text-[14px] leading-[1.6] text-ink-secondary">
            {t("landing.opsSub")}
          </p>
          <div className="mt-s10 grid gap-s3 sm:grid-cols-2 lg:grid-cols-4">
            {FEATURES.map((f) => (
              <div key={f.name} className="lp-module p-s4">
                <div className="mb-s3 flex size-8 items-center justify-center rounded-sm border border-hairline bg-jack">
                  <f.icon className="size-4 text-green-bright" aria-hidden="true" />
                </div>
                <div className="mb-s1 font-mono text-[13px] font-semibold text-ink-primary">{f.name}</div>
                <p className="text-[13px] leading-[1.55] text-ink-secondary">{t(f.copyKey)}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* stats strip */}
      <section className="border-t border-hairline">
        <div className="mx-auto grid max-w-[1120px] grid-cols-2 gap-s6 px-s6 py-s12 md:grid-cols-4">
          {stats.map(([value, label]) => (
            <div key={label} className="flex flex-col gap-s1">
              <span className="tabular font-mono text-[26px] font-semibold leading-none text-green-bright">
                {value}
              </span>
              <span className="text-micro text-ink-faint">{label}</span>
            </div>
          ))}
        </div>
      </section>

      {/* final CTA */}
      <section className="border-t border-hairline">
        <div className="mx-auto max-w-[1120px] px-s6 py-s16">
          <div className="lp-aura flex flex-col items-center rounded-lg border border-hairline bg-bg-panel px-s6 py-s16 text-center">
            <h2 className="lp-h2">{t("landing.finalH2")}</h2>
            <p className="mt-s3 text-[14px] text-ink-secondary">{t("landing.finalSub")}</p>
            <Button render={<Link to={primary.to} />} nativeButton={false} size="lg" className="mt-s8 gap-s2 font-mono text-[13px]">
              <span className="size-2 rounded-full bg-current" aria-hidden="true" />
              {primary.label}
            </Button>
          </div>
        </div>
      </section>

      {/* footer */}
      <footer className="border-t border-hairline">
        <div className="mx-auto flex h-[56px] max-w-[1120px] items-center justify-between px-s6">
          <span className="font-mono text-[11px] tracking-[0.04em] text-ink-faint">
            {t("login.footerOrg")}
          </span>
          <span className="text-micro text-ink-ghost">{t("landing.footerTag")}</span>
        </div>
      </footer>
    </div>
  );
}

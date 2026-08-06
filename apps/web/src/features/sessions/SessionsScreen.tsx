import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { FilterChips } from "@/components/ui/filter-chips";
import { Input } from "@/components/ui/input";
import { MentionTextarea } from "@/components/MentionTextarea";
import { PageHead } from "@/components/ui/page-head";
import { cn } from "@/lib/utils";
import { ActionCard } from "../../components/ActionCard";
import { ApprovalQueue } from "../../components/ApprovalQueue";
import { EventStream } from "../../components/EventStream";
import { ThreadChips } from "../../components/ThreadChips";
import { PipelineBar } from "../../components/PipelineBar";
import { SessionResume } from "../../components/SessionResume";
import { SessionTabs } from "../../components/SessionTabs";
import { agentWorking } from "../../lib/runMachine";
import { api } from "../../lib/api";
import { qk } from "../../lib/queryKeys";
import { useRuns } from "../../stores/run";
import { useSession } from "../../stores/session";
import { useUi } from "../../stores/ui";
import { SwarmView } from "../swarm/SwarmView";
import { ThreadOverlay } from "./ThreadOverlay";
import { PlanOverlay } from "./PlanOverlay";
import { PROverlay } from "./PROverlay";

/** The one operating screen. With no run open it is a clean message-sending
 *  screen — history lives behind the tab strip's history button, and the
 *  first message sent here becomes a new session. Opening a run swaps the
 *  same column to the live session — swarm strip, glass-box trace, action
 *  card, composer. Approvals surface inline on the action card, so a
 *  decision never costs a navigation. */
const MODE_OPTIONS = [
  { value: "ask", label: "ask" },
  { value: "plan", label: "plan" },
  { value: "development", label: "develop" },
  { value: "debug", label: "debug" },
  { value: "agent-rnd", label: "swarm" },
  { value: "goal", label: "goal" },
] as const;

type Mode = (typeof MODE_OPTIONS)[number]["value"];

/** One suggestion per mode — the grid doubles as a tour of what the fleet can
 *  do. Clicking loads the composer (mode included) but never sends: the
 *  example is a starting point to edit, not a fire-and-forget. */
const SUGGESTIONS: { mode: Mode; label: string; prompt: string }[] = [
  { mode: "ask", label: "ask", prompt: "how does the approval flow work end to end?" },
  { mode: "plan", label: "plan", prompt: "plan adding rate limiting to the gateway" },
  { mode: "development", label: "develop", prompt: "fix the flaky login redirect and open a PR" },
  { mode: "debug", label: "debug", prompt: "why did the last patrol run fail?" },
  { mode: "agent-rnd", label: "swarm", prompt: "spawn 4 explorers to map auth across the fleet" },
  { mode: "goal", label: "goal", prompt: "ship the team usage-stats page: plan it, build it, open the PR" },
];

export function SessionsScreen() {
  const {
    runs,
    loadRuns,
    openRun,
    closeRun,
    refreshLanes,
    createRun,
    current,
    threads,
    events,
    deltas,
    socketConnected,
    sendIntent,
  } = useRuns();
  const { overlays, pushOverlay, closedTabs, closeTab, reopenTab } = useUi();
  const [task, setTask] = useState("");
  const [mode, setMode] = useState<Mode>("ask");
  // Mode to switch to on the next send while a run is open. Null = no switch
  // pending; the chips follow current.mode until the user picks a new one.
  const [pendingMode, setPendingMode] = useState<Mode | null>(null);
  const [fanout, setFanout] = useState<number | "">("");
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  // Opening (or switching) a run resets the pending switch — the chips
  // follow the run's own mode until the user explicitly picks a new one.
  useEffect(() => {
    setPendingMode(null);
  }, [current?.id]);

  // Thread heartbeats age against wall time; re-render occasionally so stale
  // threads surface without waiting for the next socket message. Refreshing the
  // threads themselves on the same tick keeps heartbeat_at current — otherwise
  // the watchdog judges liveness from the snapshot taken when the run opened.
  useEffect(() => {
    if (!current) return;
    const t = setInterval(() => {
      setNow(Date.now());
      void refreshLanes();
    }, 15_000);
    return () => clearInterval(t);
  }, [current, refreshLanes]);

  const { data: tickets = [] } = useQuery({
    queryKey: qk.tickets,
    queryFn: () => api.get<{ id: number; title: string }[]>("/hydration/my-tickets"),
    retry: false,
  });

  const start = async (repo?: string, title?: string) => {
    setBusy(true);
    try {
      const run = await createRun({
        mode,
        task: title ?? task,
        repo,
        fanout: fanout === "" || Number.isNaN(fanout) ? undefined : fanout,
      });
      setTask("");
      await openRun(run.id);
    } finally {
      setBusy(false);
    }
  };

  const send = async () => {
    if (!task.trim()) return;
    setBusy(true);
    try {
      // A mode switch takes effect on the next message, not immediately: the
      // current turn finishes undisturbed, and this send runs the new
      // blueprint (which respawns the thread on the prior session volume).
      if (current && pendingMode && pendingMode !== current.mode) {
        await sendIntent("switch_mode", { payload: { mode: pendingMode } });
        setPendingMode(null);
      }
      await sendIntent("send_message", { text: task.trim() });
      setTask("");
    } finally {
      setBusy(false);
    }
  };

  const working = current ? agentWorking(current.stage) : false;
  const submit = () => void (current ? send() : start());
  const placeholder = current
    ? working
      ? "nudge the lead — it hears you mid-work…"
      : "message the lead…"
    : mode === "agent-rnd"
      ? 'investigate across the fleet… ("spawn 5 explorers on ClientApp")'
      : "describe the task…";

  const me = useSession((s) => s.me);
  const hour = new Date().getHours();
  const daypart = hour < 12 ? "morning" : hour < 17 ? "afternoon" : "evening";
  const firstName = me?.display_name?.split(" ")[0];

  const suggest = (s: (typeof SUGGESTIONS)[number]) => {
    setMode(s.mode);
    setTask(s.prompt);
    document.getElementById("session-composer")?.focus();
  };

  return (
    <div className="flex h-full w-full flex-col">
      <SessionTabs
        runs={runs}
        tabRuns={runs.filter((r) => !closedTabs.includes(r.id))}
        current={current}
        // Opening from history un-dismisses the tab, so a reopened run lands
        // back in the strip instead of vanishing the moment you leave it.
        onOpen={(runId) => {
          reopenTab(runId);
          if (runId !== current?.id) void openRun(runId);
        }}
        onNew={() => closeRun()}
        onClose={(runId) => {
          closeTab(runId);
          if (runId === current?.id) closeRun();
        }}
      />

      {current ? (
        <>
          <header className="flex flex-none items-center gap-s4 border-b border-hairline px-s5 py-s3">
            <div className="min-w-0 flex-1">
              <div className="truncate text-[14px] font-semibold text-ink-primary">{current.title}</div>
              <PipelineBar stage={current.stage} />
            </div>
            <div className="flex flex-none items-center gap-s2">
              {(current.available_actions.includes("approve_plan") ||
                current.available_actions.includes("review_plan")) && (
                <Button variant="outline" size="sm" onClick={() => pushOverlay({ kind: "plan" })} className="font-mono">
                  plan
                </Button>
              )}
              {current.available_actions.includes("merge_pr") && (
                <Button variant="outline" size="sm" onClick={() => pushOverlay({ kind: "pr" })} className="font-mono">
                  pr
                </Button>
              )}
              <span
                title={socketConnected ? "live" : "reconnecting"}
                className={`flex items-center gap-s2 font-mono text-[11px] ${socketConnected ? "text-ok-bright" : "text-ink-faint"}`}
              >
                <span className={socketConnected ? "led" : "led led--off"} aria-hidden="true" />
                {socketConnected ? "live" : "…"}
              </span>
              <span className="sr-only" aria-live="polite">
                {socketConnected ? "run socket live" : "run socket reconnecting"}
              </span>
            </div>
          </header>

          <div className="flex min-h-0 flex-1 flex-col px-s5">
            <SwarmView
              threads={threads}
              now={now}
              stage={current.stage}
              onOpenThread={(threadId) => pushOverlay({ kind: "thread", threadId })}
              onNudge={(threadId) => void sendIntent("nudge", { threadId, text: "status check — report progress" })}
              onLetItRun={(threadId) => void sendIntent("let_it_run", { threadId })}
            />

            {current.auto_summary && (
              <div className="mt-s3 max-h-[140px] flex-none overflow-y-auto rounded-r-md border-l-2 border-green bg-bg-module px-s4 py-2.5 text-[13px] leading-[1.6] whitespace-pre-wrap">
                <div className="text-micro mb-s1 text-ink-faint">lead</div>
                {current.auto_summary}
              </div>
            )}

            {/* The ONLY scrollable region in a session: everything around it is
                flex-none, so the stream absorbs the height instead of the page. */}
            <div className="min-h-0 flex-1 overflow-hidden">
              <EventStream events={events} deltas={deltas} prompt={current.title} promptTs={current.created_at} />
            </div>
          </div>
        </>
      ) : (
        <div className="mx-auto flex min-h-0 w-full max-w-canvas flex-1 flex-col overflow-y-auto px-s8 py-s6">
          <PageHead title="new session" sub="describe the task below — past runs live in history above" />

          <div className="flex flex-1 flex-col items-center justify-center gap-s6 py-s8">
            <div className="text-center">
              <div className="mb-s2 text-[22px] font-semibold text-ink-primary">
                good {daypart}
                {firstName ? `, ${firstName}` : ""}
              </div>
              <div className="mb-s3 text-[13px] text-ink-faint">describe the task below — or pick a starting point</div>
              {/* The two pillars, side by side: the swarm does the work, the
                  fleet is what it works on. */}
              <div className="flex items-center justify-center gap-s3 font-mono text-[11px] text-ink-faint">
                <span className="flex items-center gap-s2" title="one lead, many explorers — fan out on any task">
                  <span className="led" aria-hidden="true" />
                  powered by swarm agents
                </span>
                <span aria-hidden="true">·</span>
                <span className="flex items-center gap-s2" title="the repos, patrols and PRs they run against">
                  <span className="led led--blue" aria-hidden="true" />
                  runs across your fleet
                </span>
              </div>
            </div>

            <div className="grid w-full max-w-[560px] grid-cols-1 gap-s2 sm:grid-cols-2">
              {SUGGESTIONS.map((s, i) => (
                <button
                  key={s.mode}
                  type="button"
                  onClick={() => suggest(s)}
                  className={cn(
                    "group rounded-lg border border-hairline bg-bg-panel p-s3 text-left transition-colors duration-fast hover:border-green",
                    i === SUGGESTIONS.length - 1 && "sm:col-span-2",
                  )}
                >
                  <div className="text-micro mb-s1 text-green-bright">{s.label}</div>
                  <div className="text-[13px] text-ink-secondary transition-colors duration-fast group-hover:text-ink-primary">
                    {s.prompt}
                  </div>
                </button>
              ))}
            </div>

            <div className="font-mono text-[11px] text-ink-faint">
              ⌘K command palette · enter to send · shift+enter for a newline
            </div>
          </div>

          {tickets.length > 0 && (
            <section aria-label="my ADO tickets" className="mt-s6">
              <div className="text-micro mb-s3 text-ink-faint">my ADO tickets</div>
              <div className="flex flex-col gap-s2">
                {tickets.map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => void start(undefined, t.title)}
                    className="rounded-md border border-hairline bg-bg-module px-s3 py-2.5 text-left font-mono text-[12.5px] text-blue-bright transition-colors duration-fast hover:border-blue-bright"
                  >
                    #{t.id} {t.title}
                  </button>
                ))}
              </div>
            </section>
          )}
        </div>
      )}

      <section
        className="flex-none border-t border-hairline bg-bg-panel"
        aria-label={current ? "message the lead" : "new run"}
      >
        {current && <ApprovalQueue runId={current.id} />}
        {current && (
          <SessionResume run={current} working={working} onResumed={(runId) => void openRun(runId)} />
        )}
        {current && (
          <ActionCard
            stage={current.stage}
            actions={current.available_actions}
            working={working}
            onFire={(intent, confirmed) => void sendIntent(intent, { confirmed })}
          />
        )}
        <div className="mx-auto w-full max-w-canvas px-s5 py-s2">
          <MentionTextarea
            id="session-composer"
            rows={1}
            placeholder={placeholder}
            value={task}
            onChange={(e) => setTask(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && task.trim()) {
                e.preventDefault();
                submit();
              }
            }}
            className="mb-s2 max-h-36 min-h-9 resize-none overflow-y-auto border-0 bg-transparent px-0 py-1.5 shadow-none focus-visible:ring-0"
          />
          <div className="flex flex-wrap items-center gap-s2">
            <FilterChips
              options={[...MODE_OPTIONS]}
              value={current ? (pendingMode ?? (current.mode as Mode)) : mode}
              onChange={(v) => (current ? setPendingMode(v as Mode) : setMode(v as Mode))}
              disabledValues={
                current && !current.repo
                  ? new Set<Mode>(["development"])
                  : undefined
              }
            />
            {current ? (
              <ThreadChips threads={threads} onOpen={(threadId) => pushOverlay({ kind: "thread", threadId })} />
            ) : mode === "agent-rnd" ? (
              <Input
                type="number"
                min={1}
                placeholder="threads"
                value={fanout}
                onChange={(e) => {
                  // L-38: Number(""/non-numeric) is NaN; JSON.stringify(NaN)
                  // serializes to null on send, so the backend got
                  // fanout:null instead of "unset". Coerce NaN back to ""
                  // so the empty sentinel round-trips correctly.
                  if (e.target.value === "") { setFanout(""); return; }
                  const n = Number(e.target.value);
                  setFanout(Number.isNaN(n) ? "" : n);
                }}
                title="swarm width — the Lead still authors the slices"
                className="w-[84px] font-mono"
              />
            ) : null}
            <Button className="ml-auto font-mono" disabled={busy || !task.trim()} onClick={submit}>
              <span className="size-2 rounded-full bg-current shadow-[0_0_6px_1px_currentColor]" aria-hidden="true" />
              {current ? "send" : busy ? "routing…" : "route it"}
            </Button>
          </div>
        </div>
      </section>

      {overlays.map((o, i) =>
        o.kind === "thread" ? (
          <ThreadOverlay key={`thread-${o.threadId}-${i}`} threadId={o.threadId} />
        ) : o.kind === "plan" ? (
          <PlanOverlay key={`plan-${i}`} />
        ) : (
          <PROverlay key={`pr-${i}`} />
        ),
      )}
    </div>
  );
}

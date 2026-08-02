import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { FilterChips } from "@/components/ui/filter-chips";
import { Input } from "@/components/ui/input";
import { PageHead } from "@/components/ui/page-head";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusLamp } from "@/components/ui/status-lamp";
import { EmptyState } from "@/components/ui/empty-state";
import { Textarea } from "@/components/ui/textarea";
import { ActionCard } from "../../components/ActionCard";
import { ApprovalQueue } from "../../components/ApprovalQueue";
import { EventStream } from "../../components/EventStream";
import { PipelineBar } from "../../components/PipelineBar";
import { SessionResume } from "../../components/SessionResume";
import { SessionTabs } from "../../components/SessionTabs";
import { agentWorking, stageMeta } from "../../lib/runMachine";
import { api } from "../../lib/api";
import { qk } from "../../lib/queryKeys";
import { useRuns } from "../../stores/run";
import { useUi } from "../../stores/ui";
import { SwarmView } from "../swarm/SwarmView";
import { LaneOverlay } from "./LaneOverlay";
import { PlanOverlay } from "./PlanOverlay";
import { PROverlay } from "./PROverlay";
import type { Run } from "../../types";

/** The one operating screen. With no run open it lists the rack and takes a
 *  new task; opening a run swaps the same column to the live session —
 *  swarm strip, glass-box trace, action card, composer. Approvals surface
 *  inline on the action card, so a decision never costs a navigation. */
const MODE_OPTIONS = [
  { value: "ask", label: "ask" },
  { value: "plan", label: "plan" },
  { value: "development", label: "develop" },
  { value: "debug", label: "debug" },
  { value: "agent-rnd", label: "swarm" },
] as const;

type Mode = (typeof MODE_OPTIONS)[number]["value"];

function RunCard({ run, onOpen }: { run: Run; onOpen: () => void }) {
  const meta = stageMeta(run.stage);
  return (
    <button
      type="button"
      onClick={onOpen}
      data-testid={`run-card-${run.id}`}
      className="mb-s3 w-full rounded-lg border border-hairline bg-bg-panel p-s4 text-left shadow-card transition-colors duration-fast hover:border-blue-bright"
    >
      <div className="mb-s2 flex items-center justify-between">
        <StatusLamp tone={meta.tone} label={meta.label} />
        <span className="font-mono text-[11px] text-ink-faint">{run.mode}</span>
      </div>
      <div className="mb-s1 text-[15px] font-semibold text-ink-primary">{run.title}</div>
      {run.auto_summary && (
        <div className="mb-s2 line-clamp-2 text-[13px] text-ink-secondary">{run.auto_summary.slice(0, 140)}</div>
      )}
      <div className="flex items-center justify-between font-mono text-[11px] text-ink-faint">
        <span>{run.repo ?? "fleet"}</span>
        <span className="tabular">${run.cost_usd.toFixed(2)}</span>
      </div>
    </button>
  );
}

function RunListSkeleton() {
  return (
    <div aria-label="loading runs">
      {[0, 1, 2].map((i) => (
        <div key={i} className="mb-s3 rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card">
          <div className="mb-s2 flex items-center justify-between">
            <Skeleton className="h-3 w-20 rounded-sm" />
            <Skeleton className="h-3 w-12 rounded-sm" />
          </div>
          <Skeleton className="mb-s2 h-4 w-3/4 rounded-sm" />
          <Skeleton className="h-3 w-1/2 rounded-sm" />
        </div>
      ))}
    </div>
  );
}

export function SessionsScreen() {
  const {
    runs,
    runsLoaded,
    loadRuns,
    openRun,
    closeRun,
    createRun,
    current,
    lanes,
    events,
    deltas,
    socketConnected,
    sendIntent,
  } = useRuns();
  const { overlays, pushOverlay } = useUi();
  const [task, setTask] = useState("");
  const [mode, setMode] = useState<Mode>("ask");
  const [fanout, setFanout] = useState<number | "">("");
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  // Lane heartbeats age against wall time; re-render occasionally so stale
  // lanes surface without waiting for the next socket message.
  useEffect(() => {
    if (!current) return;
    const t = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(t);
  }, [current]);

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
        fanout: fanout === "" ? undefined : fanout,
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

  return (
    <div className="flex h-full w-full flex-col">
      <SessionTabs
        runs={runs}
        current={current}
        onOpen={(runId) => void (runId === current?.id ? undefined : openRun(runId))}
        onNew={closeRun}
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
              lanes={lanes}
              now={now}
              onOpenLane={(laneId) => pushOverlay({ kind: "lane", laneId })}
              onNudge={(laneId) => void sendIntent("nudge", { laneId, text: "status check — report progress" })}
              onLetItRun={(laneId) => void sendIntent("let_it_run", { laneId })}
            />

            {current.auto_summary && (
              <div className="mt-s3 flex-none rounded-r-md border-l-2 border-green bg-bg-module px-s4 py-2.5 text-[13px] leading-[1.6] whitespace-pre-wrap">
                <div className="text-micro mb-s1 text-ink-faint">lead</div>
                {current.auto_summary}
              </div>
            )}

            <div className="min-h-0 flex-1">
              <EventStream events={events} deltas={deltas} />
            </div>
          </div>
        </>
      ) : (
        <div className="mx-auto min-h-0 w-full max-w-canvas flex-1 overflow-y-auto px-s8 py-s6">
          <PageHead title="sessions" sub="your runs — describe a task to start a new one" />

          {!runsLoaded ? (
            <RunListSkeleton />
          ) : runs.length === 0 ? (
            <EmptyState
              title="your rack is empty"
              hint="no runs yet — describe the first one below"
              action={
                <Button
                  type="button"
                  size="sm"
                  className="font-mono"
                  onClick={() => document.getElementById("session-composer")?.focus()}
                >
                  route a run
                </Button>
              }
            />
          ) : (
            runs.map((r) => <RunCard key={r.id} run={r} onOpen={() => void openRun(r.id)} />)
          )}

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
        <div className="mx-auto w-full max-w-canvas px-s5 py-s4">
          <Textarea
            id="session-composer"
            rows={3}
            placeholder={placeholder}
            value={task}
            onChange={(e) => setTask(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && task.trim()) {
                e.preventDefault();
                submit();
              }
            }}
            className="mb-s3 resize-none border-0 bg-transparent px-0 shadow-none focus-visible:ring-0"
          />
          <div className="flex flex-wrap items-center gap-s2">
            {!current && (
              <>
                <FilterChips options={[...MODE_OPTIONS]} value={mode} onChange={(v) => setMode(v as Mode)} />
                {mode === "agent-rnd" && (
                  <Input
                    type="number"
                    min={1}
                    placeholder="lanes"
                    value={fanout}
                    onChange={(e) => setFanout(e.target.value === "" ? "" : Number(e.target.value))}
                    title="swarm width — the Lead still authors the slices"
                    className="w-[84px] font-mono"
                  />
                )}
              </>
            )}
            <Button className="ml-auto font-mono" disabled={busy || !task.trim()} onClick={submit}>
              <span className="size-2 rounded-full bg-current shadow-[0_0_6px_1px_currentColor]" aria-hidden="true" />
              {current ? "send" : busy ? "routing…" : "route it"}
            </Button>
          </div>
        </div>
      </section>

      {overlays.map((o, i) =>
        o.kind === "lane" ? (
          <LaneOverlay key={`lane-${o.laneId}-${i}`} laneId={o.laneId} />
        ) : o.kind === "plan" ? (
          <PlanOverlay key={`plan-${i}`} />
        ) : (
          <PROverlay key={`pr-${i}`} />
        ),
      )}
    </div>
  );
}

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
import { stageMeta } from "../../lib/runMachine";
import { api } from "../../lib/api";
import { qk } from "../../lib/queryKeys";
import { useRuns } from "../../stores/run";
import { useUi } from "../../stores/ui";
import type { Run } from "../../types";

/** Inbox (list archetype): patch-cable composer rail (jack strip of modes)
 *  + Stripe-density run cards. Routing a run is motion moment #2. */
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

export function InboxScreen() {
  const { runs, runsLoaded, loadRuns, openRun, createRun } = useRuns();
  const setScreen = useUi((s) => s.setScreen);
  const [task, setTask] = useState("");
  const [mode, setMode] = useState<Mode>("ask");
  const [fanout, setFanout] = useState<number | "">("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  const { data: tickets = [] } = useQuery({
    queryKey: qk.tickets,
    queryFn: () => api.get<{ id: number; title: string }[]>("/hydration/my-tickets"),
    retry: false,
  });

  const openAndMonitor = async (runId: string) => {
    await openRun(runId);
    setScreen("monitor");
  };

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
      await openAndMonitor(run.id);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto h-full max-w-canvas overflow-y-auto px-s8 py-s6">
      <PageHead title="run inbox" sub="your runs — patch a task into the rack" />

      <div className="grid grid-cols-[400px_1fr] items-start gap-s6 max-lg:grid-cols-1">
        <div>
          <section className="mb-s5 rounded-lg border border-hairline bg-bg-panel p-s5 shadow-pop" aria-label="new run">
            <div className="text-micro mb-s3 text-ink-faint">new run — pick a mode</div>
            <div className="mb-s4 flex flex-wrap items-center gap-s2">
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
            </div>
            <Textarea
              rows={3}
              placeholder={
                mode === "agent-rnd"
                  ? 'investigate across the fleet… ("spawn 5 explorers on ClientApp")'
                  : "describe the task…"
              }
              value={task}
              onChange={(e) => setTask(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && task.trim() && (e.preventDefault(), void start())}
              className="mb-s4"
            />
            <Button className="w-full" disabled={busy || !task.trim()} onClick={() => void start()}>
              <span
                className="size-2 rounded-full bg-current shadow-[0_0_6px_1px_currentColor]"
                aria-hidden="true"
              />
              {busy ? "routing…" : "route it"}
            </Button>
          </section>

          {tickets.length > 0 && (
            <section aria-label="my ADO tickets">
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

        <section aria-label="my runs" className="min-w-0">
          <div className="text-micro mb-s3 text-ink-faint">my runs</div>
          {!runsLoaded ? (
            <RunListSkeleton />
          ) : runs.length === 0 ? (
            <EmptyState hint="no runs yet — route the first one on the left" />
          ) : (
            runs.map((r) => <RunCard key={r.id} run={r} onOpen={() => void openAndMonitor(r.id)} />)
          )}
        </section>
      </div>
    </div>
  );
}

import { cn } from "@/lib/utils";

/** The thread sidebar (plan §19) — the left rail listing all threads in the
 *  run. Each thread is a row: status LED, persona label, repo scope, step
 *  count, budget bar. Clicking selects the thread (filters the feed). The
 *  critical-path thread (plan §4) is highlighted. */

export interface SidebarThread {
  id: string;
  persona: string;
  repo_scope: string | null;
  status: "queued" | "running" | "idle" | "interrupted" | "completed" | "failed" | "stopped";
  steps: number;
  cost_usd: number;
  budget_usd: number;
  is_critical?: boolean;
}

const STATUS_LED: Record<SidebarThread["status"], string> = {
  queued: "led--off",
  running: "",
  idle: "led--warn",
  interrupted: "led--warn",
  completed: "led--off",
  failed: "led--red",
  stopped: "led--off",
};

export function ThreadSidebar({
  threads,
  selectedId,
  onSelect,
}: {
  threads: SidebarThread[];
  selectedId?: string;
  onSelect: (id: string) => void;
}) {
  return (
    <nav
      className="flex h-full w-rail flex-none flex-col border-r border-hairline bg-bg-panel"
      data-testid="thread-sidebar"
      aria-label="threads"
    >
      <div className="flex items-center gap-s2 px-s4 py-s3">
        <span className="text-micro text-ink-faint">threads</span>
        <span className="ml-auto font-mono text-[11px] text-ink-faint">
          {threads.length}
        </span>
      </div>
      <div className="flex-1 overflow-y-auto">
        {threads.length === 0 && (
          <div className="px-s4 py-s4 font-mono text-[11px] text-ink-faint">
            no threads yet
          </div>
        )}
        {threads.map((t) => {
          const pct = t.budget_usd > 0 ? Math.min(t.cost_usd / t.budget_usd, 1) : 0;
          return (
            <button
              key={t.id}
              onClick={() => onSelect(t.id)}
              className={cn(
                "flex w-full flex-col gap-s1 border-b border-hairline px-s4 py-s3 text-left hover:bg-bg-module",
                selectedId === t.id && "bg-bg-module",
                t.is_critical && "border-l-2 border-l-green",
              )}
              aria-current={selectedId === t.id}
            >
              <div className="flex items-center gap-s2">
                <span className={cn("led", STATUS_LED[t.status])} aria-hidden="true" />
                <span className="truncate font-mono text-[12px] text-ink-primary">
                  {t.persona}
                </span>
                {t.is_critical && (
                  <span className="ml-auto text-micro text-green-bright">crit</span>
                )}
              </div>
              {t.repo_scope && (
                <div className="truncate font-mono text-[10.5px] text-ink-faint">
                  {t.repo_scope}
                </div>
              )}
              <div className="flex items-center gap-s2">
                <span className="font-mono text-[10.5px] text-ink-faint">
                  {t.steps} steps
                </span>
                <span className="ml-auto font-mono text-[10.5px] text-ink-faint">
                  ${t.cost_usd.toFixed(2)} / ${t.budget_usd.toFixed(2)}
                </span>
              </div>
              {/* Budget bar */}
              <div className="river-track h-[2px]">
                <div
                  className="h-full rounded-pill bg-green"
                  style={{ width: `${pct * 100}%` }}
                />
              </div>
            </button>
          );
        })}
      </div>
    </nav>
  );
}

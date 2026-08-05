import { useState } from "react";
import { HistoryIcon, PlusIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import type { LampTone } from "@/components/ui/status-lamp";
import { cn } from "@/lib/utils";
import { stageMeta } from "../lib/runMachine";
import { formatDateTime } from "../lib/time";
import type { Run } from "../types";

const RECENT_TABS = 5;

const LED_CLASS: Record<LampTone, string> = {
  ok: "led",
  info: "led led--blue",
  warn: "led led--warn",
  danger: "led led--red",
  off: "led led--off",
};

/** Tabs are tight on width, so the stage rides as an LED with its word in an
 *  sr-only span — the state stays announced without a visible label. */
function StageLed({ tone, label }: { tone: LampTone; label: string }) {
  return (
    <>
      <span className={LED_CLASS[tone]} aria-hidden="true" />
      <span className="sr-only">{label}</span>
    </>
  );
}

/** Session tab strip: the five most recent runs sit as tabs across the top,
 *  everything older lives behind the history button. The open run is always
 *  present in the strip even when it has aged past the cut. */
export function SessionTabs({
  runs,
  tabRuns,
  current,
  onOpen,
  onNew,
  onClose,
}: {
  /** Every run — history lists these, including tabs the user has closed. */
  runs: Run[];
  /** Runs eligible for the strip, minus anything dismissed. */
  tabRuns: Run[];
  current: Run | null;
  onOpen: (runId: string) => void;
  onNew: () => void;
  onClose: (runId: string) => void;
}) {
  const [historyOpen, setHistoryOpen] = useState(false);

  const recent = tabRuns.slice(0, RECENT_TABS);
  const tabs =
    current && !recent.some((r) => r.id === current.id) ? [current, ...recent].slice(0, RECENT_TABS) : recent;

  return (
    <div className="flex flex-none items-center gap-s1 border-b border-hairline bg-bg-panel px-s3">
      <div className="flex min-w-0 flex-1 items-center gap-s1 overflow-x-auto">
        {tabs.map((run) => {
          const meta = stageMeta(run.stage);
          const active = current?.id === run.id;
          return (
            <div
              key={run.id}
              className={cn(
                "group flex min-w-0 max-w-[240px] flex-none items-center gap-s2 border-b-2 pl-s3 pr-s2 transition-colors duration-fast",
                active ? "border-green" : "border-transparent",
              )}
            >
              <button
                type="button"
                onClick={() => onOpen(run.id)}
                title={run.title}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex min-w-0 items-center gap-s2 py-s2 font-mono text-[12px] transition-colors duration-fast",
                  active ? "text-ink-primary" : "text-ink-secondary hover:text-ink-primary",
                )}
              >
                <StageLed tone={meta.tone} label={meta.label} />
                <span className="truncate">{run.title}</span>
              </button>
              {/* Dismissal is reversible and rare, so it stays out of the way
                  until the tab is hovered or the close button takes focus. */}
              <button
                type="button"
                onClick={() => onClose(run.id)}
                title="close tab — the run stays in history"
                aria-label={`close ${run.title}`}
                className="flex-none rounded-sm p-0.5 text-ink-faint opacity-0 transition-opacity duration-fast hover:text-ink-primary focus-visible:opacity-100 group-hover:opacity-100"
              >
                <XIcon className="size-3" aria-hidden="true" />
              </button>
            </div>
          );
        })}
        {tabs.length === 0 && (
          <span className="px-s2 py-s2 font-mono text-[12px] text-ink-faint">
            {runs.length === 0 ? "no sessions yet" : "all tabs closed — reopen one from history"}
          </span>
        )}
      </div>

      <div className="flex flex-none items-center gap-s1">
        <Button
          variant="ghost"
          size="sm"
          className="font-mono"
          onClick={() => setHistoryOpen(true)}
          title="all sessions"
        >
          <HistoryIcon aria-hidden="true" />
          history
        </Button>
        <Button variant="ghost" size="sm" className="font-mono" onClick={onNew} title="new session">
          <PlusIcon aria-hidden="true" />
          new
        </Button>
      </div>

      <Dialog open={historyOpen} onOpenChange={setHistoryOpen}>
        <DialogContent className="max-h-[80vh] w-[720px] overflow-y-auto sm:max-w-[720px]">
          <DialogHeader>
            <DialogTitle>session history</DialogTitle>
            <DialogDescription>every run you have started, newest first</DialogDescription>
          </DialogHeader>
          {runs.length === 0 ? (
            <EmptyState hint="no sessions yet" />
          ) : (
            <div className="flex flex-col gap-s1">
              {runs.map((run) => {
                const meta = stageMeta(run.stage);
                return (
                  <button
                    key={run.id}
                    type="button"
                    onClick={() => {
                      onOpen(run.id);
                      setHistoryOpen(false);
                    }}
                    className="flex items-center gap-s3 rounded-md border border-transparent px-s3 py-s2 text-left transition-colors duration-fast hover:border-hairline hover:bg-bg-module"
                  >
                    <StageLed tone={meta.tone} label={meta.label} />
                    <span className="min-w-0 flex-1 truncate text-[13px] text-ink-primary">{run.title}</span>
                    <span className="flex-none font-mono text-[11px] text-ink-faint">{run.mode}</span>
                    <span className="flex-none font-mono text-[11px] text-ink-faint">
                      {formatDateTime(run.created_at ?? run.last_active_at)}
                    </span>
                    <span className="tabular flex-none font-mono text-[11px] text-ink-faint">
                      ${run.cost_usd.toFixed(2)}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
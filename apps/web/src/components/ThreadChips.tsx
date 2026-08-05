import { cn } from "@/lib/utils";
import type { Thread, LaneStatus } from "../types";

/** Compact thread chip for the composer row — the per-thread affordance that
 *  replaces the always-on swarm strip for genuine swarms. One chip per
 *  worker thread, opening the existing ThreadOverlay on click. Hidden entirely
 *  for a single-thread ask run (see SwarmView). */
const LED: Record<LaneStatus, string> = {
  running: "led",
  queued: "led led--blue",
  idle: "led led--blue",
  completed: "led led--off",
  interrupted: "led led--off",
  stopped: "led led--red",
  failed: "led led--red",
  replaced: "led led--off",
  pinned: "led led--blue",
};

export function ThreadChips({
  threads,
  onOpen,
}: {
  threads: Thread[];
  onOpen: (threadId: string) => void;
}) {
  const lead = threads.find((l) => l.persona === "lead");
  const workers = threads.filter((l) => l !== lead);
  // Same gate as SwarmView: only a real swarm gets chips.
  const isSwarm = workers.length > 1 || lead !== undefined;
  if (!isSwarm) return null;

  return (
    <div className="flex flex-wrap items-center gap-s2" data-testid="thread-chips">
      {lead && (
        <button
          type="button"
          onClick={() => onOpen(lead.id)}
          className="inline-flex items-center gap-s1 rounded-pill border border-blue px-2.5 py-1 font-mono text-[11px] font-semibold text-ink-primary transition-colors duration-fast hover:border-blue-bright"
          title={`open ${lead.persona} trace`}
        >
          <span className={LED[lead.status] ?? "led led--off"} aria-hidden="true" />
          lead
        </button>
      )}
      {workers.map((l) => (
        <button
          key={l.id}
          type="button"
          onClick={() => onOpen(l.id)}
          title={`open ${l.persona} trace`}
          className={cn(
            "inline-flex items-center gap-s1 rounded-pill border border-hairline px-2.5 py-1 font-mono text-[11px] text-ink-secondary transition-colors duration-fast hover:border-blue-bright hover:text-ink-primary",
          )}
        >
          <span className={LED[l.status] ?? "led led--off"} aria-hidden="true" />
          {l.persona}
        </button>
      ))}
    </div>
  );
}

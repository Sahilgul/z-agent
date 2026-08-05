import { cn } from "@/lib/utils";
import type { Lane, LaneStatus } from "../types";

/** Lane river row (monitor signature): each lane is a streaming channel —
 *  identity on the left, the river track in the middle with an activity
 *  pulse flowing left→right while it works, step/cost readout on the right.
 *  critical = on the swarm's critical path; stale = heartbeat lost. */
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

const PULSE: Partial<Record<LaneStatus, string>> = {
  running: "text-ok-bright",
  queued: "text-blue-bright",
  idle: "text-blue-bright",
};

export function LaneTile({
  lane,
  critical,
  stale,
  onOpen,
}: {
  lane: Lane;
  critical?: boolean;
  stale?: boolean;
  onOpen: (laneId: string) => void;
}) {
  const pulse = PULSE[lane.status];
  return (
    <button
      type="button"
      onClick={() => onOpen(lane.id)}
      data-testid={`lane-tile-${lane.id}`}
      title={`open ${lane.persona} trace`}
      className={cn(
        "grid w-full grid-cols-[minmax(120px,180px)_1fr_auto] items-center gap-s4 rounded-md border border-hairline bg-bg-module px-s4 py-2.5 text-left transition-colors duration-fast hover:border-blue-bright",
        critical && "border-green shadow-[0_0_10px_color-mix(in_srgb,var(--color-green)_30%,transparent)]",
        stale && "border-warn",
      )}
    >
      <span className="flex min-w-0 items-center gap-s2">
        <span className={LED[lane.status] ?? "led led--off"} aria-hidden="true" />
        <span className="min-w-0">
          <span className="block truncate font-mono text-[12px] font-semibold text-ink-primary">{lane.persona}</span>
          <span className="block truncate text-[11px] text-ink-faint">{lane.repo_scope ?? "read-only"}</span>
        </span>
      </span>
      <span className="river-track" aria-hidden="true">
        {pulse && <span className={cn("river-pulse", pulse)} />}
      </span>
      <span className="flex items-center gap-s3 font-mono text-[10.5px] tabular text-ink-faint">
        <span>{lane.steps} steps</span>
        <span>${lane.cost_usd.toFixed(2)}</span>
      </span>
    </button>
  );
}

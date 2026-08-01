import type { Lane } from "../types";

/** Lane module (Patch Bay): LED status, persona, scope, step/cost jack meta.
 *  Click opens the lane's trace overlay — subagent tiles are buttons, not rows
 *  in a list. critical = on the swarm's critical path (brighter bezel). */
const LED: Record<string, string> = {
  running: "led",
  queued: "led blue",
  idle: "led blue",
  completed: "led off",
  interrupted: "led off",
  stopped: "led red",
  failed: "led red",
  replaced: "led off",
  pinned: "led blue",
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
  return (
    <button
      className={`lane-tile ${critical ? "critical" : ""} ${stale ? "stale" : ""}`}
      onClick={() => onOpen(lane.id)}
      data-testid={`lane-tile-${lane.id}`}
    >
      <div className="lt-top">
        <span className="lt-name mono">{lane.persona}</span>
        <span className={LED[lane.status] ?? "led off"} aria-label={lane.status} />
      </div>
      <div className="lt-scope">{lane.repo_scope ?? "read-only"}</div>
      <div className="lt-jack mono">
        <span>{lane.steps} steps</span>
        <span>${lane.cost_usd.toFixed(2)}</span>
      </div>
      <style>{`
        .lane-tile {
          background: var(--bg-module); border: 1px solid var(--hairline);
          border-radius: var(--radius); padding: 12px 14px; text-align: left;
          color: var(--ink-primary); cursor: pointer; width: 100%;
          transition: border-color .15s ease, transform .15s ease;
        }
        .lane-tile:hover { border-color: var(--blue-bright); transform: translateY(-1px); }
        .lane-tile.critical { border-color: var(--green); box-shadow: 0 0 10px color-mix(in srgb, var(--green) 30%, transparent); }
        .lane-tile.stale { border-color: var(--danger); }
        .lt-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
        .lt-name { font-weight: 600; font-size: 12.5px; }
        .lt-scope { font-size: 12px; color: var(--ink-secondary); min-height: 18px; margin-bottom: 10px; }
        .lt-jack {
          display: flex; justify-content: space-between; border-top: 1px dashed var(--hairline);
          padding-top: 8px; font-size: 10px; color: var(--ink-faint);
        }
      `}</style>
    </button>
  );
}

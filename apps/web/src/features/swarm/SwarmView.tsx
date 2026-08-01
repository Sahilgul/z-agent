import { criticalLaneIds, isStaleLane } from "../../lib/runMachine";
import { LaneTile } from "../../components/LaneTile";
import type { Lane } from "../../types";

/** Swarm view (§4 Phase 3): the Lead module plus one tile per lane, critical
 *  path bezel, and watchdog cards for stale-heartbeat lanes (nudge / let it
 *  run — the lane keeps working on dismissal). */
export function SwarmView({
  lanes,
  now,
  onOpenLane,
  onNudge,
  onLetItRun,
}: {
  lanes: Lane[];
  now: number;
  onOpenLane: (laneId: string) => void;
  onNudge: (laneId: string) => void;
  onLetItRun: (laneId: string) => void;
}) {
  const critical = criticalLaneIds(lanes);
  const stale = lanes.filter((l) => isStaleLane(l, now));
  const lead = lanes.find((l) => l.persona === "lead");
  const workers = lanes.filter((l) => l !== lead);

  if (lanes.length === 0) return null;

  return (
    <section className="swarm" aria-label="swarm lanes" data-testid="swarm-view">
      {lead && (
        <div className="swarm-lead mono">
          <span className="led blue" />
          lead — orchestrates, never edits
        </div>
      )}
      <div className="swarm-grid">
        {workers.map((l) => (
          <LaneTile
            key={l.id}
            lane={l}
            critical={critical.has(l.id)}
            stale={stale.includes(l)}
            onOpen={onOpenLane}
          />
        ))}
      </div>
      {stale.map((l) => (
        <div key={`wd-${l.id}`} className="watchdog-card" data-testid={`watchdog-${l.id}`}>
          <span className="led red" />
          <span className="wd-text">
            <b className="mono">{l.persona}</b> heartbeat stale — no signal in 3+ min
          </span>
          <button className="btn btn-mono btn-ghost" onClick={() => onNudge(l.id)}>
            nudge
          </button>
          <button className="btn btn-mono btn-ghost" onClick={() => onLetItRun(l.id)}>
            let it run
          </button>
        </div>
      ))}
      <style>{`
        .swarm { border: 1px solid var(--hairline); border-radius: 8px; background: var(--bg-panel); padding: 14px 16px; margin: 10px 14px; }
        .swarm-lead {
          display: inline-flex; align-items: center; gap: 9px;
          background: var(--bg-module); border: 1px solid var(--blue); border-radius: var(--radius);
          padding: 8px 14px; font-size: 11.5px; font-weight: 600; margin-bottom: 12px;
        }
        .swarm-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }
        .watchdog-card {
          display: flex; align-items: center; gap: 10px; margin-top: 12px;
          border: 1px solid var(--danger); border-radius: var(--radius);
          padding: 8px 12px; font-size: 12.5px;
        }
        .wd-text { flex: 1; color: var(--ink-secondary); }
      `}</style>
    </section>
  );
}

import { Button } from "@/components/ui/button";
import { criticalLaneIds, isStaleLane } from "../../lib/runMachine";
import { LaneTile } from "../../components/LaneTile";
import type { Lane } from "../../types";

/** Swarm bay (live archetype): the Lead module plus the lane river — one
 *  streaming channel per worker — and watchdog cards for stale-heartbeat
 *  lanes (nudge / let it run; the lane keeps working on dismissal). */
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
    <section
      aria-label="swarm lanes"
      data-testid="swarm-view"
      className="m-s3 rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card"
    >
      {lead && (
        <div className="mb-s3 inline-flex items-center gap-s2 rounded-md border border-blue bg-bg-module px-s3 py-2 font-mono text-[11.5px] font-semibold text-ink-primary">
          <span className="led led--blue" aria-hidden="true" />
          lead — orchestrates, never edits
        </div>
      )}
      <div className="flex flex-col gap-s2">
        {workers.map((l) => (
          <LaneTile key={l.id} lane={l} critical={critical.has(l.id)} stale={stale.includes(l)} onOpen={onOpenLane} />
        ))}
      </div>
      {stale.map((l) => (
        <div
          key={`wd-${l.id}`}
          data-testid={`watchdog-${l.id}`}
          className="mt-s3 flex items-center gap-s3 rounded-md border border-danger px-s3 py-2"
        >
          <span className="led led--red" aria-hidden="true" />
          <span className="flex-1 text-[12.5px] text-ink-secondary">
            <b className="font-mono text-ink-primary">{l.persona}</b> heartbeat stale — no signal in 3+ min
          </span>
          <Button variant="outline" size="sm" onClick={() => onNudge(l.id)}>
            nudge
          </Button>
          <Button variant="ghost" size="sm" onClick={() => onLetItRun(l.id)}>
            let it run
          </Button>
        </div>
      ))}
    </section>
  );
}

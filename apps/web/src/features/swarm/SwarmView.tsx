import { Button } from "@/components/ui/button";
import { criticalLaneIds, isStaleLane } from "../../lib/runMachine";
import { LaneTile } from "../../components/LaneTile";
import type { Lane } from "../../types";

/** Swarm strip (monitor v2.3): the lead chip plus one compact tile per
 *  worker in a horizontal scroll row — the stream gets the space, lanes
 *  stay glanceable. Watchdog alerts pin below the strip. */
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
    <section aria-label="swarm lanes" data-testid="swarm-view" className="flex-none border-b border-hairline">
      <div className="flex items-stretch gap-s2 overflow-x-auto p-s3">
        {lead && (
          <div className="inline-flex flex-none items-center gap-s2 self-center rounded-md border border-blue px-s3 py-2 font-mono text-[11px] font-semibold text-ink-primary">
            <span className="led led--blue" aria-hidden="true" />
            lead — orchestrates, never edits
          </div>
        )}
        {workers.map((l) => (
          <div key={l.id} className="w-[240px] flex-none">
            <LaneTile lane={l} critical={critical.has(l.id)} stale={stale.includes(l)} onOpen={onOpenLane} />
          </div>
        ))}
      </div>
      {stale.map((l) => (
        <div
          key={`wd-${l.id}`}
          data-testid={`watchdog-${l.id}`}
          className="mx-s3 mb-s3 flex items-center gap-s3 rounded-md border border-danger px-s3 py-2"
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

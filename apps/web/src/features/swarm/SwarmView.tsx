import { Button } from "@/components/ui/button";
import { criticalLaneIds, staleLanes } from "../../lib/runMachine";
import { LaneTile } from "../../components/LaneTile";
import type { Lane, RunStage } from "../../types";

/** Swarm strip (monitor v2.3): the lead chip plus one compact tile per
 *  worker in a horizontal scroll row — the stream gets the space, lanes
 *  stay glanceable. Watchdog alerts pin below the strip.
 *
 * Hidden for a single-lane run (ask mode): one researcher IS the chat
 * partner, so a "swarm of one" plus its destructive lane controls is dead
 * weight above the conversation. The main stream already is that lane's
 * trace, so nothing becomes unreachable. */
export function SwarmView({
  lanes,
  now,
  stage,
  onOpenLane,
  onNudge,
  onLetItRun,
}: {
  lanes: Lane[];
  now: number;
  stage: RunStage;
  onOpenLane: (laneId: string) => void;
  onNudge: (laneId: string) => void;
  onLetItRun: (laneId: string) => void;
}) {
  const lead = lanes.find((l) => l.persona === "lead");
  const workers = lanes.filter((l) => l !== lead);

  // A real swarm is more than one worker lane, or any lead lane (the lead
  // orchestrates a fan-out and is never the sole voice in an ask chat).
  const isSwarm = workers.length > 1 || lead !== undefined;
  if (!isSwarm) return null;

  const critical = criticalLaneIds(lanes);
  // Terminal runs never nag: a finished run with a row stranded at "running"
  // (a lost status-change beat) would otherwise show "heartbeat stale"
  // forever. The backend's heartbeat fix keeps the row honest; this keeps
  // the UI correct even when a beat slips through.
  const stale = staleLanes(lanes, now, stage);

  return (
    <section aria-label="swarm lanes" data-testid="swarm-view" className="flex-none border-b border-hairline">
      <div className="flex items-stretch gap-s2 overflow-x-auto p-s3">
        {lead && (
          <div className="inline-flex flex-none items-center gap-s2 self-center rounded-md border border-blue px-s3 py-2 font-mono text-[11px] font-semibold text-ink-primary">
            <span className="led led--blue" aria-hidden="true" />
            lead — orchestrates, never edits
          </div>
        )}
        {/* The strip caps at two tile rows; deeper swarms scroll INSIDE this
            row instead of pushing the whole screen down and burying the stream. */}
        <div className="flex max-h-[180px] min-w-0 flex-1 flex-wrap content-start gap-s2 overflow-y-auto">
          {workers.map((l) => (
            <div key={l.id} className="w-[240px] flex-none">
              <LaneTile lane={l} critical={critical.has(l.id)} stale={stale.includes(l)} onOpen={onOpenLane} />
            </div>
          ))}
        </div>
      </div>
      {/* One banner for every stale lane — a row each would overflow the page. */}
      {stale.length > 0 && (
        <div
          data-testid="watchdog-banner"
          className="mx-s3 mb-s3 flex flex-none items-center gap-s3 rounded-md border border-danger px-s3 py-2"
        >
          <span className="led led--red" aria-hidden="true" />
          <span className="flex-1 text-[12.5px] text-ink-secondary">
            <b className="font-mono text-ink-primary">
              {stale.map((l) => l.persona).join(", ")}
            </b>{" "}
            heartbeat stale — no signal in 3+ min
          </span>
          {stale.map((l) => (
            <span key={l.id} className="flex gap-s2">
              <Button variant="outline" size="sm" onClick={() => onNudge(l.id)}>
                nudge {l.persona}
              </Button>
              <Button variant="ghost" size="sm" onClick={() => onLetItRun(l.id)}>
                let it run
              </Button>
            </span>
          ))}
        </div>
      )}
    </section>
  );
}

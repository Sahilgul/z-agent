import { Button } from "@/components/ui/button";
import { EventStream } from "../../components/EventStream";
import { OverlayShell } from "../../components/OverlayShell";
import { useRuns } from "../../stores/run";

/** Lane trace overlay: the subagent's full glass-box trace at 80%, with its
 *  lane controls — stop, pin a finding, kill & replace. The run's monitor
 *  keeps streaming underneath. */
export function LaneOverlay({ laneId }: { laneId: string }) {
  const { events, deltas, lanes, sendIntent } = useRuns();
  const lane = lanes.find((l) => l.id === laneId);
  const live = lane && ["running", "queued", "idle"].includes(lane.status);

  return (
    <OverlayShell title={`lane · ${lane?.persona ?? laneId.slice(0, 8)} · ${lane?.status ?? ""}`}>
      <div className="mb-s3 flex gap-s2">
        {live && (
          <Button variant="outline" size="sm" className="font-mono" onClick={() => void sendIntent("stop_lane", { laneId })}>
            stop lane
          </Button>
        )}
        <Button
          variant="outline"
          size="sm"
          className="font-mono"
          onClick={() => void sendIntent("pin_finding", { laneId, payload: { note: "pinned from lane overlay" } })}
        >
          pin finding
        </Button>
        {live && (
          <Button
            variant="destructive"
            size="sm"
            className="font-mono"
            onClick={() => void sendIntent("kill_replace", { laneId, confirmed: true })}
          >
            kill & replace
          </Button>
        )}
      </div>
      <EventStream events={events} deltas={deltas} laneFilter={laneId} />
    </OverlayShell>
  );
}

import { EventStream } from "../../components/EventStream";
import { OverlayShell } from "../../components/OverlayShell";
import { useRuns } from "../../stores/run";

/** Lane trace overlay (§1/§4): the subagent's full glass-box trace at 80%,
 *  with its lane controls — stop, pin a finding, kill & replace. The run's
 *  monitor keeps streaming underneath. */
export function LaneOverlay({ laneId }: { laneId: string }) {
  const { events, deltas, lanes, sendIntent } = useRuns();
  const lane = lanes.find((l) => l.id === laneId);
  const live = lane && ["running", "queued", "idle"].includes(lane.status);

  return (
    <OverlayShell title={`lane · ${lane?.persona ?? laneId.slice(0, 8)} · ${lane?.status ?? ""}`}>
      <div className="lane-controls">
        {live && (
          <button className="btn btn-mono btn-ghost" onClick={() => void sendIntent("stop_lane", { laneId })}>
            stop lane
          </button>
        )}
        <button
          className="btn btn-mono btn-ghost"
          onClick={() => void sendIntent("pin_finding", { laneId, payload: { note: "pinned from lane overlay" } })}
        >
          pin finding
        </button>
        {live && (
          <button
            className="btn btn-mono btn-danger"
            onClick={() => void sendIntent("kill_replace", { laneId, confirmed: true })}
          >
            kill & replace
          </button>
        )}
      </div>
      <EventStream events={events} deltas={deltas} laneFilter={laneId} />
      <style>{`
        .lane-controls { display: flex; gap: 8px; padding: 0 0 12px; }
      `}</style>
    </OverlayShell>
  );
}

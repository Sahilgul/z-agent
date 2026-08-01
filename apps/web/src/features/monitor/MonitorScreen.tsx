import { useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { EventStream } from "../../components/EventStream";
import { PipelineBar } from "../../components/PipelineBar";
import { useRuns } from "../../stores/run";
import { useUi } from "../../stores/ui";
import { SwarmView } from "../swarm/SwarmView";
import { ChatPane } from "./ChatPane";
import { LaneOverlay } from "./LaneOverlay";
import { PlanOverlay } from "./PlanOverlay";
import { PROverlay } from "./PROverlay";

/** Monitor (§1): the run's glass box. Left = swarm bay + live event stream
 *  (every agent step, lead and subagent, rendered the same way). Right =
 *  chat with the Lead. 50/50 default, draggable. Overlays float at 80%. */
export function MonitorScreen() {
  const { current, lanes, events, deltas, socketConnected, sendIntent } = useRuns();
  const { overlays, pushOverlay, setScreen } = useUi();
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(t);
  }, []);

  if (!current) {
    setScreen("inbox");
    return null;
  }

  return (
    <div className="monitor">
      <header className="monitor-head">
        <button className="btn btn-mono btn-ghost" onClick={() => setScreen("inbox")}>
          ← inbox
        </button>
        <div className="monitor-title">
          <div className="mt-text">{current.title}</div>
          <PipelineBar stage={current.stage} />
        </div>
        <div className="monitor-actions">
          {(current.available_actions.includes("approve_plan") ||
            current.available_actions.includes("review_plan")) && (
            <button className="btn btn-mono btn-ghost" onClick={() => pushOverlay({ kind: "plan" })}>
              plan
            </button>
          )}
          {current.available_actions.includes("merge_pr") && (
            <button className="btn btn-mono btn-ghost" onClick={() => pushOverlay({ kind: "pr" })}>
              pr
            </button>
          )}
          <span className={`mono conn ${socketConnected ? "on" : "off"}`} title={socketConnected ? "live" : "reconnecting"}>
            {socketConnected ? "● live" : "○ …"}
          </span>
        </div>
      </header>

      <PanelGroup direction="horizontal" className="monitor-split">
        <Panel defaultSize={50} minSize={30}>
          <div className="pane left">
            <SwarmView
              lanes={lanes}
              now={now}
              onOpenLane={(laneId) => pushOverlay({ kind: "lane", laneId })}
              onNudge={(laneId) => void sendIntent("nudge", { laneId, text: "status check — report progress" })}
              onLetItRun={(laneId) => void sendIntent("let_it_run", { laneId })}
            />
            <div className="stream-wrap">
              <EventStream events={events} deltas={deltas} />
            </div>
          </div>
        </Panel>
        <PanelResizeHandle className="split-handle" />
        <Panel defaultSize={50} minSize={30}>
          <ChatPane />
        </Panel>
      </PanelGroup>

      {overlays.map((o, i) =>
        o.kind === "lane" ? (
          <LaneOverlay key={`lane-${o.laneId}-${i}`} laneId={o.laneId} />
        ) : o.kind === "plan" ? (
          <PlanOverlay key={`plan-${i}`} />
        ) : (
          <PROverlay key={`pr-${i}`} />
        ),
      )}

      <style>{`
        .monitor { display: flex; flex-direction: column; height: 100%; }
        .monitor-head {
          display: flex; align-items: center; gap: 16px; padding: 8px 16px;
          border-bottom: 1px solid var(--hairline); background: var(--bg-panel);
        }
        .monitor-title { flex: 1; min-width: 0; }
        .mt-text { font-weight: 600; font-size: 14.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .monitor-actions { display: flex; align-items: center; gap: 10px; }
        .conn { font-size: 11px; }
        .conn.on { color: var(--green-bright); }
        .conn.off { color: var(--ink-faint); }
        .monitor-split { flex: 1; }
        .pane.left { display: flex; flex-direction: column; height: 100%; overflow-y: auto; }
        .stream-wrap { flex: 1; min-height: 0; }
        .split-handle { width: 3px; background: var(--hairline); cursor: col-resize; }
        .split-handle:hover, .split-handle[data-resize-handle-state="drag"] { background: var(--blue-bright); }
      `}</style>
    </div>
  );
}

import { useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { ArrowLeftIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EventStream } from "../../components/EventStream";
import { PipelineBar } from "../../components/PipelineBar";
import { useRuns } from "../../stores/run";
import { useUi } from "../../stores/ui";
import { SwarmView } from "../swarm/SwarmView";
import { ChatPane } from "./ChatPane";
import { LaneOverlay } from "./LaneOverlay";
import { PlanOverlay } from "./PlanOverlay";
import { PROverlay } from "./PROverlay";

/** Monitor (live archetype): the run's glass box, full-bleed console — no
 *  page scroll. Left = swarm bay (lane river) + live event stream; right =
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
    <div className="flex h-full flex-col">
      <header className="flex flex-none items-center gap-s4 border-b border-hairline bg-bg-panel px-s4 py-2">
        <Button variant="outline" size="sm" onClick={() => setScreen("inbox")} className="font-mono">
          <ArrowLeftIcon aria-hidden="true" />
          inbox
        </Button>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14.5px] font-semibold text-ink-primary">{current.title}</div>
          <PipelineBar stage={current.stage} />
        </div>
        <div className="flex flex-none items-center gap-s2">
          {(current.available_actions.includes("approve_plan") ||
            current.available_actions.includes("review_plan")) && (
            <Button variant="outline" size="sm" onClick={() => pushOverlay({ kind: "plan" })} className="font-mono">
              plan
            </Button>
          )}
          {current.available_actions.includes("merge_pr") && (
            <Button variant="outline" size="sm" onClick={() => pushOverlay({ kind: "pr" })} className="font-mono">
              pr
            </Button>
          )}
          <span
            title={socketConnected ? "live" : "reconnecting"}
            className={`flex items-center gap-s2 font-mono text-[11px] ${socketConnected ? "text-green-bright" : "text-ink-faint"}`}
          >
            <span className={socketConnected ? "led" : "led led--off"} aria-hidden="true" />
            {socketConnected ? "live" : "…"}
          </span>
        </div>
      </header>

      <PanelGroup direction="horizontal" className="min-h-0 flex-1">
        <Panel defaultSize={50} minSize={30}>
          <div className="flex h-full flex-col overflow-y-auto">
            <SwarmView
              lanes={lanes}
              now={now}
              onOpenLane={(laneId) => pushOverlay({ kind: "lane", laneId })}
              onNudge={(laneId) => void sendIntent("nudge", { laneId, text: "status check — report progress" })}
              onLetItRun={(laneId) => void sendIntent("let_it_run", { laneId })}
            />
            <div className="min-h-0 flex-1">
              <EventStream events={events} deltas={deltas} />
            </div>
          </div>
        </Panel>
        <PanelResizeHandle className="w-[3px] bg-hairline transition-colors duration-fast hover:bg-blue-bright data-[resize-handle-state=drag]:bg-blue-bright" />
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
    </div>
  );
}

import { Button } from "@/components/ui/button";
import { EventStream } from "../../components/EventStream";
import { OverlayShell } from "../../components/OverlayShell";
import { useRuns } from "../../stores/run";

/** Thread trace overlay: the subagent's full glass-box trace at 80%, with its
 *  thread controls — stop, pin a finding, kill & replace. The run's session
 *  keeps streaming underneath. */
export function ThreadOverlay({ threadId }: { threadId: string }) {
  const { events, deltas, threads, sendIntent } = useRuns();
  const thread = threads.find((l) => l.id === threadId);
  const live = thread && ["running", "queued", "idle"].includes(thread.status);

  return (
    <OverlayShell title={`thread · ${thread?.persona ?? threadId.slice(0, 8)} · ${thread?.status ?? ""}`}>
      <div className="mb-s3 flex gap-s2">
        {live && (
          <Button variant="outline" size="sm" className="font-mono" onClick={() => void sendIntent("stop_thread", { threadId })}>
            stop thread
          </Button>
        )}
        <Button
          variant="outline"
          size="sm"
          className="font-mono"
          onClick={() => void sendIntent("pin_finding", { threadId, payload: { note: "pinned from thread overlay" } })}
        >
          pin finding
        </Button>
        {live && (
          <Button
            variant="destructive"
            size="sm"
            className="font-mono"
            onClick={() => void sendIntent("kill_replace", { threadId, confirmed: true })}
          >
            kill & replace
          </Button>
        )}
      </div>
      <EventStream events={events} deltas={deltas} laneFilter={threadId} />
    </OverlayShell>
  );
}

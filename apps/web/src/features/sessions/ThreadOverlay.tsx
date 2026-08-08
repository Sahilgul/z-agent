import { useState } from "react";
import { Button } from "@/components/ui/button";
import { EventStream } from "../../components/EventStream";
import { OverlayShell } from "../../components/OverlayShell";
import { useRuns } from "../../stores/run";

/** Thread trace overlay: the subagent's full glass-box trace at 80%, with its
 *  thread controls — stop, pin a finding, kill & replace. The run's session
 *  keeps streaming underneath.
 *
 *  W5-M2: kill & replace fires from an OVERLAY — a context switch away from
 *  the run — so it gets a two-tap confirm (the first tap arms, the second
 *  fires) plus an in-flight disable; one misclick used to kill a thread. */
export function ThreadOverlay({ threadId }: { threadId: string }) {
  const { events, deltas, threads, sendIntent } = useRuns();
  const [busy, setBusy] = useState<string | null>(null);
  const [armingKill, setArmingKill] = useState(false);
  const thread = threads.find((l) => l.id === threadId);
  // input_required is parked-on-approval but alive: the backend considers it
  // nudgeable/stoppable, so the controls must stay reachable.
  const live = thread && ["running", "queued", "idle", "input_required"].includes(thread.status);

  const fire = (name: string, intent: Parameters<typeof sendIntent>[0], opts?: Parameters<typeof sendIntent>[1]) => {
    if (busy) return;
    setBusy(name);
    void sendIntent(intent, opts).finally(() => setBusy(null));
  };

  const onKillReplace = () => {
    if (busy) return;
    if (!armingKill) {
      setArmingKill(true);
      return;
    }
    setArmingKill(false);
    fire("kill_replace", "kill_replace", { threadId, confirmed: true });
  };

  return (
    <OverlayShell title={`thread · ${thread?.persona ?? threadId.slice(0, 8)} · ${thread?.status ?? ""}`}>
      <div className="mb-s3 flex gap-s2">
        {live && (
          <Button
            variant="outline"
            size="sm"
            className="font-mono"
            disabled={busy !== null}
            onClick={() => fire("stop_thread", "stop_thread", { threadId })}
          >
            {busy === "stop_thread" ? "stopping…" : "stop thread"}
          </Button>
        )}
        <Button
          variant="outline"
          size="sm"
          className="font-mono"
          disabled={busy !== null}
          onClick={() => fire("pin_finding", "pin_finding", { threadId, payload: { note: "pinned from thread overlay" } })}
        >
          pin finding
        </Button>
        {live && (
          <Button
            variant="destructive"
            size="sm"
            className="font-mono"
            disabled={busy !== null}
            onClick={onKillReplace}
            onBlur={() => setArmingKill(false)}
            title="kills this thread and spawns a fresh replacement on its session volume"
          >
            {armingKill ? "confirm kill & replace?" : busy === "kill_replace" ? "replacing…" : "kill & replace"}
          </Button>
        )}
      </div>
      <EventStream events={events} deltas={deltas} laneFilter={threadId} />
    </OverlayShell>
  );
}

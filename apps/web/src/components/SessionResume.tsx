import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "@/components/ui/sonner";
import { api } from "../lib/api";
import { qk } from "../lib/queryKeys";
import type { Run } from "../types";

/** "Continue session" — appears when the run is paused/ended AND its session
 *  volume still exists (30d TTL). The poll only runs while the run is
 *  resumable-at-a-glance, so a live run costs zero requests.
 *
 *  W-B2 gating: the old logic hid the card on completed runs whose volume was
 *  alive but showed it while working (a no-op resume). The honest gate:
 *  interrupted/completed/failed AND resumable. Abandoned never offers it —
 *  abandon shreds the workspace, so there is nothing to continue. */
export function SessionResume({
  run,
  working,
  onResumed,
  onEdit,
}: {
  run: Run;
  working: boolean;
  onResumed: (runId: string) => void;
  onEdit?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const resumableStage = ["interrupted", "completed", "failed"].includes(run.stage);
  const { data } = useQuery({
    queryKey: qk.resumable(run.id),
    queryFn: () => api.get<{ resumable: boolean }>(`/sessions/${run.id}/resumable`),
    enabled: resumableStage,
    staleTime: 30_000,
  });
  const resumable = data?.resumable ?? false;

  if (!resumableStage) return null;

  const resume = async () => {
    if (busy || !resumable) return;
    setBusy(true);
    try {
      // W-B2: the button previously fired the intent and silently swallowed
      // any error — surface it and leave the card armed for a retry.
      await api.post(`/sessions/${run.id}/resume`);
      toast.success("session resumed");
      onResumed(run.id);
    } catch (err) {
      toast.error("couldn't resume", {
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-s3 border-t border-hairline px-s4 py-s3">
      {resumable ? (
        <button
          type="button"
          disabled={busy}
          onClick={() => void resume()}
          className="rounded-md border border-green bg-green-soft px-s3 py-s1.5 font-mono text-[12px] text-ok-bright transition-colors duration-fast hover:border-green disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? "resuming…" : "continue session"}
        </button>
      ) : (
        <span className="rounded-md border border-hairline bg-bg-module px-s3 py-s1.5 font-mono text-[12px] text-ink-faint">
          replay only — session expired
        </span>
      )}
      {run.stage === "interrupted" && onEdit && (
        <button
          type="button"
          disabled={busy}
          onClick={onEdit}
          className="rounded-md border border-hairline bg-bg-module px-s3 py-s1.5 font-mono text-[12px] text-ink-secondary transition-colors duration-fast hover:border-blue-bright hover:text-ink-primary disabled:cursor-not-allowed disabled:opacity-40"
        >
          edit & resend
        </button>
      )}
      <span className="text-[11.5px] text-ink-faint">
        {resumable
          ? working
            ? "picks up where the agent left off"
            : "the agent wakes up with its full context intact"
          : "the transcript is complete; the workspace is gone"}
      </span>
    </div>
  );
}

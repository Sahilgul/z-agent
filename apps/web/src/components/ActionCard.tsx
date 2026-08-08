import { useEffect, useRef, useState } from "react";
import type { RunStage } from "../types";
import { visibleActions } from "../lib/runMachine";
import { cn } from "@/lib/utils";

/** The run's action strip: what `visibleActions` computes — the backend's
 *  `available_actions` PLUS Stop, which is hardcoded (W-B1). The backend's
 *  intent gate keeps stop_run legal on every stage
 *  (backend/app/services/intents.py), i.e. the contract is "the UI always
 *  offers it on non-terminal runs" — the old code rendered server-sent
 *  actions verbatim, so the primary kill switch was unreachable.
 *
 *  Abandon is a two-tap destructive action: the first click arms the button
 *  ("confirm abandon"), the second fires confirmed=true — irreversible
 *  (workspace shred), never confused with the safe, resumable Stop.
 *
 *  Every button disables while an intent is in flight (W3-M6): stop now
 *  blocks on up to 10s of per-thread acks, and a double-fire used to send
 *  duplicate intents. */

const LABELS: Record<string, string> = {
  start_plan: "start plan",
  approve_plan: "approve plan",
  reject_plan: "reject plan",
  create_pr: "create PR",
  merge_pr: "merge",
  review_plan: "review plan",
  review_evidence: "evidence",
  review_diff: "diff",
  resume_run: "resume",
  edit_and_resend: "edit & resend",
  kill_replace: "kill & replace",
  summarize_thread: "summarize",
  ask_counsel: "ask counsel",
  nudge: "nudge",
  let_it_run: "let it run",
  stop_run: "stop",
};

const DISARM_MS = 4_000;

export function ActionCard({
  stage,
  actions,
  onFire,
}: {
  stage: RunStage;
  actions: string[];
  /** @deprecated kept for call-site compatibility; gating is stage-derived. */
  working?: boolean;
  onFire: (intent: string, confirmed?: boolean) => void;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const [armingAbandon, setArmingAbandon] = useState(false);
  const disarmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (disarmTimer.current) clearTimeout(disarmTimer.current);
  }, []);

  const shown = visibleActions(stage, actions ?? []);
  const busy = pending !== null;

  const fire = (intent: string, confirmed = false) => {
    if (busy) return;
    setPending(intent);
    try {
      onFire(intent, confirmed);
    } finally {
      // The store's sendIntent is async fire-and-forget here; the in-flight
      // window only needs to span the optimistic beat — the backend's
      // idempotency and the store's error toast cover the rest.
      setTimeout(() => setPending(null), 1_500);
    }
  };

  const onAbandon = () => {
    if (busy) return;
    if (!armingAbandon) {
      setArmingAbandon(true);
      if (disarmTimer.current) clearTimeout(disarmTimer.current);
      disarmTimer.current = setTimeout(() => setArmingAbandon(false), DISARM_MS);
      return;
    }
    if (disarmTimer.current) clearTimeout(disarmTimer.current);
    setArmingAbandon(false);
    fire("abandon_run", true);
  };

  // The strip hides entirely on terminal stages (visibleActions = []).
  // W4-L3's premise — awaiting_user offers only tool-permission actions —
  // is stale: the backend advertises review/approve/reject plan there
  // (services/runs.py), real decisions that must render.
  if (shown.length === 0) return null;

  const cls =
    "rounded-md border px-s3 py-s1.5 font-mono text-[12px] transition-colors duration-fast disabled:cursor-not-allowed disabled:opacity-40";

  return (
    <div className="flex flex-wrap items-center gap-s2 border-t border-hairline px-s4 py-s3" data-testid="action-strip">
      {shown.map((a) => (
        <button
          key={a}
          type="button"
          disabled={busy}
          onClick={() => fire(a)}
          className={cn(
            cls,
            a === "approve_plan" || a === "create_pr"
              ? "border-green bg-green-soft text-ok-bright hover:border-green"
              : a === "stop_run"
                ? "border-hairline bg-bg-module text-ink-secondary hover:border-warn hover:text-warn-bright"
                : "border-hairline bg-bg-module text-ink-secondary hover:border-blue-bright hover:text-ink-primary",
          )}
          title={a === "stop_run" ? "stop the run — all work preserved, resumable" : undefined}
        >
          {LABELS[a] ?? a.replaceAll("_", " ")}
        </button>
      ))}

      <button
        type="button"
        disabled={busy}
        onClick={onAbandon}
        className={cn(
          cls,
          "ml-auto",
          armingAbandon
            ? "border-red bg-danger-soft text-danger-bright"
            : "border-hairline bg-bg-module text-ink-faint hover:border-red hover:text-danger-bright",
        )}
        title="abandon — kills the run and shreds the workspace; cannot be undone"
      >
        {armingAbandon ? "confirm abandon?" : "abandon"}
      </button>
    </div>
  );
}

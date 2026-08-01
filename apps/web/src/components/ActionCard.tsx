import { useState } from "react";
import { agentWorking } from "../lib/runMachine";
import type { RunStage } from "../types";

/** Action card (§1a): the run's legal moves as buttons. Irreversible intents
 *  are two-tap (tap once -> confirm state, tap again -> fire). While the agent
 *  works, only Stop + the typed nudge box stay live. */
const IRREVERSIBLE = new Set(["merge_pr", "abandon_run", "kill_replace"]);

const LABELS: Record<string, string> = {
  approve_plan: "Approve plan",
  reject_plan: "Reject plan",
  create_pr: "Open PR",
  review_diff: "Review diff",
  merge_pr: "Merge PR",
  stop_run: "Stop",
  abandon_run: "Abandon",
  start_plan: "Turn into a plan",
  review_evidence: "Review evidence",
  stop_lane: "Stop lane",
  kill_replace: "Kill & replace",
  let_it_run: "Let it run",
};

export function ActionCard({
  stage,
  actions,
  working,
  onFire,
}: {
  stage: RunStage;
  actions: string[];
  working?: boolean;
  onFire: (intent: string, confirmed: boolean) => void;
}) {
  const [confirming, setConfirming] = useState<string | null>(null);
  const busy = working ?? agentWorking(stage);

  const fire = (intent: string) => {
    if (IRREVERSIBLE.has(intent) && confirming !== intent) {
      setConfirming(intent);
      return;
    }
    setConfirming(null);
    onFire(intent, IRREVERSIBLE.has(intent));
  };

  return (
    <div className="action-card" data-testid="action-card">
      {actions.map((a) => {
        const isStop = a === "stop_run";
        const hiddenWhileWorking = busy && !isStop;
        if (hiddenWhileWorking) return null;
        const danger = IRREVERSIBLE.has(a) || a === "abandon_run";
        return (
          <button
            key={a}
            className={`btn btn-mono ${danger ? "btn-danger" : isStop ? "btn-ghost" : "btn-primary"}`}
            onClick={() => fire(a)}
            data-intent={a}
          >
            {confirming === a ? `${LABELS[a] ?? a} — confirm?` : LABELS[a] ?? a}
          </button>
        );
      })}
    </div>
  );
}

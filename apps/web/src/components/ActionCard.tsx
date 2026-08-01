import { useState } from "react";
import { Button } from "@/components/ui/button";
import { agentWorking } from "../lib/runMachine";
import type { RunStage } from "../types";

/** Action card: the run's legal moves as buttons. Irreversible intents are
 *  two-tap (tap once -> confirm state, tap again -> fire). While the agent
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
    <div data-testid="action-card" className="flex flex-wrap gap-s2 border-t border-hairline px-s4 py-2.5">
      {actions.map((a) => {
        const isStop = a === "stop_run";
        const hiddenWhileWorking = busy && !isStop;
        if (hiddenWhileWorking) return null;
        const danger = IRREVERSIBLE.has(a) || a === "abandon_run";
        return (
          <Button
            key={a}
            variant={danger ? "destructive" : isStop ? "outline" : "default"}
            size="sm"
            onClick={() => fire(a)}
            data-intent={a}
            className="font-mono"
          >
            {confirming === a ? `${LABELS[a] ?? a} — confirm?` : LABELS[a] ?? a}
          </Button>
        );
      })}
    </div>
  );
}

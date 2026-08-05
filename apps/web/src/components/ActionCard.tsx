import { useState } from "react";
import { Button } from "@/components/ui/button";
import { agentWorking } from "../lib/runMachine";
import type { RunStage } from "../types";

/** Action card: the run's legal moves as buttons. Irreversible intents are
 *  two-tap (tap once -> confirm state, tap again -> fire). While the agent
 *  works, only Stop + the typed nudge box stay live. */
const IRREVERSIBLE = new Set(["merge_pr", "abandon_run", "kill_replace"]);

/** Tool-permission decisions carry an approval_id and are answered on the card
 *  itself (ApprovalQueue). Rendering them here too would fire an intent with no
 *  idea which pending ask it answers. */
const TOOL_PERMISSION = new Set(["allow_once", "always_allow", "deny_tool"]);

const LABELS: Record<string, string> = {
  review_plan: "Review plan",
  approve_plan: "Approve plan",
  reject_plan: "Reject plan",
  create_pr: "Open PR",
  review_diff: "Review diff",
  merge_pr: "Merge PR",
  stop_run: "Stop",
  abandon_run: "Abandon",
  resume_run: "Resume",
  edit_and_resend: "Edit & resend",
  start_plan: "Turn into a plan",
  start_planning: "Start planning",
  move_to_development: "Move to development",
  switch_to_agent_mode: "Switch to agent mode",
  review_evidence: "Review evidence",
  stop_thread: "Stop thread",
  kill_replace: "Kill & replace",
  let_it_run: "Let it run",
  nudge: "Nudge",
  pin_finding: "Pin finding",
  ask_counsel: "Ask counsel",
  summarize_thread: "Summarize thread",
  promote_to_plan: "Promote to plan",
  approve_knowledge: "Approve knowledge",
  dismiss_proposal: "Dismiss",
  accept_proposal: "Accept",
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
      {actions.filter((a) => !TOOL_PERMISSION.has(a)).map((a) => {
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

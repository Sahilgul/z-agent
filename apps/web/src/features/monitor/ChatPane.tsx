import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ActionCard } from "../../components/ActionCard";
import { agentWorking } from "../../lib/runMachine";
import { useRuns } from "../../stores/run";

/** Right pane: talk to the LEAD only — typed nudges and messages stay live
 *  while the agent works; the action card renders the run's legal moves.
 *  Subagents never appear here — they report notebooks. */
export function ChatPane() {
  const { current, sendIntent } = useRuns();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  if (!current) return null;
  const working = agentWorking(current.stage);

  const send = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await sendIntent("send_message", { text: text.trim() });
      setText("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto p-s4">
        {current.auto_summary ? (
          <div className="rounded-r-md border-l-2 border-green bg-bg-module px-s4 py-2.5 text-[13.5px] leading-[1.6] whitespace-pre-wrap">
            <div className="text-micro mb-s1 text-ink-faint">lead</div>
            {current.auto_summary}
          </div>
        ) : (
          <div className="py-s3 font-mono text-[12px] text-ink-faint">
            the lead reports here — ask anything mid-flight
          </div>
        )}
      </div>
      <ActionCard
        stage={current.stage}
        actions={current.available_actions}
        working={working}
        onFire={(intent, confirmed) => void sendIntent(intent, { confirmed })}
      />
      <div className="flex gap-2.5 border-t border-hairline px-s4 py-s3">
        <Textarea
          placeholder={working ? "nudge the lead — it hears you mid-work…" : "message the lead…"}
          value={text}
          rows={2}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          className="flex-1 resize-none"
        />
        <Button disabled={busy || !text.trim()} onClick={() => void send()}>
          send
        </Button>
      </div>
    </div>
  );
}

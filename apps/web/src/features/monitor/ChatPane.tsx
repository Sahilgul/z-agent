import { useState } from "react";
import { ActionCard } from "../../components/ActionCard";
import { agentWorking } from "../../lib/runMachine";
import { useRuns } from "../../stores/run";

/** Right pane (§1): talk to the LEAD only — typed nudges and messages stay
 *  live while the agent works (§1a carve-out); the action card renders the
 *  run's legal moves. Subagents never appear here — they report notebooks. */
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
    <div className="chat-pane">
      <div className="chat-scroll">
        {current.auto_summary ? (
          <div className="chat-msg lead">
            <div className="mono faint chat-tag">lead</div>
            {current.auto_summary}
          </div>
        ) : (
          <div className="faint mono chat-empty">the lead reports here — ask anything mid-flight</div>
        )}
      </div>
      <ActionCard
        stage={current.stage}
        actions={current.available_actions}
        working={working}
        onFire={(intent, confirmed) => void sendIntent(intent, { confirmed })}
      />
      <div className="chat-input-row">
        <textarea
          className="chat-input"
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
        />
        <button className="btn btn-primary" disabled={busy || !text.trim()} onClick={() => void send()}>
          send
        </button>
      </div>
      <style>{`
        .chat-pane { display: flex; flex-direction: column; height: 100%; }
        .chat-scroll { flex: 1; overflow-y: auto; padding: 14px; }
        .chat-empty { font-size: 12px; padding: 12px 0; }
        .chat-msg.lead {
          background: var(--bg-module); border-left: 2px solid var(--green);
          border-radius: 0 var(--radius) var(--radius) 0; padding: 10px 14px;
          font-size: 13.5px; line-height: 1.6; white-space: pre-wrap;
        }
        .chat-tag { font-size: 10px; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 5px; }
        .action-card { display: flex; flex-wrap: wrap; gap: 8px; padding: 10px 14px; border-top: 1px solid var(--hairline); }
        .chat-input-row { display: flex; gap: 10px; padding: 12px 14px; border-top: 1px solid var(--hairline); }
        .chat-input {
          flex: 1; background: var(--jack); border: 1px solid var(--hairline); border-radius: var(--radius);
          color: var(--ink-primary); padding: 10px 12px; font-family: var(--font-ui); font-size: 13.5px; resize: none;
        }
      `}</style>
    </div>
  );
}

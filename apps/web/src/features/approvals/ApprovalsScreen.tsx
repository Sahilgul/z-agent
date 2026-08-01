import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";
import { hasSubscription, pushSupported, subscribeToPush } from "../../lib/push";
import type { Approval } from "../../types";

/** Push opt-in (plan Phase 4): the ask appears ONLY here — after the user's
 *  first AwaitingYou moment — never on landing. */
function PushOptIn() {
  const [state, setState] = useState<"hidden" | "ask" | "done">("hidden");
  useEffect(() => {
    if (!pushSupported()) return;
    void hasSubscription().then((subbed) => setState(subbed ? "hidden" : "ask"));
  }, []);
  if (state !== "ask") return null;
  return (
    <div className="push-ask" data-testid="push-ask">
      <span className="push-ask-text">
        approve from your phone — notifications deep-link straight to each card
      </span>
      <button
        className="btn btn-mono btn-primary"
        onClick={() => void subscribeToPush().then((ok) => setState(ok ? "done" : "hidden"))}
      >
        enable push
      </button>
      <button className="btn btn-mono btn-ghost" onClick={() => setState("hidden")}>
        not now
      </button>
    </div>
  );
}

/** Approval inbox (§1b/§4 gated autonomy): pending tool-permission asks across
 *  the user's runs — allow once / deny. */
export function ApprovalsScreen() {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const load = useCallback(() => {
    api.get<Approval[]>("/approvals").then(setApprovals).catch(() => setApprovals([]));
  }, []);

  useEffect(load, [load]);

  const decide = async (id: string, decision: "allow_once" | "deny") => {
    await api.post(`/approvals/${id}/decide`, { decision });
    load();
  };

  return (
    <div className="approvals">
      <h2 className="ap-head">approval inbox</h2>
      {approvals.length > 0 && <PushOptIn />}
      {approvals.length === 0 && <div className="faint mono">nothing waiting on you</div>}
      {approvals.map((a) => (
        <div key={a.id} className="ap-card">
          <div className="mono faint ap-meta">run {a.run_id.slice(0, 8)} · lane {a.lane_id.slice(0, 8)}</div>
          <div className="ap-tool mono">{a.tool}</div>
          <pre className="ap-input">{JSON.stringify(a.input, null, 2).slice(0, 600)}</pre>
          <div className="ap-actions">
            <button className="btn btn-mono btn-primary" onClick={() => void decide(a.id, "allow_once")}>
              allow once
            </button>
            <button className="btn btn-mono btn-danger" onClick={() => void decide(a.id, "deny")}>
              deny
            </button>
          </div>
        </div>
      ))}
      <style>{`
        .approvals { max-width: 720px; margin: 0 auto; padding: 22px 18px; }
        .ap-head { font-family: var(--font-display); font-weight: 500; }
        .ap-card { background: var(--bg-panel); border: 1px solid var(--hairline); border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; }
        .ap-meta { font-size: 10.5px; margin-bottom: 6px; }
        .ap-tool { font-size: 13px; color: var(--blue-bright); margin-bottom: 8px; }
        .ap-input { background: var(--jack); border-radius: var(--radius); padding: 10px 12px; font-size: 11px; overflow-x: auto; max-height: 180px; }
        .ap-actions { display: flex; gap: 8px; margin-top: 10px; }
        .push-ask { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; background: var(--bg-panel); border: 1px solid var(--hairline); border-radius: 8px; padding: 12px 14px; margin-bottom: 14px; }
        .push-ask-text { flex: 1; min-width: 220px; font-size: 12px; color: var(--ink-secondary); }
      `}</style>
    </div>
  );
}

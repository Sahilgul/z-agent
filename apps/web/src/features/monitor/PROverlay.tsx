import { useEffect, useState } from "react";
import { Markdown } from "../../components/Markdown";
import { OverlayShell } from "../../components/OverlayShell";
import { api } from "../../lib/api";
import { useRuns } from "../../stores/run";

interface EvidencePackage {
  plan_title: string;
  plan_steps: { index: number; title: string; status: string }[];
  lanes: { persona: string; status: string; cost_usd: number }[];
  test_signals: { title: string; ts: string }[];
  total_cost_usd: number;
  total_tokens: number;
  sha256?: string;
  trajectory?: string;
}

/** PR overlay (§9): the tamper-proof evidence package + merge button — the
 *  human's approval of record happens here (or hands off to ADO native UI). */
export function PROverlay() {
  const { current, sendIntent } = useRuns();
  const [pkg, setPkg] = useState<EvidencePackage | null>(null);
  const [handoff, setHandoff] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!current) return;
    api
      .get<EvidencePackage>(`/runs/${current.id}/evidence`)
      .then(setPkg)
      .catch(() => setPkg(null)); // no PR yet — the card explains itself
  }, [current]);

  const merge = async () => {
    setBusy(true);
    setError("");
    try {
      const res = await sendIntent("merge_pr", { confirmed: true });
      if (typeof res.handoff_url === "string" && res.handoff_url) {
        setHandoff(res.handoff_url);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "merge failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <OverlayShell title="pull request · evidence package">
      {!pkg && (
        <div className="faint mono">no evidence package yet — the PR opens after the evaluator passes.</div>
      )}
      {pkg && (
        <div>
          <h3 className="pr-title">{pkg.plan_title || "evidence package"}</h3>
          <div className="mono faint pr-hash">sha256 {pkg.sha256 ?? "—"}</div>
          <div className="pr-grid mono">
            <div><b>{pkg.plan_steps.length}</b> plan steps</div>
            <div><b>{pkg.lanes.length}</b> lanes</div>
            <div><b>{pkg.test_signals.length}</b> test signals</div>
            <div><b>${pkg.total_cost_usd.toFixed(2)}</b> · {pkg.total_tokens} tokens</div>
          </div>
          {pkg.trajectory && (
            <div className="pr-traj">
              <div className="mono faint section-tag">trajectory</div>
              <Markdown>{pkg.trajectory}</Markdown>
            </div>
          )}
          {handoff ? (
            <a className="btn btn-primary" href={handoff} target="_blank" rel="noreferrer">
              complete the merge in ADO →
            </a>
          ) : (
            <button className="btn btn-primary" disabled={busy} onClick={() => void merge()}>
              {busy ? "merging…" : "approve & merge — my identity is the record"}
            </button>
          )}
          {error && <div className="pr-error">{error}</div>}
        </div>
      )}
      <style>{`
        .pr-title { font-family: var(--font-display); font-weight: 500; margin: 0 0 6px; }
        .pr-hash { font-size: 10.5px; margin-bottom: 14px; word-break: break-all; }
        .pr-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; background: var(--bg-module); border: 1px solid var(--hairline); border-radius: var(--radius); padding: 14px; font-size: 12px; margin-bottom: 16px; }
        .pr-traj { margin-bottom: 16px; }
        .section-tag { font-size: 10.5px; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 8px; }
        .pr-error { color: var(--danger); font-size: 12.5px; margin-top: 10px; }
      `}</style>
    </OverlayShell>
  );
}

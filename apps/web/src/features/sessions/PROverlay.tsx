import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Markdown } from "../../components/Markdown";
import { OverlayShell } from "../../components/OverlayShell";
import { api } from "../../lib/api";
import { qk } from "../../lib/queryKeys";
import { useRuns } from "../../stores/run";

interface EvidencePackage {
  plan_title: string;
  plan_steps: { index: number; title: string; status: string }[];
  threads: { persona: string; status: string; cost_usd: number }[];
  test_signals: { title: string; ts: string }[];
  total_cost_usd: number;
  total_tokens: number;
  sha256?: string;
  trajectory?: string;
}

/** PR overlay: the tamper-proof evidence package + merge button — the
 *  human's approval of record happens here (or hands off to ADO native UI). */
export function PROverlay() {
  const { current, sendIntent } = useRuns();
  const [handoff, setHandoff] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const { data: pkg } = useQuery({
    queryKey: qk.pr(current?.id ?? ""),
    queryFn: () => api.get<EvidencePackage>(`/runs/${current!.id}/evidence`),
    enabled: !!current,
    retry: false,
  });

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
        <div className="font-mono text-[12px] text-ink-faint">
          no evidence package yet — the PR opens after the evaluator passes.
        </div>
      )}
      {pkg && (
        <div>
          <h3 className="mb-s1 font-display text-[19px] font-medium">{pkg.plan_title || "evidence package"}</h3>
          <div className="mb-s4 break-all font-mono text-[10.5px] text-ink-faint">sha256 {pkg.sha256 ?? "—"}</div>
          <div className="mb-s4 grid grid-cols-2 gap-2.5 rounded-md border border-hairline bg-bg-module p-s4 font-mono text-[12px]">
            <div>
              <b className="tabular">{pkg.plan_steps.length}</b> plan steps
            </div>
            <div>
              <b className="tabular">{pkg.threads.length}</b> threads
            </div>
            <div>
              <b className="tabular">{pkg.test_signals.length}</b> test signals
            </div>
            <div>
              <b className="tabular">${pkg.total_cost_usd.toFixed(2)}</b> · {pkg.total_tokens} tokens
            </div>
          </div>
          {pkg.trajectory && (
            <div className="mb-s4">
              <div className="text-micro mb-s2 text-ink-faint">trajectory</div>
              <Markdown>{pkg.trajectory}</Markdown>
            </div>
          )}
          {handoff ? (
            <Button render={<a href={handoff} target="_blank" rel="noreferrer" />}>complete the merge in ADO →</Button>
          ) : (
            <Button disabled={busy} onClick={() => void merge()}>
              {busy ? "merging…" : "approve & merge — my identity is the record"}
            </Button>
          )}
          {error && <div className="mt-2.5 text-[12.5px] text-danger-bright">{error}</div>}
        </div>
      )}
    </OverlayShell>
  );
}

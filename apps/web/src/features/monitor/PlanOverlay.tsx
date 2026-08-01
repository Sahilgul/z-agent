import { useEffect, useState } from "react";
import { Markdown } from "../../components/Markdown";
import { OverlayShell } from "../../components/OverlayShell";
import { api } from "../../lib/api";
import { useRuns } from "../../stores/run";
import type { PlanPayload } from "../../types";

/** Plan overlay (§2): the draft Plan rendered — steps table with per-step
 *  status, critic notes, drifted-citation flags. Approve/reject rides the
 *  action card below; this overlay is the EVIDENCE for that decision. */
export function PlanOverlay() {
  const current = useRuns((s) => s.current);
  const [plan, setPlan] = useState<PlanPayload | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!current) return;
    api
      .get<PlanPayload>(`/runs/${current.id}/plan`)
      .then(setPlan)
      .catch((e) => setError(e instanceof Error ? e.message : "no plan"));
  }, [current]);

  return (
    <OverlayShell title={`plan · ${plan?.status ?? "loading"}`}>
      {error && <div className="faint mono">{error}</div>}
      {plan && (
        <div>
          <h3 className="plan-title">{plan.structured.title ?? "untitled plan"}</h3>
          <table className="plan-steps mono">
            <thead>
              <tr><th>#</th><th>step</th><th>repo</th><th>files</th><th>status</th></tr>
            </thead>
            <tbody>
              {plan.steps.map((s) => (
                <tr key={s.id}>
                  <td>{s.index}</td>
                  <td>
                    <div>{s.title}</div>
                    <div className="faint">{s.success_criterion}</div>
                  </td>
                  <td>{s.repo ?? "—"}</td>
                  <td className="faint">{s.files.join(", ") || "—"}</td>
                  <td>{s.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {(plan.structured.critic_notes ?? []).length > 0 && (
            <div className="critic">
              <div className="mono faint section-tag">critic notes</div>
              <Markdown>{(plan.structured.critic_notes ?? []).map((n) => `- ${n}`).join("\n")}</Markdown>
            </div>
          )}
        </div>
      )}
      <style>{`
        .plan-title { font-family: var(--font-display); font-weight: 500; margin: 0 0 14px; }
        .plan-steps { width: 100%; border-collapse: collapse; font-size: 12px; }
        .plan-steps th { text-align: left; color: var(--ink-secondary); text-transform: uppercase; font-size: 10px; letter-spacing: .07em; border-bottom: 1px solid var(--hairline); padding: 6px 8px; }
        .plan-steps td { border-bottom: 1px solid var(--hairline); padding: 8px; vertical-align: top; }
        .critic { margin-top: 18px; }
        .section-tag { font-size: 10.5px; text-transform: uppercase; letter-spacing: .08em; margin-bottom: 8px; }
      `}</style>
    </OverlayShell>
  );
}

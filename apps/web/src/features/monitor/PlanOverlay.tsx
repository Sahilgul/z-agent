import { useQuery } from "@tanstack/react-query";
import { DataTable, type Column } from "@/components/ui/data-table";
import { Tag, type TagTone } from "@/components/ui/tag";
import { Markdown } from "../../components/Markdown";
import { OverlayShell } from "../../components/OverlayShell";
import { api } from "../../lib/api";
import { qk } from "../../lib/queryKeys";
import { useRuns } from "../../stores/run";
import type { PlanPayload, PlanStep } from "../../types";

/** Plan overlay: the draft Plan rendered — steps ledger with per-step
 *  status, critic notes, drifted-citation flags. Approve/reject rides the
 *  action card; this overlay is the EVIDENCE for that decision. */
const STATUS_TONE: Record<string, TagTone> = {
  completed: "ok",
  done: "ok",
  failed: "danger",
  in_progress: "info",
  running: "info",
};

const COLUMNS: Column<PlanStep>[] = [
  { key: "index", header: "#", numeric: true, render: (s) => s.index },
  {
    key: "step",
    header: "step",
    className: "whitespace-normal",
    render: (s) => (
      <div className="py-s1">
        <div className="text-ink-primary">{s.title}</div>
        <div className="text-[12px] text-ink-faint">{s.success_criterion}</div>
      </div>
    ),
  },
  { key: "repo", header: "repo", render: (s) => s.repo ?? "—" },
  {
    key: "files",
    header: "files",
    className: "max-w-[280px] truncate",
    render: (s) => <span className="font-mono text-[11.5px] text-ink-faint">{s.files.join(", ") || "—"}</span>,
  },
  { key: "status", header: "status", render: (s) => <Tag tone={STATUS_TONE[s.status] ?? "neutral"}>{s.status}</Tag> },
];

export function PlanOverlay() {
  const current = useRuns((s) => s.current);
  const { data: plan, isLoading, isError, error } = useQuery({
    queryKey: qk.plan(current?.id ?? ""),
    queryFn: () => api.get<PlanPayload>(`/runs/${current!.id}/plan`),
    enabled: !!current,
    retry: false,
  });

  return (
    <OverlayShell title={`plan · ${plan?.status ?? "loading"}`}>
      {isError && <div className="font-mono text-[12px] text-ink-faint">{error instanceof Error ? error.message : "no plan"}</div>}
      {(isLoading || plan) && (
        <div>
          {plan && <h3 className="mb-s4 font-display text-[19px] font-medium">{plan.structured.title ?? "untitled plan"}</h3>}
          <DataTable
            columns={COLUMNS}
            rows={plan?.steps ?? []}
            rowKey={(s) => s.id}
            loading={isLoading}
            skeletonRows={4}
          />
          {(plan?.structured.critic_notes ?? []).length > 0 && (
            <div className="mt-s5">
              <div className="text-micro mb-s2 text-ink-faint">critic notes</div>
              <Markdown>{(plan?.structured.critic_notes ?? []).map((n) => `- ${n}`).join("\n")}</Markdown>
            </div>
          )}
        </div>
      )}
    </OverlayShell>
  );
}

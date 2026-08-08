import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { DataTable, type Column } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { FilterChips } from "@/components/ui/filter-chips";
import { PageHead } from "@/components/ui/page-head";
import { Skeleton } from "@/components/ui/skeleton";
import { Tag } from "@/components/ui/tag";
import { api } from "../../lib/api";
import { qk } from "../../lib/queryKeys";

interface Bucket {
  cost_usd: number;
  tokens: number;
  runs: number;
}

interface Dashboard {
  days: number;
  total: Bucket;
  by_day: Record<string, Bucket>;
  by_mode: Record<string, Bucket>;
  by_repo: Record<string, Bucket>;
  by_user: Record<string, Bucket>;
}

interface Delivery {
  id: number;
  title: string;
  runs: number;
  stages: Record<string, number>;
  cost_usd: number;
  prs: { repo: string; ado_pr_id: number | null; status: string }[];
}

interface LedgerRow {
  name: string;
  bucket: Bucket;
  share: number;
}

const DAY_OPTIONS = [
  { value: "7", label: "7d" },
  { value: "30", label: "30d" },
  { value: "90", label: "90d" },
] as const;

/** Ledger table with the bar INSIDE the row (costs signature) — the
 *  proportional bar aligns with the cost column, not a separate chart. */
function ledgerColumns(totalCost: number): Column<LedgerRow>[] {
  return [
    {
      key: "name",
      header: "name",
      sortable: true,
      sortAccessor: (r) => r.name,
      render: (r) => <span className="font-mono text-[12px] text-ink-primary">{r.name}</span>,
    },
    { key: "runs", header: "runs", numeric: true, sortable: true, sortAccessor: (r) => r.bucket.runs, render: (r) => r.bucket.runs },
    { key: "cost", header: "cost", numeric: true, sortable: true, sortAccessor: (r) => r.bucket.cost_usd, render: (r) => `$${r.bucket.cost_usd.toFixed(2)}` },
    {
      key: "share",
      header: "share",
      className: "w-[40%]",
      render: (r) => (
        <span className="flex items-center gap-s2">
          <span className="h-2 flex-1 overflow-hidden rounded-sm bg-jack" aria-hidden="true">
            <span
              className="block h-full rounded-sm bg-blue-bright"
              style={{ width: `${totalCost > 0 ? (r.bucket.cost_usd / totalCost) * 100 : 0}%` }}
            />
          </span>
          <span className="w-10 text-right font-mono text-[10.5px] tabular text-ink-faint">
            {totalCost > 0 ? Math.round((r.bucket.cost_usd / totalCost) * 100) : 0}%
          </span>
        </span>
      ),
    },
  ];
}

function Ledger({ title, buckets, loading }: { title: string; buckets?: Record<string, Bucket>; loading: boolean }) {
  const entries = Object.entries(buckets ?? {}).sort((a, b) => b[1].cost_usd - a[1].cost_usd);
  const total = entries.reduce((acc, [, b]) => acc + b.cost_usd, 0);
  const rows: LedgerRow[] = entries.map(([name, bucket]) => ({ name, bucket, share: 0 }));
  return (
    <section className="mb-s6" aria-label={title}>
      <h3 className="text-micro mb-s3 text-ink-faint">{title}</h3>
      <DataTable
        columns={ledgerColumns(total)}
        rows={rows}
        rowKey={(r) => r.name}
        loading={loading}
        skeletonRows={3}
        empty={<EmptyState hint={`no ${title} data in this window`} />}
        rowTestId={(r) => `bar-${title}-${r.name}`}
      />
    </section>
  );
}

export function DashboardScreen() {
  const [days, setDays] = useState("30");

  // W8-L3: cost now settles on EVERY terminal path, so the dashboard is
  // truthful-ish but only for the mount-time fetch. Poll so a run finishing
  // while the tab sits open shows up without a manual refresh.
  const stats = useQuery({
    queryKey: qk.costStats(Number(days)),
    queryFn: () => api.get<Dashboard>(`/stats/cost?days=${days}`),
    placeholderData: (prev) => prev,
    refetchInterval: 30_000,
  });
  const deliveries = useQuery({
    queryKey: qk.deliveries,
    queryFn: () => api.get<{ items: Delivery[] }>("/deliveries"),
    refetchInterval: 30_000,
  });

  const dash = stats.data;
  const perRun = dash && dash.total.runs > 0 ? dash.total.cost_usd / dash.total.runs : 0;
  const topRepo = dash
    ? Object.entries(dash.by_repo).sort((a, b) => b[1].cost_usd - a[1].cost_usd)[0]?.[0] ?? "—"
    : "—";

  return (
    <div className="mx-auto h-full max-w-canvas overflow-y-auto px-s8 py-s6">
      <PageHead
        title="costs & campaigns"
        sub="gateway metering — metadata only, never content"
        actions={<FilterChips options={[...DAY_OPTIONS]} value={days} onChange={setDays} />}
      />

      <div className="mb-s6 grid grid-cols-2 gap-s3 md:grid-cols-4">
        {[
          { label: "total spend", value: dash ? `$${dash.total.cost_usd.toFixed(2)}` : null, testId: "dash-total", accent: true },
          { label: "runs", value: dash ? String(dash.total.runs) : null },
          { label: "$ / run", value: dash ? `$${perRun.toFixed(2)}` : null },
          { label: "top repo", value: dash ? topRepo : null },
        ].map((s) => (
          <div key={s.label} className="rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card">
            <div className="text-micro mb-s2 text-ink-faint">{s.label}</div>
            {s.value === null ? (
              <Skeleton className="h-6 w-24 rounded-sm" />
            ) : (
              <div
                data-testid={s.testId}
                className={`font-mono text-[22px] tabular ${s.accent ? "text-ok-bright" : "text-ink-primary"}`}
              >
                {s.value}
              </div>
            )}
          </div>
        ))}
      </div>

      <Ledger title="by mode" buckets={dash?.by_mode} loading={stats.isLoading} />
      <Ledger title="by repo" buckets={dash?.by_repo} loading={stats.isLoading} />
      <Ledger title="by teammate" buckets={dash?.by_user} loading={stats.isLoading} />

      <section aria-label="campaigns">
        <h3 className="text-micro mb-s3 text-ink-faint">campaigns</h3>
        {deliveries.isLoading ? (
          <div className="flex flex-col gap-s3">
            {[0, 1].map((i) => (
              <div key={i} className="rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card">
                <Skeleton className="mb-s2 h-4 w-1/3 rounded-sm" />
                <Skeleton className="h-3 w-2/3 rounded-sm" />
              </div>
            ))}
          </div>
        ) : (deliveries.data?.items ?? []).length === 0 ? (
          <EmptyState hint="no fleet campaigns yet" />
        ) : (
          (deliveries.data?.items ?? []).map((d) => (
            <article
              key={d.id}
              data-testid={`delivery-${d.id}`}
              className="mb-s3 rounded-lg border border-hairline bg-bg-panel p-s4 shadow-card"
            >
              <header className="mb-s2 flex items-center justify-between gap-s3">
                <strong className="text-[15px] font-semibold">{d.title}</strong>
                <span className="font-mono text-[12px] tabular text-ink-primary">${d.cost_usd.toFixed(2)}</span>
              </header>
              <div className="flex flex-wrap gap-s2">
                {Object.entries(d.stages).map(([stage, n]) => (
                  <Tag key={stage} tone={stage === "completed" ? "ok" : stage === "failed" ? "danger" : "info"}>
                    {stage} ×{n}
                  </Tag>
                ))}
                {d.prs.map((p) => (
                  <Tag key={`${p.repo}-${p.ado_pr_id}`}>
                    {p.repo} PR {p.ado_pr_id ?? "?"} · {p.status}
                  </Tag>
                ))}
              </div>
            </article>
          ))
        )}
      </section>
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";

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

function Bars({ title, buckets }: { title: string; buckets: Record<string, Bucket> }) {
  const entries = Object.entries(buckets).sort((a, b) => b[1].cost_usd - a[1].cost_usd);
  const max = Math.max(1, ...entries.map(([, b]) => b.cost_usd));
  if (entries.length === 0) return null;
  return (
    <section className="dash-block">
      <h3 className="dash-h">{title}</h3>
      {entries.map(([name, b]) => (
        <div className="dash-row" key={name} data-testid={`bar-${title}-${name}`}>
          <span className="dash-label mono">{name}</span>
          <span className="dash-track">
            <span className="dash-fill" style={{ width: `${(b.cost_usd / max) * 100}%` }} />
          </span>
          <span className="dash-num mono">${b.cost_usd.toFixed(2)} · {b.runs} runs</span>
        </div>
      ))}
    </section>
  );
}

export function DashboardScreen() {
  const [dash, setDash] = useState<Dashboard | null>(null);
  const [deliveries, setDeliveries] = useState<Delivery[]>([]);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const [d, del] = await Promise.all([
        api.get<Dashboard>("/stats/cost?days=30"),
        api.get<{ items: Delivery[] }>("/deliveries"),
      ]);
      setDash(d);
      setDeliveries(del.items);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to load");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="dashboard">
      <header className="screen-head">
        <h1>costs &amp; campaigns</h1>
        <p className="muted">30-day gateway metering — metadata only, never content</p>
      </header>
      {err && <div className="err">{err}</div>}
      {dash && (
        <>
          <div className="dash-total" data-testid="dash-total">
            <span className="dash-big mono">${dash.total.cost_usd.toFixed(2)}</span>
            <span className="muted">{dash.total.tokens.toLocaleString()} tokens · {dash.total.runs} runs</span>
          </div>
          <Bars title="by mode" buckets={dash.by_mode} />
          <Bars title="by repo" buckets={dash.by_repo} />
          <Bars title="by teammate" buckets={dash.by_user} />
        </>
      )}
      <section className="dash-block">
        <h3 className="dash-h">campaigns</h3>
        {deliveries.length === 0 && <p className="muted">no fleet campaigns yet</p>}
        {deliveries.map((d) => (
          <div className="card" key={d.id} data-testid={`delivery-${d.id}`}>
            <header className="card-head">
              <strong className="card-title">{d.title}</strong>
              <span className="chip mono">${d.cost_usd.toFixed(2)}</span>
            </header>
            <div className="card-body meta-row">
              {Object.entries(d.stages).map(([stage, n]) => (
                <span className="tag mono" key={stage}>{stage} ×{n}</span>
              ))}
              {d.prs.map((p) => (
                <span className="chip mono" key={`${p.repo}-${p.ado_pr_id}`}>
                  {p.repo} PR {p.ado_pr_id ?? "?"} · {p.status}
                </span>
              ))}
            </div>
          </div>
        ))}
      </section>
      <style>{`
        .dashboard { max-width: 780px; margin: 0 auto; padding: 22px 18px; overflow-y: auto; height: 100%; }
        .dash-total { display: flex; align-items: baseline; gap: 14px; margin-bottom: 18px; }
        .dash-big { font-size: 28px; color: var(--green-bright); }
        .dash-block { margin-bottom: 20px; }
        .dash-h { font-family: var(--font-display); font-weight: 500; font-size: 14px; margin-bottom: 8px; }
        .dash-row { display: flex; align-items: center; gap: 10px; margin-bottom: 5px; }
        .dash-label { width: 140px; font-size: 11px; color: var(--ink-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .dash-track { flex: 1; height: 8px; background: var(--jack); border-radius: 4px; overflow: hidden; }
        .dash-fill { display: block; height: 100%; background: var(--blue-bright); border-radius: 4px; }
        .dash-num { width: 140px; text-align: right; font-size: 10.5px; color: var(--ink-faint); }
      `}</style>
    </section>
  );
}

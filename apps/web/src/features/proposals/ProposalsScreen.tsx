import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";

export interface Proposal {
  id: number;
  source: "janitor" | "perfector";
  repo: string | null;
  title: string;
  body: string;
  evidence: string[];
  impact: string;
  confidence: string;
  rank_score: number;
  status: string;
  promoted_run_id: string | null;
  created_at: string | null;
}

function levelClass(level: string) {
  return level === "high" ? "st-failed" : level === "medium" ? "st-running" : "st-queued";
}

function ProposalCard({ item, onDecided }: { item: Proposal; onDecided: () => void }) {
  const [err, setErr] = useState("");
  const decided = item.status !== "proposed";
  async function accept() {
    setErr("");
    try {
      await api.post(`/proposals/${item.id}/accept`, {});
      onDecided();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "accept failed");
    }
  }
  async function dismiss() {
    setErr("");
    try {
      await api.post(`/proposals/${item.id}/dismiss`, {});
      onDecided();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "dismiss failed");
    }
  }
  return (
    <article className="card" data-testid={`proposal-${item.id}`}>
      <header className="card-head">
        <span className={`tag mono ${item.source === "janitor" ? "st-running" : "st-awaiting"}`}>
          {item.source}
        </span>
        <strong className="card-title">{item.title}</strong>
        <span className="chip mono">score {item.rank_score}</span>
      </header>
      <div className="card-body">
        <p className="muted">{item.body}</p>
        <div className="meta-row">
          <span className={`tag mono ${levelClass(item.impact)}`}>impact {item.impact}</span>
          <span className={`tag mono ${levelClass(item.confidence)}`}>confidence {item.confidence}</span>
          {item.repo && <span className="chip mono">{item.repo}</span>}
        </div>
        <ul className="evidence-list mono">
          {item.evidence.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      </div>
      {!decided ? (
        <footer className="card-foot">
          <button className="btn btn-primary" onClick={accept}>accept</button>
          <button className="btn btn-danger" onClick={dismiss}>dismiss</button>
          {err && <span className="err mono">{err}</span>}
        </footer>
      ) : (
        <footer className="card-foot">
          <span className="tag mono">{item.status}</span>
          {item.promoted_run_id && <span className="chip mono">run {item.promoted_run_id.slice(0, 8)}</span>}
        </footer>
      )}
    </article>
  );
}

export function ProposalsScreen() {
  const [items, setItems] = useState<Proposal[]>([]);
  const [showAll, setShowAll] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const url = showAll ? "/proposals?status=" : "/proposals";
      const data = await api.get<{ items: Proposal[] }>(url);
      setItems(data.items);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to load");
    }
  }, [showAll]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="patrol-screen">
      <header className="pt-head">
        <h1 className="pt-title">patrol</h1>
        <p className="pt-sub">
          ranked proposals from the janitor and perfector patrols — accept promotes
          to a development run, dismiss teaches the flywheel
        </p>
        <span className="pt-knobs"><span className="knob" /><span className="knob" /></span>
      </header>
      <div className="pt-toolbar">
        <label className="mono pt-toggle">
          <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
          {" "}show decided
        </label>
      </div>
      {err && <div className="pt-err">{err}</div>}
      <div className="pt-list">
        {items.map((item) => (
          <ProposalCard key={item.id} item={item} onDecided={load} />
        ))}
        {items.length === 0 && (
          <div className="pt-empty">
            <span className="pt-empty-glyph">⌁</span>
            <div className="mono">no proposals — the patrols run on schedule</div>
          </div>
        )}
      </div>
      <style>{`
        .patrol-screen { max-width: 940px; margin: 0 auto; padding: 26px 28px; height: 100%; overflow-y: auto; }
        .pt-head { position: relative; margin-bottom: 18px; }
        .pt-title { font-family: var(--font-display); font-size: 26px; font-weight: 600; margin: 0; }
        .pt-sub { color: var(--ink-secondary); font-size: 14px; margin: 6px 0 0; max-width: 640px; }
        .pt-knobs { position: absolute; right: 0; top: 6px; display: flex; gap: 10px; }
        .knob { width: 24px; height: 24px; border-radius: 50%; background: radial-gradient(circle at 35% 30%, var(--bg-module), var(--jack) 75%); border: 1px solid var(--hairline); box-shadow: inset 0 2px 4px rgba(0,0,0,.55), 0 1px 0 rgba(255,255,255,.05); position: relative; }
        .knob::after { content: ""; position: absolute; left: 50%; top: 3px; width: 2px; height: 7px; background: var(--ink-faint); border-radius: 1px; transform: translateX(-50%) rotate(24deg); transform-origin: bottom center; }
        .pt-toolbar { margin-bottom: 16px; }
        .pt-toggle { font-size: 12px; color: var(--ink-secondary); display: inline-flex; align-items: center; gap: 7px; cursor: pointer; }
        .pt-err { color: var(--danger); font-size: 13px; margin-bottom: 12px; }
        .pt-list { display: flex; flex-direction: column; }
        .pt-empty { border: 1px dashed var(--hairline); border-radius: 12px; padding: 48px 20px; text-align: center; color: var(--ink-faint); font-size: 12.5px; }
        .pt-empty-glyph { font-family: var(--font-display); font-size: 34px; display: block; margin-bottom: 10px; }
        .patrol-screen .card { background: var(--bg-panel); border: 1px solid var(--hairline); border-radius: 12px; padding: 18px 20px; margin-bottom: 14px; box-shadow: 0 12px 32px rgba(0,0,0,.24); }
        .patrol-screen .card-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
        .patrol-screen .card-title { font-size: 15.5px; font-weight: 600; flex: 1; min-width: 200px; }
        .patrol-screen .card-body p { font-size: 14px; line-height: 1.6; color: var(--ink-secondary); margin: 0 0 12px; }
        .patrol-screen .meta-row { display: flex; gap: 9px; flex-wrap: wrap; margin-bottom: 10px; }
        .patrol-screen .tag { font-size: 11px; padding: 4px 10px; border-radius: 6px; border: 1px solid var(--hairline); background: var(--bg-module); }
        .patrol-screen .chip { font-size: 11px; padding: 4px 10px; border-radius: 6px; border: 1px dashed var(--hairline); color: var(--ink-faint); }
        .patrol-screen .st-running { color: var(--blue-bright); border-color: color-mix(in srgb, var(--blue-bright) 40%, var(--hairline)); }
        .patrol-screen .st-awaiting { color: #D9B36C; border-color: color-mix(in srgb, #D9B36C 40%, var(--hairline)); }
        .patrol-screen .st-failed { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, var(--hairline)); }
        .patrol-screen .st-queued { color: var(--ink-faint); }
        .patrol-screen .evidence-list { margin: 0; padding-left: 18px; font-size: 12px; color: var(--ink-faint); line-height: 1.7; }
        .patrol-screen .card-foot { display: flex; align-items: center; gap: 10px; margin-top: 14px; border-top: 1px solid var(--hairline); padding-top: 14px; }
        .patrol-screen .card-foot .btn { padding: 10px 20px; font-size: 13.5px; }
        .patrol-screen .err { color: var(--danger); font-size: 12px; }
      `}</style>
    </section>
  );
}

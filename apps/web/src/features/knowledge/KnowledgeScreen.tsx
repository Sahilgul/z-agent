import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";

/** Knowledge flywheel (§3/§7): the shared corpus + the team-wide draft inbox.
 *  Drafts are born scope=user (the PHI checkpoint — the API enforces it);
 *  approving is where a private episode becomes a shared fact. */

export interface KnowledgeItem {
  id: number;
  content: string;
  trigger_description: string;
  scope: "global" | "repo" | "user";
  repo: string | null;
  status: "draft" | "approved" | "rejected";
  created_by: number | null;
  source_run_id: string | null;
  created_at: string | null;
}

type ScopeChoice = "global" | "repo" | "user";

function DraftCard({ item, onDecided }: { item: KnowledgeItem; onDecided: () => void }) {
  const [scope, setScope] = useState<ScopeChoice>("global");
  const [repo, setRepo] = useState(item.repo ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const approve = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.post(`/knowledge/${item.id}/approve`, {
        scope,
        repo: scope === "repo" ? repo || null : null,
      });
      onDecided();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "approve failed");
      setBusy(false);
    }
  };
  const reject = async () => {
    setBusy(true);
    try {
      await api.post(`/knowledge/${item.id}/reject`, {});
      onDecided();
    } catch {
      setBusy(false);
    }
  };

  return (
    <div className="kn-card kn-draft">
      <div className="kn-phi mono">draft · private until approved (PHI checkpoint)</div>
      <div className="kn-content">{item.content}</div>
      {item.trigger_description && (
        <div className="kn-trig mono faint">when: {item.trigger_description}</div>
      )}
      {item.source_run_id && (
        <div className="kn-trig mono faint">distilled from run {item.source_run_id.slice(0, 8)}</div>
      )}
      <div className="kn-decide">
        <select
          className="kn-select mono"
          value={scope}
          onChange={(e) => setScope(e.target.value as ScopeChoice)}
        >
          <option value="global">share: global</option>
          <option value="repo">share: repo</option>
          <option value="user">keep: private</option>
        </select>
        {scope === "repo" && (
          <input
            className="kn-repo mono"
            placeholder="repo name"
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
          />
        )}
        <button className="btn btn-mono btn-primary" disabled={busy} onClick={() => void approve()}>
          approve
        </button>
        <button className="btn btn-mono btn-danger" disabled={busy} onClick={() => void reject()}>
          reject
        </button>
      </div>
      {err && <div className="kn-err mono">{err}</div>}
    </div>
  );
}

export function KnowledgeScreen() {
  const [corpus, setCorpus] = useState<KnowledgeItem[]>([]);
  const [pending, setPending] = useState<KnowledgeItem[]>([]);
  const [content, setContent] = useState("");
  const [trigger, setTrigger] = useState("");

  const load = useCallback(() => {
    api.get<KnowledgeItem[]>("/knowledge").then(setCorpus).catch(() => setCorpus([]));
    api.get<KnowledgeItem[]>("/knowledge/pending").then(setPending).catch(() => setPending([]));
  }, []);
  useEffect(load, [load]);

  const draft = async () => {
    if (!content.trim()) return;
    await api.post("/knowledge", { content: content.trim(), trigger_description: trigger.trim() });
    setContent("");
    setTrigger("");
    load();
  };

  return (
    <div className="knowledge-wrap">
      <header className="kn-screen-head">
        <h1 className="kn-screen-title">knowledge</h1>
        <p className="kn-screen-sub">the flywheel — private drafts become shared facts only by approval</p>
        <span className="kn-knobs"><span className="knob" /><span className="knob" /></span>
      </header>
      <div className="knowledge">
      <section className="kn-col">
        <h2 className="kn-head">draft inbox <span className="faint mono">{pending.length}</span></h2>
        {pending.length === 0 && <div className="faint mono">nothing waiting for approval</div>}
        {pending.map((k) => (
          <DraftCard key={k.id} item={k} onDecided={load} />
        ))}

        <h2 className="kn-head kn-compose-head">new draft</h2>
        <div className="kn-card">
          <textarea
            className="kn-input"
            placeholder="the distilled lesson — no transcripts, no PHI"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            rows={3}
          />
          <input
            className="kn-input mono"
            placeholder="when should this surface? (trigger description)"
            value={trigger}
            onChange={(e) => setTrigger(e.target.value)}
          />
          <div className="kn-decide">
            <button className="btn btn-mono btn-primary" onClick={() => void draft()}>
              save draft
            </button>
            <span className="faint mono kn-hint">born private; approval shares it</span>
          </div>
        </div>
      </section>

      <section className="kn-col">
        <h2 className="kn-head">corpus <span className="faint mono">{corpus.length}</span></h2>
        {corpus.length === 0 && (
          <div className="kn-empty">
            <span className="kn-empty-glyph">⌁</span>
            <div className="mono">nothing approved yet — the flywheel starts with the first draft</div>
          </div>
        )}
        {corpus.map((k) => (
          <div key={k.id} className="kn-card">
            <div className="kn-scope mono">
              <span className={`led ${k.status === "approved" ? "on" : "off"}`} />
              {k.scope}{k.repo ? `:${k.repo}` : ""} · {k.status}
            </div>
            <div className="kn-content">{k.content}</div>
            {k.trigger_description && (
              <div className="kn-trig mono faint">when: {k.trigger_description}</div>
            )}
          </div>
        ))}
      </section>
      </div>
      <style>{`
        .knowledge-wrap { max-width: 1180px; margin: 0 auto; padding: 26px 28px; height: 100%; overflow-y: auto; }
        .kn-screen-head { position: relative; margin-bottom: 24px; }
        .kn-screen-title { font-family: var(--font-display); font-size: 26px; font-weight: 600; margin: 0; }
        .kn-screen-sub { color: var(--ink-secondary); font-size: 14px; margin: 6px 0 0; }
        .kn-knobs { position: absolute; right: 0; top: 6px; display: flex; gap: 10px; }
        .knob { width: 24px; height: 24px; border-radius: 50%; background: radial-gradient(circle at 35% 30%, var(--bg-module), var(--jack) 75%); border: 1px solid var(--hairline); box-shadow: inset 0 2px 4px rgba(0,0,0,.55), 0 1px 0 rgba(255,255,255,.05); position: relative; }
        .knob::after { content: ""; position: absolute; left: 50%; top: 3px; width: 2px; height: 7px; background: var(--ink-faint); border-radius: 1px; transform: translateX(-50%) rotate(24deg); transform-origin: bottom center; }
        .knowledge { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; align-items: start; }
        @media (max-width: 900px) { .knowledge { grid-template-columns: 1fr; } }
        .kn-head { font-family: var(--font-display); font-size: 20px; font-weight: 600; display: flex; gap: 10px; align-items: baseline; margin: 0 0 16px; }
        .kn-compose-head { margin-top: 28px; }
        .kn-card { background: var(--bg-panel); border: 1px solid var(--hairline); border-radius: 12px; padding: 18px 20px; margin-bottom: 14px; box-shadow: 0 12px 32px rgba(0,0,0,.24); }
        .kn-draft { border-color: color-mix(in srgb, #d9a441 55%, var(--hairline)); }
        .kn-phi { font-size: 11px; color: #d9a441; margin-bottom: 10px; letter-spacing: .06em; text-transform: uppercase; }
        .kn-scope { font-size: 11.5px; color: var(--ink-secondary); margin-bottom: 9px; display: flex; align-items: center; gap: 8px; }
        .kn-content { font-size: 14.5px; line-height: 1.6; white-space: pre-wrap; }
        .kn-trig { font-size: 12px; margin-top: 9px; }
        .kn-decide { display: flex; gap: 10px; margin-top: 14px; align-items: center; flex-wrap: wrap; }
        .kn-decide .btn { padding: 10px 18px; font-size: 13.5px; }
        .kn-select, .kn-repo { background: var(--jack); color: var(--ink-primary); border: 1px solid var(--hairline); border-radius: var(--radius); padding: 9px 11px; font-size: 12.5px; }
        .kn-input { width: 100%; background: var(--jack); color: var(--ink-primary); border: 1px solid var(--hairline); border-radius: var(--radius); padding: 12px 14px; font-size: 14px; margin-bottom: 10px; font-family: inherit; box-sizing: border-box; box-shadow: inset 0 3px 8px rgba(0,0,0,.5); }
        .kn-hint { font-size: 11.5px; }
        .kn-err { color: var(--danger); font-size: 12px; margin-top: 10px; }
        .kn-empty { border: 1px dashed var(--hairline); border-radius: 12px; padding: 44px 18px; text-align: center; color: var(--ink-faint); font-size: 12.5px; }
        .kn-empty-glyph { font-family: var(--font-display); font-size: 32px; display: block; margin-bottom: 10px; }
      `}</style>
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";

/** Ideas space (§6): the team-wide collaboration layer. Threads + ALL comments
 *  (including Counsel's) are shared and permanent. Counsel speaks on demand —
 *  never uninvited. The Lead synthesis pins atop; raw voices stay below. */

interface IdeaThread {
  id: number;
  title: string;
  body: string;
  created_by: number;
  status: "open" | "summarized" | "promoted" | "archived";
  summary: {
    consensus: string;
    disagreements: string[];
    recommendation: string;
    open_questions: string[];
  } | null;
  promoted_run_id: string | null;
  comment_count?: number;
  created_at: string | null;
}

interface IdeaComment {
  id: number;
  author_type: "user" | "agent";
  author_name: string;
  body: string;
  created_at: string | null;
}

type ThreadDetail = IdeaThread & { comments: IdeaComment[] };

function CounselBadge() {
  return <span className="counsel-badge">counsel · 11th member</span>;
}

function SummaryCard({ summary }: { summary: NonNullable<IdeaThread["summary"]> }) {
  return (
    <div className="sum-card">
      <div className="sum-tag mono">lead synthesis · all voices</div>
      <p><strong>Consensus.</strong> {summary.consensus}</p>
      <p><strong>Recommendation.</strong> {summary.recommendation}</p>
      {summary.disagreements.length > 0 && (
        <p><strong>Disagreements.</strong> {summary.disagreements.join(" · ")}</p>
      )}
      {summary.open_questions.length > 0 && (
        <p><strong>Open questions.</strong> {summary.open_questions.join(" · ")}</p>
      )}
    </div>
  );
}

function ThreadView({ id, onBack }: { id: number; onBack: () => void }) {
  const [thread, setThread] = useState<ThreadDetail | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    api.get<ThreadDetail>(`/ideas/${id}`).then(setThread).catch(() => setThread(null));
  }, [id]);
  useEffect(load, [load]);

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    setErr("");
    try {
      await fn();
      load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : `${label} failed`);
    } finally {
      setBusy("");
    }
  };

  if (!thread) return <div className="faint mono iv-loading">loading thread…</div>;

  return (
    <div className="thread-view">
      <button className="btn btn-mono btn-ghost" onClick={onBack}>← all threads</button>
      <h2 className="iv-title">{thread.title}</h2>
      <div className="mono faint iv-status">{thread.status}{thread.promoted_run_id ? ` · run ${thread.promoted_run_id.slice(0, 8)}` : ""}</div>
      {thread.body && <p className="iv-body">{thread.body}</p>}

      {thread.summary && <SummaryCard summary={thread.summary} />}

      <div className="iv-voices">
        {thread.comments.map((c) => (
          <div key={c.id} className={`voice ${c.author_type === "agent" ? "voice-agent" : ""}`}>
            <div className="voice-head mono">
              {c.author_type === "agent" ? <CounselBadge /> : <span className="voice-name">{c.author_name}</span>}
            </div>
            <div className={`voice-body ${c.author_type === "agent" ? "counsel-voice" : ""}`}>{c.body}</div>
          </div>
        ))}
      </div>

      <div className="iv-composer">
        <textarea
          className="kn-input"
          rows={2}
          placeholder="add your voice — it stays on the thread forever"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <div className="iv-actions">
          <button
            className="btn btn-mono btn-primary"
            disabled={!draft.trim() || !!busy}
            onClick={() => void act("comment", async () => {
              await api.post(`/ideas/${id}/comments`, { body: draft.trim() });
              setDraft("");
            })}
          >
            comment
          </button>
          <button
            className="btn btn-mono btn-ghost"
            disabled={!!busy}
            onClick={() => void act("counsel", () => api.post(`/ideas/${id}/ask-counsel`, {}))}
          >
            {busy === "counsel" ? "counsel is thinking…" : "ask counsel"}
          </button>
          <button
            className="btn btn-mono btn-ghost"
            disabled={!!busy}
            onClick={() => void act("summarize", () => api.post(`/ideas/${id}/summarize`, {}))}
          >
            {busy === "summarize" ? "synthesizing…" : "summarize"}
          </button>
          <button
            className="btn btn-mono btn-ghost"
            disabled={!!busy || thread.status === "promoted"}
            onClick={() => void act("promote", () => api.post(`/ideas/${id}/promote`, {}))}
          >
            promote to plan
          </button>
        </div>
        {err && <div className="kn-err mono">{err}</div>}
      </div>
    </div>
  );
}

export function IdeasScreen() {
  const [threads, setThreads] = useState<IdeaThread[]>([]);
  const [openId, setOpenId] = useState<number | null>(null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");

  const load = useCallback(() => {
    api.get<IdeaThread[]>("/ideas").then(setThreads).catch(() => setThreads([]));
  }, []);
  useEffect(load, [load]);

  const create = async () => {
    if (!title.trim()) return;
    const t = await api.post<IdeaThread>("/ideas", { title: title.trim(), body: body.trim() });
    setTitle("");
    setBody("");
    load();
    setOpenId(t.id);
  };

  if (openId !== null) {
    return (
      <div className="ideas">
        <ThreadView id={openId} onBack={() => { setOpenId(null); load(); }} />
        <IdeasStyles />
      </div>
    );
  }

  return (
    <div className="ideas">
      <header className="id-head">
        <h1 className="id-title">ideas</h1>
        <p className="id-sub">the proposal space — shared, permanent, counsel is the 11th member</p>
        <span className="id-knobs"><span className="knob" /><span className="knob" /></span>
      </header>
      <div className="kn-card id-new">
        <div className="mono faint section-tag">open a thread</div>
        <input
          className="kn-input"
          placeholder="thread title — a feature thought, product direction, architecture concern"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <textarea
          className="kn-input"
          rows={3}
          placeholder="the idea, in your own words"
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
        <button className="btn btn-primary id-open-btn" onClick={() => void create()}>
          <span className="route-dot" />
          open thread
        </button>
      </div>
      <div className="mono faint section-tag id-threads-tag">threads</div>
      {threads.length === 0 && (
        <div className="id-empty">
          <span className="id-empty-glyph">⌁</span>
          <div className="mono">no threads yet — open the first one above</div>
        </div>
      )}
      {threads.map((t) => (
        <button key={t.id} className="idea-row" onClick={() => setOpenId(t.id)}>
          <span className="idea-title">{t.title}</span>
          <span className="mono faint idea-meta">
            {t.comment_count ?? 0} voices · {t.status}
          </span>
        </button>
      ))}
      <IdeasStyles />
    </div>
  );
}

function IdeasStyles() {
  return (
    <style>{`
      .ideas { max-width: 940px; margin: 0 auto; padding: 26px 28px; height: 100%; overflow-y: auto; }
      .id-head { position: relative; margin-bottom: 24px; }
      .id-title { font-family: var(--font-display); font-size: 26px; font-weight: 600; margin: 0; }
      .id-sub { color: var(--ink-secondary); font-size: 14px; margin: 6px 0 0; }
      .id-knobs { position: absolute; right: 0; top: 6px; display: flex; gap: 10px; }
      .knob { width: 24px; height: 24px; border-radius: 50%; background: radial-gradient(circle at 35% 30%, var(--bg-module), var(--jack) 75%); border: 1px solid var(--hairline); box-shadow: inset 0 2px 4px rgba(0,0,0,.55), 0 1px 0 rgba(255,255,255,.05); position: relative; }
      .knob::after { content: ""; position: absolute; left: 50%; top: 3px; width: 2px; height: 7px; background: var(--ink-faint); border-radius: 1px; transform: translateX(-50%) rotate(24deg); transform-origin: bottom center; }
      .section-tag { font-size: 11px; text-transform: uppercase; letter-spacing: .1em; margin-bottom: 12px; }
      .kn-card { background: var(--bg-panel); border: 1px solid var(--hairline); border-radius: 12px; padding: 20px; margin-bottom: 14px; box-shadow: 0 12px 32px rgba(0,0,0,.28); }
      .kn-input { width: 100%; background: var(--jack); color: var(--ink-primary); border: 1px solid var(--hairline); border-radius: var(--radius); padding: 12px 14px; font-size: 14.5px; margin-bottom: 12px; font-family: inherit; box-sizing: border-box; box-shadow: inset 0 3px 8px rgba(0,0,0,.5); }
      .id-open-btn { display: flex; align-items: center; gap: 9px; padding: 12px 22px; font-size: 14.5px; }
      .route-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; box-shadow: 0 0 6px 1px currentColor; }
      .id-threads-tag { margin-top: 24px; }
      .id-empty { border: 1px dashed var(--hairline); border-radius: 12px; padding: 48px 20px; text-align: center; color: var(--ink-faint); font-size: 12.5px; }
      .id-empty-glyph { font-family: var(--font-display); font-size: 34px; display: block; margin-bottom: 10px; }
      .idea-row { display: flex; justify-content: space-between; align-items: baseline; gap: 14px; width: 100%; text-align: left; background: var(--bg-panel); border: 1px solid var(--hairline); border-radius: 10px; padding: 17px 20px; margin-bottom: 12px; cursor: pointer; color: var(--ink-primary); transition: border-color .15s ease, transform .15s ease; }
      .idea-row:hover { border-color: var(--green-bright); transform: translateY(-1px); }
      .idea-title { font-size: 15.5px; font-weight: 600; }
      .idea-meta { font-size: 11.5px; white-space: nowrap; }
      .iv-loading { padding: 40px; text-align: center; }
      .iv-title { font-family: var(--font-display); font-size: 24px; font-weight: 600; margin: 16px 0 6px; }
      .iv-status { font-size: 12px; margin-bottom: 12px; }
      .iv-body { font-size: 14.5px; line-height: 1.6; color: var(--ink-secondary); white-space: pre-wrap; }
      .sum-card { background: color-mix(in srgb, var(--green-bright) 7%, var(--bg-panel)); border: 1px solid color-mix(in srgb, var(--green-bright) 35%, var(--hairline)); border-radius: 12px; padding: 18px 20px; margin: 18px 0; font-size: 14px; line-height: 1.6; }
      .sum-card p { margin: 8px 0; }
      .sum-tag { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--green-bright); margin-bottom: 8px; }
      .iv-voices { margin-top: 20px; }
      .voice { margin-bottom: 14px; }
      .voice-head { font-size: 11.5px; margin-bottom: 6px; }
      .voice-name { color: var(--ink-secondary); }
      .counsel-badge { font-style: italic; color: var(--blue-bright); }
      .voice-body { background: var(--bg-panel); border: 1px solid var(--hairline); border-radius: 10px; padding: 14px 18px; font-size: 14px; line-height: 1.6; white-space: pre-wrap; }
      .counsel-voice { font-style: italic; border-color: color-mix(in srgb, var(--blue-bright) 40%, var(--hairline)); }
      .iv-composer { margin-top: 20px; background: var(--bg-panel); border: 1px solid var(--hairline); border-radius: 12px; padding: 18px; }
      .iv-actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 4px; }
      .iv-actions .btn { padding: 10px 18px; font-size: 13.5px; }
      .kn-err { color: var(--danger); font-size: 12px; margin-top: 10px; }
    `}</style>
  );
}

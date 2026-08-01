import { useEffect, useState } from "react";
import { stageMeta } from "../../lib/runMachine";
import { api } from "../../lib/api";
import { useRuns } from "../../stores/run";
import { useUi } from "../../stores/ui";
import type { Run } from "../../types";

/** Inbox (§1): MY runs only — card per run with stage lamp + summary, a
 *  new-run composer (mode chip row + task + optional swarm width). */
const MODE_CHIPS = [
  { mode: "ask", label: "ask" },
  { mode: "plan", label: "plan" },
  { mode: "development", label: "develop" },
  { mode: "debug", label: "debug" },
  { mode: "agent-rnd", label: "swarm" },
];

function RunCard({ run, onOpen }: { run: Run; onOpen: () => void }) {
  const meta = stageMeta(run.stage);
  return (
    <button className="run-card" onClick={onOpen} data-testid={`run-card-${run.id}`}>
      <div className="rc-top">
        <span className={`rc-stage mono tone-${meta.tone}`}>
          <span className={`rc-lamp lamp-${meta.tone}`} />
          {meta.label}
        </span>
        <span className="mono faint">{run.mode}</span>
      </div>
      <div className="rc-title">{run.title}</div>
      {run.auto_summary && <div className="rc-summary">{run.auto_summary.slice(0, 140)}</div>}
      <div className="rc-meta mono faint">
        <span>{run.repo ?? "fleet"}</span>
        <span>${run.cost_usd.toFixed(2)}</span>
      </div>
    </button>
  );
}

export function InboxScreen() {
  const { runs, loadRuns, openRun, createRun } = useRuns();
  const setScreen = useUi((s) => s.setScreen);
  const [task, setTask] = useState("");
  const [mode, setMode] = useState("ask");
  const [fanout, setFanout] = useState<number | "">("");
  const [tickets, setTickets] = useState<{ id: number; title: string }[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void loadRuns();
    api
      .get<{ id: number; title: string }[]>("/hydration/my-tickets")
      .then(setTickets)
      .catch(() => setTickets([])); // unbound ADO identity → tickets simply absent
  }, [loadRuns]);

  const start = async (repo?: string, title?: string) => {
    setBusy(true);
    try {
      const run = await createRun({
        mode,
        task: title ?? task,
        repo,
        fanout: fanout === "" ? undefined : fanout,
      });
      setTask("");
      await openRun(run.id);
      setScreen("monitor");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="inbox">
      <header className="inbox-head">
        <h1 className="inbox-title">run inbox</h1>
        <p className="inbox-sub">your runs — patch a task into the rack</p>
        <span className="inbox-knobs"><span className="knob" /><span className="knob" /></span>
      </header>

      <div className="inbox-cols">
        <div className="inbox-rail">
          <div className="composer">
            <div className="mono faint section-tag">new run — pick a mode</div>
            <div className="mode-row">
              {MODE_CHIPS.map((c) => (
                <button
                  key={c.mode}
                  className={`mode-chip mono ${mode === c.mode ? "on" : ""}`}
                  onClick={() => setMode(c.mode)}
                >
                  <span className="mc-led" />
                  {c.label}
                </button>
              ))}
              {mode === "agent-rnd" && (
                <input
                  className="fanout mono"
                  type="number"
                  min={1}
                  placeholder="lanes"
                  value={fanout}
                  onChange={(e) => setFanout(e.target.value === "" ? "" : Number(e.target.value))}
                  title="swarm width — the Lead still authors the slices"
                />
              )}
            </div>
            <textarea
              className="composer-input"
              rows={3}
              placeholder={mode === "agent-rnd" ? "investigate across the fleet… (\"spawn 5 explorers on ClientApp\")" : "describe the task…"}
              value={task}
              onChange={(e) => setTask(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && task.trim() && (e.preventDefault(), void start())}
            />
            <button className="btn btn-primary route-btn" disabled={busy || !task.trim()} onClick={() => void start()}>
              <span className="route-dot" />
              {busy ? "routing…" : "route it"}
            </button>
          </div>

          {tickets.length > 0 && (
            <div className="tickets">
              <div className="mono faint section-tag">my ADO tickets</div>
              <div className="ticket-row">
                {tickets.map((t) => (
                  <button key={t.id} className="ticket mono" onClick={() => void start(undefined, t.title)}>
                    #{t.id} {t.title}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="run-list">
          <div className="mono faint section-tag">my runs</div>
          {runs.length === 0 && (
            <div className="empty-module">
              <span className="empty-glyph">⌁</span>
              <div className="empty-text mono">no runs yet — route the first one on the left</div>
            </div>
          )}
          {runs.map((r) => (
            <RunCard
              key={r.id}
              run={r}
              onOpen={async () => {
                await openRun(r.id);
                setScreen("monitor");
              }}
            />
          ))}
        </div>
      </div>

      <style>{`
        .inbox { max-width: 1240px; margin: 0 auto; padding: 26px 28px; height: 100%; overflow-y: auto; }
        .inbox-head { position: relative; margin-bottom: 22px; }
        .inbox-title { font-family: var(--font-display); font-size: 26px; font-weight: 600; margin: 0; }
        .inbox-sub { color: var(--ink-secondary); font-size: 14px; margin: 6px 0 0; }
        .inbox-knobs { position: absolute; right: 0; top: 6px; display: flex; gap: 10px; }
        .knob { width: 24px; height: 24px; border-radius: 50%; background: radial-gradient(circle at 35% 30%, var(--bg-module), var(--jack) 75%); border: 1px solid var(--hairline); box-shadow: inset 0 2px 4px rgba(0,0,0,.55), 0 1px 0 rgba(255,255,255,.05); position: relative; }
        .knob::after { content: ""; position: absolute; left: 50%; top: 3px; width: 2px; height: 7px; background: var(--ink-faint); border-radius: 1px; transform: translateX(-50%) rotate(24deg); transform-origin: bottom center; }

        .inbox-cols { display: grid; grid-template-columns: 400px 1fr; gap: 22px; align-items: start; }
        @media (max-width: 980px) { .inbox-cols { grid-template-columns: 1fr; } }

        .section-tag { font-size: 11px; text-transform: uppercase; letter-spacing: .1em; margin-bottom: 12px; }

        .composer { background: var(--bg-panel); border: 1px solid var(--hairline); border-radius: 12px; padding: 20px; box-shadow: 0 12px 32px rgba(0,0,0,.28); margin-bottom: 20px; }
        .mode-row { display: flex; flex-wrap: wrap; gap: 9px; margin-bottom: 14px; align-items: center; }
        .mode-chip { display: inline-flex; align-items: center; gap: 8px; background: var(--bg-module); border: 1px solid var(--hairline); color: var(--ink-secondary); border-radius: 8px; padding: 10px 16px; font-size: 13px; cursor: pointer; transition: border-color .15s ease, color .15s ease, transform .15s ease; }
        .mode-chip:hover { transform: translateY(-1px); border-color: var(--blue-bright); }
        .mc-led { width: 7px; height: 7px; border-radius: 50%; background: var(--ink-faint); }
        .mode-chip.on { border-color: var(--green); color: var(--green-bright); background: color-mix(in srgb, var(--green) 10%, var(--bg-module)); }
        .mode-chip.on .mc-led { background: var(--green-bright); box-shadow: 0 0 6px 1px var(--green-bright); }
        .fanout { width: 84px; background: var(--jack); border: 1px solid var(--hairline); border-radius: 8px; color: var(--ink-primary); padding: 10px 12px; font-size: 13px; }
        .composer-input { width: 100%; resize: vertical; background: var(--jack); border: 1px solid var(--hairline); border-radius: var(--radius); color: var(--ink-primary); padding: 13px 15px; font-size: 15px; font-family: var(--font-ui); box-shadow: inset 0 3px 8px rgba(0,0,0,.5); margin-bottom: 14px; }
        .route-btn { width: 100%; justify-content: center; padding: 13px; font-size: 15px; display: flex; align-items: center; gap: 9px; }
        .route-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; box-shadow: 0 0 6px 1px currentColor; }

        .tickets { margin-bottom: 18px; }
        .ticket-row { display: flex; flex-direction: column; gap: 8px; }
        .ticket { text-align: left; background: var(--bg-module); border: 1px solid var(--hairline); color: var(--blue-bright); border-radius: var(--radius); padding: 10px 14px; font-size: 12.5px; cursor: pointer; }
        .ticket:hover { border-color: var(--blue-bright); }

        .run-list { min-width: 0; }
        .empty-module { border: 1px dashed var(--hairline); border-radius: 12px; padding: 54px 20px; text-align: center; }
        .empty-glyph { font-family: var(--font-display); font-size: 34px; color: var(--ink-faint); display: block; margin-bottom: 10px; }
        .empty-text { font-size: 12.5px; color: var(--ink-faint); letter-spacing: .04em; }

        .run-card { width: 100%; background: var(--bg-panel); border: 1px solid var(--hairline); border-radius: 10px; padding: 16px 18px; text-align: left; color: var(--ink-primary); cursor: pointer; margin-bottom: 12px; transition: border-color .15s ease, transform .15s ease; }
        .run-card:hover { border-color: var(--blue-bright); transform: translateY(-1px); }
        .rc-top { display: flex; justify-content: space-between; margin-bottom: 8px; }
        .rc-stage { display: inline-flex; align-items: center; gap: 7px; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
        .rc-lamp { width: 8px; height: 8px; border-radius: 50%; background: currentColor; box-shadow: 0 0 6px 1px currentColor; }
        .tone-ok { color: var(--green-bright); } .tone-info { color: var(--blue-bright); }
        .tone-warn { color: #D9B36C; } .tone-danger { color: var(--danger); }
        .rc-title { font-weight: 600; font-size: 15.5px; margin-bottom: 5px; }
        .rc-summary { font-size: 13px; color: var(--ink-secondary); margin-bottom: 9px; }
        .rc-meta { display: flex; justify-content: space-between; font-size: 11px; }
      `}</style>
    </div>
  );
}

import { RAIL_STAGES, stageMeta } from "../lib/runMachine";
import type { RunStage } from "../types";

/** The readout rail (Patch Bay): one lamp per stage, done = blue trace,
 *  current = pulsing green. Off-rail terminals (failed/stopped) show as a
 *  banner tone instead of a lamp. */
export function PipelineBar({ stage }: { stage: RunStage }) {
  const meta = stageMeta(stage);
  return (
    <div className="pipeline" aria-label={`run stage: ${meta.label}`}>
      <div className="pipeline-track">
        {RAIL_STAGES.map((s, i) => {
          const state = meta.index === -1 ? "todo" : i < meta.index ? "done" : i === meta.index ? "current" : "todo";
          return (
            <div key={s} className={`pl-stage ${state}`}>
              {i < RAIL_STAGES.length - 1 && <span className="pl-rail" />}
              <span className="pl-lamp" />
              <span className="pl-text">{s.replace(/_/g, " ")}</span>
            </div>
          );
        })}
      </div>
      <style>{`
        .pipeline { padding: 10px 4px 2px; }
        .pipeline-track { display: flex; align-items: flex-start; }
        .pl-stage { display: flex; flex-direction: column; align-items: center; gap: 6px; flex: 1; position: relative; }
        .pl-lamp { width: 10px; height: 10px; border-radius: 50%; background: var(--jack); border: 1px solid var(--ink-faint); z-index: 2; }
        .pl-stage.done .pl-lamp { background: var(--blue); border-color: var(--blue-bright); }
        .pl-stage.current .pl-lamp { background: var(--green); border-color: var(--green-bright); color: var(--green); animation: glow-pulse 2.2s ease-in-out infinite; }
        .pl-rail { position: absolute; top: 4.5px; left: 50%; width: 100%; height: 1px; background: var(--hairline); z-index: 1; }
        .pl-stage.done .pl-rail { background: var(--blue); }
        .pl-text { font-family: var(--font-mono); font-size: 9px; color: var(--ink-faint); text-align: center; }
        .pl-stage.current .pl-text { color: var(--green-bright); }
      `}</style>
    </div>
  );
}

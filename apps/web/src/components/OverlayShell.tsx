import type { ReactNode } from "react";
import { useUi } from "../stores/ui";

/** 80% overlay shell (§1 monitor): the monitor keeps streaming underneath;
 *  close returns to a live screen, never a stale one. */
export function OverlayShell({ title, children }: { title: string; children: ReactNode }) {
  const popOverlay = useUi((s) => s.popOverlay);
  return (
    <div className="overlay-backdrop" role="dialog" aria-label={title}>
      <div className="overlay-panel">
        <div className="overlay-head">
          <span className="mono overlay-title">{title}</span>
          <button className="btn btn-mono btn-ghost" onClick={popOverlay} aria-label="close overlay">
            close ✕
          </button>
        </div>
        <div className="overlay-body">{children}</div>
      </div>
      <style>{`
        .overlay-backdrop {
          position: fixed; inset: 0; z-index: 100;
          background: color-mix(in srgb, var(--jack) 55%, transparent);
          backdrop-filter: blur(3px);
          display: flex; align-items: center; justify-content: center;
        }
        .overlay-panel {
          width: 80%; height: 82%;
          background: var(--bg-panel);
          border: 1px solid var(--hairline);
          border-radius: 10px;
          display: flex; flex-direction: column;
          box-shadow: 0 24px 60px color-mix(in srgb, var(--jack) 70%, transparent);
        }
        .overlay-head {
          display: flex; align-items: center; justify-content: space-between;
          padding: 12px 18px; border-bottom: 1px solid var(--hairline);
        }
        .overlay-title { font-size: 12.5px; font-weight: 600; letter-spacing: .03em; color: var(--ink-secondary); }
        .overlay-body { flex: 1; overflow-y: auto; padding: 18px; }
      `}</style>
    </div>
  );
}

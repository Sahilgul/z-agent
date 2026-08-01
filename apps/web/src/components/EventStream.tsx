import { useEffect, useRef } from "react";
import { foldStream, type StreamItem } from "../lib/runMachine";
import type { StepEvent } from "../types";

/** The glass box (§1): every agent step rendered the SAME way for lead and
 *  subagents — kind-colored rail, one-line title, expandable payload. WS
 *  typing deltas fold into live bubbles beneath the stored events. */
const KIND_TONE: Record<string, string> = {
  thinking: "var(--ink-faint)",
  command: "var(--blue-bright)",
  file_read: "var(--blue)",
  file_edit: "var(--green-bright)",
  mcp_call: "var(--blue-bright)",
  test_run: "var(--green)",
  message: "var(--green-bright)",
  notebook: "var(--green)",
  status: "var(--ink-faint)",
};

function Item({ item }: { item: StreamItem }) {
  return (
    <div className="es-item" data-kind={item.kind}>
      <span className="es-rail" style={{ background: KIND_TONE[item.kind] ?? "var(--hairline)" }} />
      <div className="es-body">
        <div className="es-title mono">
          {item.title}
          {item.ok === false && <span className="es-fail"> failed</span>}
        </div>
        {item.text && <pre className="es-text">{item.text}</pre>}
      </div>
    </div>
  );
}

export function EventStream({
  events,
  deltas,
  laneFilter,
}: {
  events: StepEvent[];
  deltas: { lane_id: string; kind: string; text: string }[];
  laneFilter?: string;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  const filtered = laneFilter ? events.filter((e) => e.lane_id === laneFilter) : events;
  const filteredDeltas = laneFilter ? deltas.filter((d) => d.lane_id === laneFilter) : deltas;
  const items = foldStream(filtered, filteredDeltas);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [items.length]);

  return (
    <div className="event-stream" data-testid="event-stream">
      {items.length === 0 && (
        <div className="faint mono es-empty">no trace yet — the agent's first step lands here</div>
      )}
      {items.map((i) => (
        <Item key={i.key} item={i} />
      ))}
      <div ref={endRef} />
      <style>{`
        .event-stream { padding: 12px 14px; overflow-y: auto; height: 100%; font-size: 13px; }
        .es-empty { padding: 18px 6px; font-size: 12px; }
        .es-item { display: flex; gap: 10px; margin-bottom: 10px; }
        .es-rail { width: 2px; border-radius: 2px; flex-shrink: 0; }
        .es-body { min-width: 0; flex: 1; }
        .es-title { font-size: 12px; color: var(--ink-secondary); }
        .es-fail { color: var(--danger); }
        .es-text {
          margin: 4px 0 0; font-family: var(--font-mono); font-size: 11.5px; line-height: 1.5;
          color: var(--ink-primary); white-space: pre-wrap; word-break: break-word;
          max-height: 220px; overflow-y: auto;
        }
      `}</style>
    </div>
  );
}

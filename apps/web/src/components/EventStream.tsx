import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { foldStream, type StreamItem } from "../lib/runMachine";
import type { StepEvent } from "../types";

/** The glass box: every agent step rendered the SAME way for lead and
 *  subagents — kind-colored rail, one-line title, expandable payload. WS
 *  typing deltas fold into live bubbles beneath the stored events. */
const KIND_RAIL: Record<string, string> = {
  thinking: "bg-ink-faint",
  command: "bg-blue-bright",
  file_read: "bg-blue",
  file_edit: "bg-green-bright",
  mcp_call: "bg-blue-bright",
  test_run: "bg-green",
  message: "bg-green-bright",
  notebook: "bg-green",
  status: "bg-ink-faint",
};

function Item({ item }: { item: StreamItem }) {
  return (
    <div className="mb-2.5 flex gap-2.5" data-kind={item.kind}>
      <span className={cn("w-0.5 flex-none rounded-sm", KIND_RAIL[item.kind] ?? "bg-hairline")} aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className="font-mono text-[12px] text-ink-secondary">
          {item.title}
          {item.ok === false && <span className="text-danger-bright"> failed</span>}
        </div>
        {item.text && (
          <pre className="mt-1 max-h-[220px] overflow-y-auto whitespace-pre-wrap break-words font-mono text-[11.5px] leading-[1.5] text-ink-primary">
            {item.text}
          </pre>
        )}
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
    <div
      className="h-full overflow-y-auto px-s4 py-s3 text-[13px]"
      data-testid="event-stream"
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      aria-label="agent event stream"
    >
      {items.length === 0 && (
        <div className="px-s2 py-s4 font-mono text-[12px] text-ink-faint">
          no trace yet — the agent's first step lands here
        </div>
      )}
      {items.map((i) => (
        <Item key={i.key} item={i} />
      ))}
      <span className="sr-only" aria-live="polite">
        {items.length} events
      </span>
      <div ref={endRef} />
    </div>
  );
}

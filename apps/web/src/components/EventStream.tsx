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

const BODY =
  "mt-1 max-h-[220px] overflow-y-auto whitespace-pre-wrap break-words font-mono text-[11.5px] leading-[1.5] text-ink-primary";

/** Reasoning is process, not answer: it stays open while the agent is mid-thought
 *  and folds away once the thought lands, still one click from being reread. */
function Thinking({ item }: { item: StreamItem }) {
  return (
    <details open={item.live} className="min-w-0 flex-1">
      <summary className="cursor-pointer list-none font-mono text-[12px] text-ink-faint hover:text-ink-secondary">
        {item.live ? "thinking…" : "thought for a moment"}
      </summary>
      <pre className={BODY}>{item.text}</pre>
    </details>
  );
}

function Item({ item }: { item: StreamItem }) {
  const isAnswer = item.kind === "message" && !item.live;
  // Short replies arrive as a title with no detail body; the answer must still
  // render, so fall back to the title rather than suppressing both.
  const body = isAnswer ? item.text || item.title : item.text;
  return (
    <div className="mb-2.5 flex gap-2.5" data-kind={item.kind}>
      <span className={cn("w-0.5 flex-none rounded-sm", KIND_RAIL[item.kind] ?? "bg-hairline")} aria-hidden="true" />
      {item.kind === "thinking" && item.text ? (
        <Thinking item={item} />
      ) : (
        <div className="min-w-0 flex-1">
          {/* The final answer is the point of the run — it gets prose treatment
              instead of the mono step title every other kind carries. */}
          {!isAnswer && (
            <div className="font-mono text-[12px] text-ink-secondary">
              {item.title}
              {item.ok === false && <span className="text-danger-bright"> failed</span>}
            </div>
          )}
          {body && (
            <pre
              className={cn(
                BODY,
                isAnswer && "max-h-none font-sans text-[13.5px] leading-[1.65] text-ink-primary",
              )}
            >
              {body}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

export function EventStream({
  events,
  deltas,
  laneFilter,
  prompt,
}: {
  events: StepEvent[];
  deltas: { lane_id: string; kind: string; text: string }[];
  laneFilter?: string;
  /** What the user asked — a transcript that opens with the agent's reply reads
   *  like an answer to a question nobody can see. */
  prompt?: string;
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
      {prompt && (
        <div className="mb-s3 flex gap-2.5" data-kind="prompt">
          <span className="w-0.5 flex-none rounded-sm bg-blue" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <div className="text-micro mb-s1 text-ink-faint">you</div>
            <div className="whitespace-pre-wrap break-words text-[13.5px] leading-[1.6] text-ink-primary">
              {prompt}
            </div>
          </div>
        </div>
      )}
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

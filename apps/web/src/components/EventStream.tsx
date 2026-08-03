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
        &lt;thinking&gt;
        {item.live && <span className="ml-s1 text-ink-faint">…</span>}
      </summary>
      <pre className={BODY}>{item.text}</pre>
    </details>
  );
}

/** The agent's prose reads as a left-aligned chat bubble, opposite the user's
 *  right-aligned one — the two voices are told apart at a glance. */
function Answer({ body }: { body: string }) {
  return (
    <div className="mb-2.5 flex" data-kind="message">
      <div className="max-w-[85%] rounded-2xl rounded-tl-sm border border-hairline bg-bg-raised px-s4 py-2.5">
        <div className="text-micro mb-s1 text-ink-faint">agent</div>
        <div className="whitespace-pre-wrap break-words font-sans text-[13.5px] leading-[1.65] text-ink-primary">
          {body}
        </div>
      </div>
    </div>
  );
}

/** The user's own message — right-aligned and filled, the mirror of Answer. */
function UserBubble({ body }: { body: string }) {
  return (
    <div className="mb-2.5 flex justify-end" data-kind="message">
      <div className="max-w-[80%] rounded-2xl rounded-tr-sm border border-blue bg-bg-module px-s4 py-2.5">
        <div className="text-micro mb-s1 text-right text-ink-faint">you</div>
        <div className="whitespace-pre-wrap break-words font-sans text-[13.5px] leading-[1.6] text-ink-primary">
          {body}
        </div>
      </div>
    </div>
  );
}

function Item({ item }: { item: StreamItem }) {
  const isAnswer = item.kind === "message" && !item.live;
  // Short replies arrive as a title with no detail body; the answer must still
  // render, so fall back to the title rather than suppressing both.
  const body = isAnswer ? item.text || item.title : item.text;
  if (isAnswer) {
    return item.role === "user" ? <UserBubble body={body} /> : <Answer body={body} />;
  }
  return (
    <div className="mb-2.5 flex gap-2.5" data-kind={item.kind}>
      <span className={cn("w-0.5 flex-none rounded-sm", KIND_RAIL[item.kind] ?? "bg-hairline")} aria-hidden="true" />
      {item.kind === "thinking" && item.text ? (
        <Thinking item={item} />
      ) : (
        <div className="min-w-0 flex-1">
          <div className="font-mono text-[12px] text-ink-secondary">
            {item.title}
            {item.ok === false && <span className="text-danger-bright"> failed</span>}
          </div>
          {body && (
            <pre className={BODY}>{body}</pre>
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
  const logRef = useRef<HTMLDivElement>(null);
  const filtered = laneFilter ? events.filter((e) => e.lane_id === laneFilter) : events;
  const filteredDeltas = laneFilter ? deltas.filter((d) => d.lane_id === laneFilter) : deltas;
  const items = foldStream(filtered, filteredDeltas);

  // scrollIntoView walks up and scrolls every ancestor — including the
  // overflow:hidden shell and the document — which drags the whole app
  // sideways and up. Drive this pane's own scrollTop instead.
  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [items.length]);

  return (
    <div
      ref={logRef}
      className="h-full overflow-x-hidden overflow-y-auto overscroll-contain px-s4 py-s3 text-[13px]"
      data-testid="event-stream"
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      aria-label="agent event stream"
    >
      {prompt && <UserBubble body={prompt} />}
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
    </div>
  );
}

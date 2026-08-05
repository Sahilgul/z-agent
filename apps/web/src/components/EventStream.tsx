import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import { Markdown } from "./Markdown";
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

/** Both voices share the left column and the full width: a transcript reads as
 *  one column of prose, not a chat app's zigzag, and wide payloads — tables,
 *  code fences, file:line citations — get every pixel instead of 80%. The tag
 *  and the frame colour tell the two speakers apart, so alignment doesn't
 *  have to. Markdown is rendered, never printed raw. */
function Bubble({ role, body }: { role: "user" | "agent"; body: string }) {
  const isUser = role === "user";
  return (
    <div className="mb-2.5" data-kind="message" data-role={role}>
      <div
        className={cn(
          "rounded-2xl rounded-tl-sm border px-s4 py-2.5",
          isUser ? "border-green bg-bg-module" : "border-hairline bg-bg-raised",
        )}
      >
        <div className={cn("text-micro mb-s1", isUser ? "text-green-bright" : "text-ink-faint")}>
          {isUser ? "you" : "agent"}
        </div>
        <Markdown>{body}</Markdown>
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
    return <Bubble role={item.role === "user" ? "user" : "agent"} body={body} />;
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
  deltas: { thread_id: string; kind: string; text: string }[];
  laneFilter?: string;
  /** What the user asked — a transcript that opens with the agent's reply reads
   *  like an answer to a question nobody can see. */
  prompt?: string;
}) {
  const logRef = useRef<HTMLDivElement>(null);
  const filtered = laneFilter ? events.filter((e) => e.thread_id === laneFilter) : events;
  const filteredDeltas = laneFilter ? deltas.filter((d) => d.thread_id === laneFilter) : deltas;
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
      {prompt && <Bubble role="user" body={prompt} />}
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

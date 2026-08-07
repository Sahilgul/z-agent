import { Fragment, memo, useEffect, useMemo, useRef } from "react";
import { cn } from "@/lib/utils";
import { Markdown } from "./Markdown";
import { CodeView, langFromPath } from "./CodeView";
import {
  ApprovalCard,
  CompactionCard,
  RecapCard,
  TodoChecklist,
  WarningCard,
} from "./feed/cards";
import { foldStream, type StreamItem } from "../lib/runMachine";
import { formatClock, formatTook } from "../lib/time";
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
  todo_checklist: "bg-green",
  compaction: "bg-blue",
  warning: "bg-warn",
  recap: "bg-blue-bright",
  approval: "bg-warn",
};

const BODY =
  "mt-1 max-h-[220px] overflow-y-auto whitespace-pre-wrap break-words font-mono text-[11.5px] leading-[1.5] text-ink-primary";

/** EXPERIMENTAL turn separator: a whisper-faint rule drawn BETWEEN turns —
 *  i.e. only where a new user message follows completed work — never inside
 *  a message (`---` in prose renders as spacing, see Markdown's hr override).
 *  Deliberately ghost/40: visible when you look for the boundary, invisible
 *  when you're reading. Tune or drop after the experiment. */
function TurnDivider() {
  return (
    <div
      data-testid="turn-divider"
      aria-hidden="true"
      className="my-s5 h-px w-full bg-ink-ghost/40"
    />
  );
}

/** Trace kinds whose payload is file content — rendered as VS Code-themed
 *  code instead of raw text. Commands stay raw: the terminal look is right. */
const CODE_KINDS = new Set(["file_read", "file_edit"]);

/** How close to the bottom (px) counts as "following the stream" — same
 *  reader tolerance as Feed's M-77. Beyond it the user is reading history
 *  and the pane must not yank them down on new content. */
const NEAR_BOTTOM_PX = 120;

/** Language for a code trace row: an edit carrying a unified diff highlights
 *  as diff (green/red lines); everything else resolves from the file path —
 *  detail.path when the worker sends it, else the title ("read src/app.py"). */
function codeLang(item: StreamItem): string | undefined {
  if (item.kind === "file_edit" && /^(--- |\+\+\+ |@@ )/m.test(item.text)) return "diff";
  const ref = item.detail.path ?? item.detail.file_path ?? item.title;
  return langFromPath(String(ref));
}

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

/** The clock stamp under every message; agent replies also carry the turn's
 *  "took Ns". Deliberately NOT text-micro — the parity contract reserves that
 *  class for speaker tags, which agent prose never shows. */
function MsgMeta({ ts, durationS, right }: { ts?: string | null; durationS?: number | null; right?: boolean }) {
  const clock = formatClock(ts);
  if (!clock && durationS == null) return null;
  return (
    <div
      data-testid="msg-meta"
      className={cn("mt-1 font-mono text-[10.5px] text-ink-faint", right && "text-right")}
    >
      {clock}
      {durationS != null && `${clock ? " · " : ""}${formatTook(durationS)}`}
    </div>
  );
}

/** Speakers are told apart by shape, not tags: the agent answers in plain
 *  prose (the surrounding trace is chrome enough), while the user's own
 *  message is a compact right-aligned card so you can re-find what you asked
 *  while scrolling a long session. Markdown is rendered, never printed raw. */
function Bubble({ role, body, ts, durationS }: {
  role: "user" | "agent";
  body: string;
  ts?: string | null;
  durationS?: number | null;
}) {
  if (role === "user") {
    return (
      <div className="mb-2.5 flex justify-end" data-kind="message" data-role="user">
        <div className="flex max-w-[80%] flex-col items-end">
          <div className="rounded-2xl rounded-br-sm bg-bg-module px-s4 py-2.5">
            <span className="sr-only">you</span>
            <Markdown>{body}</Markdown>
          </div>
          <MsgMeta ts={ts} right />
        </div>
      </div>
    );
  }
  return (
    <div className="mb-2.5 py-1" data-kind="message" data-role="agent">
      <span className="sr-only">agent</span>
      <Markdown>{body}</Markdown>
      <MsgMeta ts={ts} durationS={durationS} />
    </div>
  );
}

/** Memoized per item content, not identity: foldStream rebuilds item objects
 *  on every render, so without a field-wise comparator every historical card
 *  (and its Markdown parse + Prism highlight) re-rendered on every streamed
 *  token — the steady-state flicker under a growing bubble. */
const Item = memo(
  function Item({ item }: { item: StreamItem }) {
  // H-58: a streaming message (live) used to fall through to the rail-row
  // branch and only became a Bubble once `live` flipped false — so the pane
  // showed a one-line rail entry that snapped into a chat bubble mid-stream.
  // Render messages as bubbles from the first delta so there is no snap.
  const isAnswer = item.kind === "message";
  // Short replies arrive as a title with no detail body; the answer must still
  // render, so fall back to the title rather than suppressing both.
  const body = isAnswer ? item.text || item.title : item.text;
  if (isAnswer) {
    return (
      <Bubble
        role={item.role === "user" ? "user" : "agent"}
        body={body}
        ts={item.ts}
        durationS={item.durationS}
      />
    );
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
          {item.kind === "todo_checklist" && <TodoChecklist detail={item.detail} />}
          {item.kind === "compaction" && <CompactionCard detail={item.detail} />}
          {item.kind === "warning" && <WarningCard detail={item.detail} title={item.title} />}
          {item.kind === "recap" && <RecapCard detail={item.detail} />}
          {item.kind === "approval" && <ApprovalCard detail={item.detail} title={item.title} />}
          {!["todo_checklist", "compaction", "warning", "recap", "approval"].includes(item.kind) && body && (
            CODE_KINDS.has(item.kind) ? (
              <div className="mt-1 max-h-[220px] overflow-y-auto">
                <CodeView code={body} lang={codeLang(item)} maxLines={400} />
              </div>
            ) : (
              <pre className={BODY}>{body}</pre>
            )
          )}
        </div>
      )}
    </div>
  );
  },
  (a, b) =>
    a.item.kind === b.item.kind &&
    a.item.title === b.item.title &&
    a.item.text === b.item.text &&
    a.item.ok === b.item.ok &&
    a.item.live === b.item.live &&
    a.item.ts === b.item.ts &&
    a.item.role === b.item.role &&
    a.item.durationS === b.item.durationS &&
    // Stored events keep stable detail references across folds (the store
    // appends; it never mutates an event in place), so identity works here.
    a.item.detail === b.item.detail,
);

export function EventStream({
  events,
  deltas,
  laneFilter,
  prompt,
  promptTs,
}: {
  events: StepEvent[];
  deltas: { thread_id: string; kind: string; text: string }[];
  laneFilter?: string;
  /** What the user asked — a transcript that opens with the agent's reply reads
   *  like an answer to a question nobody can see. */
  prompt?: string;
  /** When the prompt was sent (run creation) — stamps the header bubble. */
  promptTs?: string | null;
}) {
  const logRef = useRef<HTMLDivElement>(null);
  // foldStream re-runs per token; keying the memo on the RAW store slices
  // (stable references between renders) keeps the fold itself off the
  // per-token path when a sibling state change re-renders this pane.
  const items = useMemo(
    () => foldStream(
      laneFilter ? events.filter((e) => e.thread_id === laneFilter) : events,
      laneFilter ? deltas.filter((d) => d.thread_id === laneFilter) : deltas,
    ),
    [events, deltas, laneFilter],
  );

  // scrollIntoView walks up and scrolls every ancestor — including the
  // overflow:hidden shell and the document — which drags the whole app
  // sideways and up. Drive this pane's own scrollTop instead.
  // Key on total content length, not items.length: a streaming message
  // grows its text without adding a new item, so items.length alone misses
  // mid-stream growth and the pane never follows the live bubble.
  //
  // "smooth" is reserved for a NEW item landing. Growth ticks fire per token
  // (20-50/sec) and each call would restart the smooth animation toward a
  // stale target — the constant cancel/restart was the visible pane flicker
  // ("blink blink") during streaming. Growth scrolls instantly instead.
  //
  // Stick-to-bottom (Cursor-style): the pane follows the stream ONLY while
  // the user is already near the bottom. A reader who scrolled up is left
  // alone — the stream waits, they scroll back when they're done. The one
  // exception: the user's own just-sent message always jumps to the bottom,
  // because sending implies they want to watch the reply land.
  const streamTick = items.reduce((n, i) => n + i.text.length, 0);
  const prevCount = useRef(0);
  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    const firstPaint = prevCount.current === 0;
    const landed = items.length !== prevCount.current;
    prevCount.current = items.length;
    const last = items[items.length - 1];
    const ownMessageLanded = landed && last?.kind === "message" && last?.role === "user";
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom > NEAR_BOTTOM_PX && !ownMessageLanded) return;
    el.scrollTo({ top: el.scrollHeight, behavior: firstPaint || !landed ? "auto" : "smooth" });
  }, [streamTick, items.length]);

  // The pane shrinks when the composer grows (auto-sizing textarea, mobile
  // keyboard). If the user is pinned to the bottom, re-pin after the resize
  // instead of leaving a strip of half-cut text above the composer.
  useEffect(() => {
    const el = logRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX) {
        el.scrollTo({ top: el.scrollHeight, behavior: "auto" });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <div
      ref={logRef}
      className="scroll-fade-b h-full overflow-x-hidden overflow-y-auto overscroll-contain px-s4 pt-s3 pb-[8vh] text-[13px]"
      data-testid="event-stream"
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      aria-label="agent event stream"
    >
      {prompt && <Bubble role="user" body={prompt} ts={promptTs} />}
      {items.length === 0 && (
        <div className="px-s2 py-s4 font-mono text-[12px] text-ink-faint">
          no trace yet — the agent's first step lands here
        </div>
      )}
      {items.map((i, idx) => (
        <Fragment key={i.key}>
          {/* A fresh user message closes the previous turn — mark the seam.
              The opening turn (first item, no prompt above) stays unmarked. */}
          {(idx > 0 || prompt) && i.kind === "message" && i.role === "user" && <TurnDivider />}
          <Item item={i} />
        </Fragment>
      ))}
      <span className="sr-only" aria-live="polite">
        {items.length} events
      </span>
    </div>
  );
}

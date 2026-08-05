import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { Markdown } from "../Markdown";
import {
  CARD_META,
  DISCLOSURE,
  clipPreview,
  shouldUseViewer,
  type CardKind,
} from "./cardTypes";
import {
  ApprovalCard,
  CompactionCard,
  RecapCard,
  TodoChecklist,
  WarningCard,
} from "./cards";
import { Viewer } from "./Viewer";

/** A feed item — the raw event mapped to a card. */
export interface FeedItem {
  key: string;
  kind: CardKind;
  title: string;
  text: string;
  threadId: string;
  ok: boolean | null;
  live: boolean;
  role: "user" | "agent" | null;
  /** For file cards: the file path (drives the viewer title). */
  filePath?: string;
  /** For diff cards: the diff content. */
  diff?: string;
  /** The typed card payload (todo tasks, compaction counts, approval args…). */
  detail?: Record<string, unknown>;
}

/** The two-tier disclosure: a bounded inline preview, click-through to the
 *  viewer modal. The inline row is a clickable chip; the full content lives
 *  in the modal. Never expand full content inline. */
function FeedRow({ item, onOpenViewer }: {
  item: FeedItem;
  onOpenViewer: (item: FeedItem) => void;
}) {
  const policy = DISCLOSURE[item.kind];
  // H-58: render messages as bubbles from the first delta (live or not) so a
  // streaming message doesn't render as a rail row and snap to a bubble
  // when the stream completes.
  const isMessage = item.kind === "message";
  const isThinking = item.kind === "thinking" && item.text;
  const isUser = item.role === "user";

  // Messages render as bubbles (chat alignment), not rail rows.
  if (isMessage) {
    const body = item.text || item.title;
    return (
      <div className="mb-2.5" data-kind="message" data-role={item.role}>
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

  const meta = CARD_META[item.kind];
  const useViewer = shouldUseViewer(item.kind, item.text);
  const { clipped, more } = useViewer ? { clipped: "", more: 0 } : clipPreview(item.text);
  const clickable = policy === "preview" || useViewer || policy === "viewer";

  return (
    <div
      className={cn(
        "group mb-2.5 flex gap-2.5",
        clickable && "cursor-pointer",
      )}
      data-kind={item.kind}
      onClick={clickable ? () => onOpenViewer(item) : undefined}
    >
      <span
        className={cn("w-0.5 flex-none rounded-sm", meta.rail)}
        aria-hidden="true"
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-s1 font-mono text-[12px] text-ink-secondary">
          <span className={cn("w-3 text-center", meta.rail.replace("bg-", "text-"))}>
            {meta.glyph}
          </span>
          <span className="truncate">{item.title}</span>
          {item.ok === false && <span className="text-danger-bright"> failed</span>}
          {clickable && (
            <span className="ml-auto text-ink-faint group-hover:text-blue-bright">
              {useViewer ? "view" : more > 0 ? `+${more} more` : "view"}
            </span>
          )}
        </div>
        {isThinking && (
          <details open={item.live} className="mt-1">
            <summary className="cursor-pointer list-none font-mono text-[12px] text-ink-faint hover:text-ink-secondary">
              &lt;thinking&gt;
              {item.live && <span className="ml-s1 text-ink-faint">…</span>}
            </summary>
            <pre className="mt-1 max-h-[220px] overflow-y-auto whitespace-pre-wrap break-words font-mono text-[11.5px] leading-[1.5] text-ink-primary">
              {item.text}
            </pre>
          </details>
        )}
        {policy === "inline" && !isThinking && item.kind === "todo_checklist" && item.detail && (
          <TodoChecklist detail={item.detail} />
        )}
        {policy === "inline" && !isThinking && item.kind === "compaction" && item.detail && (
          <CompactionCard detail={item.detail} />
        )}
        {policy === "inline" && !isThinking && item.kind === "warning" && item.detail && (
          <WarningCard detail={item.detail} title={item.title} />
        )}
        {policy === "inline" && !isThinking && item.kind === "recap" && item.detail && (
          <RecapCard detail={item.detail} />
        )}
        {policy === "inline" && !isThinking && item.kind === "approval" && item.detail && (
          <ApprovalCard detail={item.detail} title={item.title} />
        )}
        {policy === "inline" && !isThinking &&
          !["todo_checklist", "compaction", "warning", "recap", "approval"].includes(item.kind) &&
          item.text && (
          <pre className="mt-1 max-h-[220px] overflow-y-auto whitespace-pre-wrap break-words font-mono text-[11.5px] leading-[1.5] text-ink-primary">
            {item.text}
          </pre>
        )}
        {policy === "preview" && !useViewer && clipped && (
          <pre className="mt-1 max-h-[180px] overflow-y-auto whitespace-pre-wrap break-words font-mono text-[11.5px] leading-[1.5] text-ink-primary">
            {clipped}
            {more > 0 && (
              <span className="block mt-s1 text-ink-faint group-hover:text-blue-bright">
                +{more} more — click to expand
              </span>
            )}
          </pre>
        )}
        {(policy === "viewer" || useViewer) && (
          <div className="mt-1 font-mono text-[11.5px] text-ink-faint group-hover:text-blue-bright">
            {useViewer ? "large content — click to open viewer" : "click to open viewer"}
          </div>
        )}
      </div>
    </div>
  );
}

/** The full feed — 11 card types, two-tier viewer, thread filter.
 *  Factory-style: each tool call is a clickable row/chip, expandable to
 *  show details (diff preview, command output). */
export function Feed({
  items,
  threadFilter,
  prompt,
}: {
  items: FeedItem[];
  threadFilter?: string;
  prompt?: string;
}) {
  const logRef = useRef<HTMLDivElement>(null);
  const [viewerItem, setViewerItem] = useState<FeedItem | null>(null);
  const filtered = threadFilter
    ? items.filter((i) => i.threadId === threadFilter)
    : items;

  // Key on total content length, not filtered.length: a streaming message
  // grows its text without adding a new item, so filtered.length alone
  // misses mid-stream growth and the pane never follows the live bubble.
  const streamTick = filtered.reduce((n, i) => n + i.text.length, 0);
  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    // M-77: smooth-scroll on every streamTick change fought itself during
    // event bursts (competing smooth animations jittered/fought). Only
    // auto-scroll when the user is already near the bottom (don't yank a
    // reader who scrolled up to read), and use instant ("auto") behavior so
    // rapid bursts don't queue overlapping smooth scrolls.
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom > 120) return;  // user scrolled up to read
    el.scrollTo({ top: el.scrollHeight, behavior: "auto" });
  }, [streamTick, filtered.length]);

  return (
    <>
      <div
        ref={logRef}
        className="h-full overflow-x-hidden overflow-y-auto overscroll-contain px-s4 py-s3 text-[13px]"
        data-testid="feed"
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label="agent feed"
      >
        {prompt && (
          <div className="mb-2.5" data-kind="message" data-role="user">
            <div className="rounded-2xl rounded-tl-sm border border-green bg-bg-module px-s4 py-2.5">
              <div className="text-micro mb-s1 text-green-bright">you</div>
              <Markdown>{prompt}</Markdown>
            </div>
          </div>
        )}
        {filtered.length === 0 && (
          <div className="px-s2 py-s4 font-mono text-[12px] text-ink-faint">
            no trace yet — the agent's first step lands here
          </div>
        )}
        {filtered.map((i) => (
          <FeedRow key={i.key} item={i} onOpenViewer={setViewerItem} />
        ))}
        <span className="sr-only" aria-live="polite">
          {filtered.length} events
        </span>
      </div>
      <Viewer item={viewerItem} onClose={() => setViewerItem(null)} />
    </>
  );
}

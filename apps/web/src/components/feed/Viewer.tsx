import { useEffect } from "react";
import { cn } from "@/lib/utils";
import { Markdown } from "../Markdown";
import { CARD_META, type CardKind } from "./cardTypes";

/** The two-tier viewer — a separate, view-only modal for full file/diff
 *  content. Opened by clicking a preview row in the feed. Never expands
 *  full content inline (plan §19). The viewer is read-only: no edit actions,
 *  just the full content with a header showing the card kind + title + path. */
export function Viewer({ item, onClose }: {
  item: import("./Feed").FeedItem | null;
  onClose: () => void;
}) {
  useEffect(() => {
    if (!item) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [item, onClose]);

  if (!item) return null;
  const meta = CARD_META[item.kind as CardKind];

  return (
    <div
      className="fixed inset-0 z-overlay flex items-center justify-center bg-black/60 p-s6"
      onClick={onClose}
      data-testid="viewer"
      role="dialog"
      aria-modal="true"
      aria-label={item.title}
    >
      <div
        className={cn(
          "flex max-h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-lg border border-hairline bg-bg-panel shadow-overlay",
        )}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header — card kind + title + path */}
        <div className="flex items-center gap-s2 border-b border-hairline px-s5 py-s3">
          <span className={cn("w-0.5 self-stretch rounded-sm", meta.rail)} aria-hidden="true" />
          <span className="font-mono text-[11px] text-ink-faint">{meta.glyph}</span>
          <span className="text-micro text-ink-faint">{meta.label}</span>
          <span className="truncate font-mono text-[13px] text-ink-primary">
            {item.filePath ?? item.title}
          </span>
          {item.ok === false && <span className="text-danger-bright">failed</span>}
          <button
            onClick={onClose}
            className="ml-auto rounded-sm px-s2 py-s1 font-mono text-[11px] text-ink-faint hover:bg-bg-module hover:text-ink-primary"
            aria-label="close viewer"
          >
            esc
          </button>
        </div>

        {/* Body — the full content, view-only */}
        <div className="flex-1 overflow-y-auto bg-jack p-s5">
          {item.diff ? (
            <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-[1.5] text-ink-primary">
              {item.diff}
            </pre>
          ) : item.kind === "message" ? (
            <Markdown>{item.text}</Markdown>
          ) : (
            <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-[1.5] text-ink-primary">
              {item.text}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}

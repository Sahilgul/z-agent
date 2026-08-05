/** The console workspace — the 15 card types.
 *
 * Each card type has a rail color, an icon glyph, and a disclosure policy
 * (inline / click-through). The feed renders every agent step the SAME way
 * for lead and subagents — kind-colored rail, one-line title, expandable
 * payload. The two-tier disclosure contract: bounded inline preview (a few
 * clipped lines + "N more" counter), click-through to a separate view-only
 * modal for full file/diff content. Never expand full content inline. */

export type CardKind =
  | "thinking"
  | "command"
  | "file_read"
  | "file_edit"
  | "file_write"
  | "mcp_call"
  | "test_run"
  | "message"
  | "notebook"
  | "approval"
  | "status"
  | "todo_checklist"
  | "compaction"
  | "warning"
  | "recap";

export interface CardRail {
  /** Tailwind class for the left rail color. */
  rail: string;
  /** Monospace glyph shown in the title row. */
  glyph: string;
  /** Human label for the card kind. */
  label: string;
}

export const CARD_META: Record<CardKind, CardRail> = {
  thinking: { rail: "bg-ink-faint", glyph: "·", label: "thinking" },
  command: { rail: "bg-blue-bright", glyph: "$", label: "command" },
  file_read: { rail: "bg-blue", glyph: "R", label: "read" },
  file_edit: { rail: "bg-green-bright", glyph: "E", label: "edit" },
  file_write: { rail: "bg-green-bright", glyph: "W", label: "write" },
  mcp_call: { rail: "bg-blue-bright", glyph: "M", label: "mcp" },
  test_run: { rail: "bg-green", glyph: "T", label: "test" },
  message: { rail: "bg-green-bright", glyph: "¶", label: "message" },
  notebook: { rail: "bg-green", glyph: "N", label: "notebook" },
  approval: { rail: "bg-warn", glyph: "?", label: "approval" },
  status: { rail: "bg-ink-faint", glyph: "·", label: "status" },
  todo_checklist: { rail: "bg-green", glyph: "☑", label: "tasks" },
  compaction: { rail: "bg-blue", glyph: "▤", label: "compaction" },
  warning: { rail: "bg-warn", glyph: "⚠", label: "warning" },
  recap: { rail: "bg-blue-bright", glyph: "◆", label: "recap" },
};

/** The two-tier disclosure contract.
 * - INLINE: the content is small enough to show inline (thinking, message).
 * - PREVIEW: a bounded inline preview (a few clipped lines + "N more"),
 *   click-through opens the viewer modal (file_read, file_edit, command output).
 * - VIEWER: content is ONLY shown in the viewer modal, never inline (large diffs,
 *   full file contents). The inline row shows just the title. */
export type DisclosurePolicy = "inline" | "preview" | "viewer";

export const DISCLOSURE: Record<CardKind, DisclosurePolicy> = {
  thinking: "inline",
  command: "preview",
  file_read: "preview",
  file_edit: "preview",
  file_write: "viewer",
  mcp_call: "preview",
  test_run: "preview",
  message: "inline",
  notebook: "inline",
  approval: "inline",
  status: "inline",
  todo_checklist: "inline",
  compaction: "inline",
  warning: "inline",
  recap: "inline",
};

/** The inline preview clip limit — how many lines show before the "N more"
 *  counter. Two-tier disclosure: "feed ≤10 clipped diff lines +
 *  `+... (N more)`" (raised 6 → 10 to match the locked contract). */
export const PREVIEW_CLIP_LINES = 10;

/** The character cap for the inline preview body. */
export const PREVIEW_CLIP_CHARS = 600;

/** Viewer-threshold decision: the feed renders from
 *  payloads the engine has ALREADY bounded (edit-preview hunks ≤10 lines,
 *  truncated tool outputs) and preview-policy cards are always click-through
 *  to the viewer — so the 4000-char threshold is a FALLBACK for oversize
 *  payloads, not the primary gate. Kept at 4000. */
export const VIEWER_THRESHOLD_CHARS = 4000;

export function clipPreview(text: string): { clipped: string; more: number } {
  const lines = text.split("\n");
  if (lines.length <= PREVIEW_CLIP_LINES && text.length <= PREVIEW_CLIP_CHARS) {
    return { clipped: text, more: 0 };
  }
  const clippedLines = lines.slice(0, PREVIEW_CLIP_LINES);
  const clipped = clippedLines.join("\n").slice(0, PREVIEW_CLIP_CHARS);
  const more = lines.length - PREVIEW_CLIP_LINES;
  return { clipped, more: Math.max(more, 0) };
}

export function shouldUseViewer(kind: CardKind, text: string): boolean {
  if (DISCLOSURE[kind] === "viewer") return true;
  return text.length > VIEWER_THRESHOLD_CHARS;
}

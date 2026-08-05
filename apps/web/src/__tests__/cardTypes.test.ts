import { describe, it, expect } from "vitest";
import {
  CARD_META,
  DISCLOSURE,
  PREVIEW_CLIP_LINES,
  VIEWER_THRESHOLD_CHARS,
  clipPreview,
  shouldUseViewer,
  type CardKind,
} from "../components/feed/cardTypes";

describe("cardTypes", () => {
  it("defines 15 card kinds (taxonomy complete)", () => {
    const kinds = Object.keys(CARD_META) as CardKind[];
    expect(kinds).toHaveLength(15);
    // The additions: todo-checklist, compaction, ⚠ warning, ◆ recap.
    for (const k of ["todo_checklist", "compaction", "warning", "recap"] as CardKind[]) {
      expect(kinds).toContain(k);
    }
  });

  it("every card kind has a rail, glyph, and label", () => {
    for (const meta of Object.values(CARD_META)) {
      expect(meta.rail).toBeTruthy();
      expect(meta.glyph).toBeTruthy();
      expect(meta.label).toBeTruthy();
    }
  });

  it("every card kind has a disclosure policy", () => {
    const policies = Object.keys(DISCLOSURE) as CardKind[];
    expect(policies).toHaveLength(15);
    for (const p of Object.values(DISCLOSURE)) {
      expect(["inline", "preview", "viewer"]).toContain(p);
    }
  });

  it("messages and thinking use inline disclosure", () => {
    expect(DISCLOSURE.message).toBe("inline");
    expect(DISCLOSURE.thinking).toBe("inline");
  });

  it("file_write uses viewer-only disclosure", () => {
    expect(DISCLOSURE.file_write).toBe("viewer");
  });

  it("command and file_edit use preview disclosure", () => {
    expect(DISCLOSURE.command).toBe("preview");
    expect(DISCLOSURE.file_edit).toBe("preview");
  });
});

describe("clipPreview", () => {
  it("returns full text when under the clip limit", () => {
    const text = "line1\nline2\nline3";
    const { clipped, more } = clipPreview(text);
    expect(clipped).toBe(text);
    expect(more).toBe(0);
  });

  it("clips lines beyond the PREVIEW_CLIP_LINES limit", () => {
    const lines = Array.from({ length: 20 }, (_, i) => `line ${i + 1}`);
    const text = lines.join("\n");
    const { clipped, more } = clipPreview(text);
    expect(clipped.split("\n").length).toBeLessThanOrEqual(PREVIEW_CLIP_LINES);
    expect(more).toBe(20 - PREVIEW_CLIP_LINES);
    expect(more).toBeGreaterThan(0);
  });

  it("clips long single-line text by character cap", () => {
    const text = "x".repeat(1000);
    const { clipped, more } = clipPreview(text);
    expect(clipped.length).toBeLessThanOrEqual(600);
    expect(more).toBeGreaterThanOrEqual(0);
  });
});

describe("shouldUseViewer", () => {
  it("returns true for file_write regardless of size", () => {
    expect(shouldUseViewer("file_write", "small")).toBe(true);
  });

  it("returns false for small command output", () => {
    expect(shouldUseViewer("command", "ls -la")).toBe(false);
  });

  it("returns true for large content exceeding the threshold", () => {
    const big = "x".repeat(VIEWER_THRESHOLD_CHARS + 1);
    expect(shouldUseViewer("command", big)).toBe(true);
  });

  it("returns false for inline kinds even when large", () => {
    const big = "x".repeat(VIEWER_THRESHOLD_CHARS + 1);
    expect(shouldUseViewer("message", big)).toBe(false);
  });
});

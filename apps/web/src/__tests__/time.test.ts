import { describe, expect, it } from "vitest";
import { formatClock, formatDateTime, parseIso } from "../lib/time";

// W-H9: backend timestamps historically arrived tz-NAIVE; `new Date()` parses
// suffix-less ISO as browser-local, which east of UTC shifted every replayed
// timestamp and turned heartbeat ages negative (watchdog never fired).
// parseIso treats naive strings as UTC; offset-carrying strings are untouched.

describe("parseIso", () => {
  it("treats naive ISO as UTC", () => {
    expect(parseIso("2026-08-01T12:00:00")).toBe(Date.parse("2026-08-01T12:00:00Z"));
  });

  it("leaves offset-carrying ISO alone", () => {
    expect(parseIso("2026-08-01T12:00:00+00:00")).toBe(Date.parse("2026-08-01T12:00:00Z"));
    expect(parseIso("2026-08-01T12:00:00Z")).toBe(Date.parse("2026-08-01T12:00:00Z"));
    expect(parseIso("2026-08-01T17:30:00+05:30")).toBe(Date.parse("2026-08-01T12:00:00Z"));
  });

  it("heartbeat age east of UTC never goes negative for a fresh naive beat", () => {
    // Simulates UTC+5:30: a naive "now" UTC beat parsed as local would land
    // ~5.5h in the FUTURE — age negative, watchdog dead. parseIso keeps it ≥ 0.
    const naiveNow = new Date().toISOString().slice(0, 19); // drop the Z
    expect(Date.now() - parseIso(naiveNow)).toBeGreaterThanOrEqual(0);
    expect(Date.now() - parseIso(naiveNow)).toBeLessThan(5_000);
  });

  it("returns NaN for missing/garbage input", () => {
    expect(parseIso(null)).toBeNaN();
    expect(parseIso("")).toBeNaN();
    expect(parseIso("not a date")).toBeNaN();
  });
});

describe("formatters", () => {
  it("formatClock renders HH:MM:SS and tolerates null", () => {
    expect(formatClock(null)).toBe("");
    expect(formatClock("2026-08-01T12:34:56Z")).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });

  it("formatDateTime renders 'Mon D, HH:MM' and tolerates garbage", () => {
    expect(formatDateTime("not a date")).toBe("");
    expect(formatDateTime("2026-08-01T12:34:56Z")).toMatch(/^Aug 1, \d{2}:\d{2}$/);
  });
});

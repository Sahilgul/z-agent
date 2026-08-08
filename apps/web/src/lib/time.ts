/** Timestamp presentation for the session stream and history. Pure — no
 *  React imports. */

const pad = (n: number) => String(n).padStart(2, "0");

/** Parse a backend ISO timestamp as UTC when it carries no offset.
 *  Some serializers historically emitted tz-NAIVE ISO (`.isoformat()` on a
 *  naive datetime); `new Date()` parses a suffix-less string as browser-LOCAL,
 *  which east of UTC shifts replayed timestamps and turns heartbeat ages
 *  negative (the stale-thread watchdog then never fires). The backend now
 *  emits offset-suffixed ISO; this is the defensive web half for any naive
 *  string that still arrives. Returns NaN for unparseable input. */
export function parseIso(iso: string | null | undefined): number {
  if (!iso) return NaN;
  const s = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(iso) ? iso : `${iso}Z`;
  return Date.parse(s);
}

/** HH:MM:SS local — the per-message stamp under chat bubbles. */
export function formatClock(iso: string | null | undefined): string {
  const ms = parseIso(iso);
  if (Number.isNaN(ms)) return "";
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "Aug 6, 14:32" — session history rows, where the date matters as much as
 *  the time. Returns "" for missing/unparseable input. */
export function formatDateTime(iso: string | null | undefined): string {
  const ms = parseIso(iso);
  if (Number.isNaN(ms)) return "";
  const d = new Date(ms);
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "took 17s" — turn durations are shown in raw seconds, never rounded up
 *  to minutes, so 150s stays "150s". */
export function formatTook(seconds: number): string {
  return `took ${Math.max(0, Math.round(seconds))}s`;
}

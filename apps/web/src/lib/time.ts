/** Timestamp presentation for the session stream and history. Pure — no
 *  React imports. */

const pad = (n: number) => String(n).padStart(2, "0");

/** HH:MM:SS local — the per-message stamp under chat bubbles. */
export function formatClock(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "Aug 6, 14:32" — session history rows, where the date matters as much as
 *  the time. Returns "" for missing/unparseable input. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "took 17s" — turn durations are shown in raw seconds, never rounded up
 *  to minutes, so 150s stays "150s". */
export function formatTook(seconds: number): string {
  return `took ${Math.max(0, Math.round(seconds))}s`;
}

import { cn } from "@/lib/utils";

export type LampTone = "ok" | "info" | "warn" | "danger" | "off";

const LED_CLASS: Record<LampTone, string> = {
  ok: "led",
  info: "led led--blue",
  warn: "led led--warn",
  danger: "led led--red",
  off: "led led--off",
};

const TEXT_CLASS: Record<LampTone, string> = {
  ok: "text-green-bright",
  info: "text-blue-bright",
  warn: "text-warn",
  danger: "text-danger-bright",
  off: "text-ink-faint",
};

/** LED + text state — the LED never appears without its word. */
export function StatusLamp({ tone, label, className }: { tone: LampTone; label: string; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-s2 font-mono text-[11px] font-semibold uppercase tracking-[0.08em]", TEXT_CLASS[tone], className)}>
      <span className={LED_CLASS[tone]} aria-hidden="true" />
      {label}
    </span>
  );
}

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export type TagTone = "neutral" | "ok" | "info" | "warn" | "danger";

const TONE_CLASS: Record<TagTone, string> = {
  neutral: "border-hairline bg-bg-module text-ink-secondary",
  ok: "border-green/45 bg-bg-module text-ok-bright",
  info: "border-blue/55 bg-bg-module text-blue-bright",
  warn: "border-warn/40 bg-bg-module text-warn-bright",
  danger: "border-danger/60 bg-bg-module text-danger-bright",
};

/** Static metadata pill (not interactive — interactive filters use FilterChips). */
export function Tag({ tone = "neutral", children, className }: { tone?: TagTone; children: ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 w-fit shrink-0 items-center gap-s1 whitespace-nowrap rounded-sm border px-2 font-mono text-[11px] font-semibold",
        TONE_CLASS[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

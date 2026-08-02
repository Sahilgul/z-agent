import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

/** Empty module — dashed hairline, Fraunces glyph, mono hint. Extended with
 *  optional title (Fraunces 18px), primary action, and secondary link — the
 *  review's "text hints only" finding. The glyph + hint remain the base case. */
export function EmptyState({
  glyph = "⌁",
  hint,
  title,
  action,
  secondary,
  className,
}: {
  glyph?: string;
  hint: string;
  title?: string;
  action?: ReactNode;
  secondary?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-lg border border-dashed border-hairline px-s5 py-s12 text-center",
        className,
      )}
    >
      <span className="mb-s2 block font-display text-[30px] text-ink-ghost" aria-hidden="true">
        {glyph}
      </span>
      {title && (
        <div className="mb-s1 font-display text-[18px] font-medium text-ink-primary">{title}</div>
      )}
      <div className="font-mono text-[12.5px] tracking-[0.04em] text-ink-faint">{hint}</div>
      {action && <div className="mt-s4">{action}</div>}
      {secondary && <div className="mt-s2 font-mono text-[11.5px] text-ink-secondary">{secondary}</div>}
    </div>
  );
}

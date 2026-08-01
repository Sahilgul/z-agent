import { cn } from "@/lib/utils";

/** Empty module — dashed hairline, Fraunces glyph, mono hint. */
export function EmptyState({
  glyph = "⌁",
  hint,
  className,
}: {
  glyph?: string;
  hint: string;
  className?: string;
}) {
  return (
    <div className={cn("rounded-lg border border-dashed border-hairline px-s5 py-s12 text-center", className)}>
      <span className="mb-s2 block font-display text-[30px] text-ink-ghost" aria-hidden="true">
        {glyph}
      </span>
      <div className="font-mono text-[12.5px] tracking-[0.04em] text-ink-faint">{hint}</div>
    </div>
  );
}

import { cn } from "@/lib/utils";

export interface ChipOption<T extends string> {
  value: T;
  label: string;
}

/** Filter chip row — Stripe-style list filters. Pill, mono, single accent. */
export function FilterChips<T extends string>({
  options,
  value,
  onChange,
  className,
  disabledValues,
}: {
  options: ChipOption<T>[];
  value: T;
  onChange: (value: T) => void;
  className?: string;
  /** Values that cannot be selected. Rendered greyed-out with a tooltip
   *  via the option's `title`, and their click is suppressed. */
  disabledValues?: ReadonlySet<T>;
}) {
  return (
    <div className={cn("flex flex-wrap items-center gap-s2", className)} role="group">
      {options.map((o) => {
        const on = o.value === value;
        const disabled = disabledValues?.has(o.value) ?? false;
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={on}
            disabled={disabled}
            onClick={() => !disabled && onChange(o.value)}
            title={disabled ? "needs a registered repo to write to" : undefined}
            className={cn(
              "inline-flex items-center gap-s1 rounded-pill border px-3 py-1 font-mono text-[11.5px] font-semibold transition-colors duration-fast",
              on
                ? "border-green bg-green/10 text-ok-bright"
                : disabled
                  ? "border-hairline text-ink-faint opacity-50 cursor-not-allowed"
                  : "border-hairline text-ink-secondary hover:border-blue-bright hover:text-ink-primary",
            )}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

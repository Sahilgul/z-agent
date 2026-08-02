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
}: {
  options: ChipOption<T>[];
  value: T;
  onChange: (value: T) => void;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-wrap items-center gap-s2", className)} role="group">
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(o.value)}
            className={cn(
              "inline-flex items-center gap-s1 rounded-pill border px-3 py-1 font-mono text-[11.5px] font-semibold transition-colors duration-fast",
              on
                ? "border-green bg-green/10 text-ok-bright"
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

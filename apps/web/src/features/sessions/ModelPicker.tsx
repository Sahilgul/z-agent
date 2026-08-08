import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { cn } from "@/lib/utils";
import { api } from "../../lib/api";
import { qk } from "../../lib/queryKeys";
import type { ModelOption } from "../../types";

/** Composer model selector. Ask mode is multi-select (compare: one lane per
 *  model answers the same prompt); every other mode is single-select (the
 *  run's threads all use it). Empty selection = the deployment default, so
 *  the control always reads as a statement of what will run.
 *
 *  Each ACTIVE row also carries a reasoning control: "auto" sends no override
 *  (provider default — thinking on), "off" disables thinking, and any effort
 *  the model lists (low/high/max) pins it. Kimi offers no efforts — auto/off
 *  only. */
export function ModelPicker({
  selected,
  onChange,
  reasoning,
  onReasoningChange,
  multi,
}: {
  selected: string[];
  onChange: (next: string[]) => void;
  reasoning: Record<string, string>;
  onReasoningChange: (alias: string, effort: string | null) => void;
  multi: boolean;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const { data } = useQuery({
    queryKey: qk.models,
    queryFn: () => api.get<{ models: ModelOption[]; default: string }>("/models"),
    // The fleet changes on deploy, not mid-session — one fetch is enough.
    staleTime: Infinity,
    retry: false,
  });

  // Close on outside click / Escape — the popover anchors above the composer,
  // where a stray click otherwise leaves it hanging over the stream.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!data || data.models.length === 0) return null;
  const byAlias = new Map(data.models.map((m) => [m.alias, m]));
  const effective = selected.length > 0 ? selected : [data.default];
  const summary =
    effective.length === 1
      ? `model: ${byAlias.get(effective[0])?.label ?? effective[0]}`
      : `${effective.length} models`;

  const toggle = (alias: string) => {
    if (multi) {
      onChange(
        selected.includes(alias)
          ? selected.filter((a) => a !== alias)
          : [...selected, alias],
      );
      return;
    }
    // Single-select: re-clicking the active row clears back to the default.
    onChange(selected.includes(alias) ? [] : [alias]);
    setOpen(false);
  };

  return (
    <div ref={rootRef} className="relative" data-testid="model-picker">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={
          multi
            ? "models — pick several to compare answers side by side"
            : "model — one per run; compare mode lives in ask"
        }
        className={cn(
          "inline-flex items-center gap-s1 rounded-pill border px-2.5 py-1 font-mono text-[11px] transition-colors duration-fast",
          open || selected.length > 0
            ? "border-green text-ink-primary"
            : "border-hairline text-ink-secondary hover:border-green hover:text-ink-primary",
        )}
      >
        <span className="led led--blue" aria-hidden="true" />
        {summary}
        <span aria-hidden="true" className="text-ink-faint">{open ? "▴" : "▾"}</span>
      </button>
      {open && (
        <div
          role="listbox"
          aria-multiselectable={multi}
          aria-label="models"
          className="absolute bottom-full left-0 z-20 mb-s2 w-[300px] rounded-md border border-hairline bg-bg-panel py-s1 shadow-[0_8px_24px_rgba(0,0,0,0.45)]"
        >
          <div className="text-micro px-s3 py-s1 text-ink-faint">
            {multi ? "compare — one lane per model" : "one model per run"}
          </div>
          {data.models.map((m) => {
            const active = effective.includes(m.alias);
            const effort = reasoning[m.alias] ?? "auto";
            const efforts = ["auto", "off", ...m.reasoning_efforts];
            return (
              <div key={m.alias} role="option" aria-selected={active}>
                <button
                  type="button"
                  data-testid={`model-option-${m.alias}`}
                  onClick={() => toggle(m.alias)}
                  className={cn(
                    "flex w-full items-center gap-s2 px-s3 py-s2 text-left font-mono text-[11.5px] transition-colors duration-fast hover:bg-bg-module",
                    active ? "text-ink-primary" : "text-ink-secondary",
                    active && "pb-s1",
                  )}
                >
                  <span
                    aria-hidden="true"
                    className={cn(
                      "flex size-3.5 flex-none items-center justify-center rounded-[4px] border text-[9px]",
                      active ? "border-green bg-green/15 text-green-bright" : "border-hairline text-transparent",
                    )}
                  >
                    ✓
                  </span>
                  <span className="min-w-0 flex-1 truncate">{m.label}</span>
                  <span className="flex-none text-[10px] text-ink-faint">
                    ${m.price_in_per_mtok}/${m.price_out_per_mtok}
                  </span>
                </button>
                {active && (
                  <div
                    className="flex flex-wrap items-center gap-1 px-s3 pb-s2 pl-[34px]"
                    data-testid={`reasoning-row-${m.alias}`}
                  >
                    <span className="text-[9.5px] uppercase tracking-[0.08em] text-ink-faint">
                      reasoning
                    </span>
                    {efforts.map((e) => (
                      <button
                        key={e}
                        type="button"
                        aria-pressed={effort === e}
                        data-testid={`reasoning-${m.alias}-${e}`}
                        onClick={() => onReasoningChange(m.alias, e === "auto" ? null : e)}
                        className={cn(
                          "rounded-pill border px-1.5 py-px font-mono text-[10px] transition-colors duration-fast",
                          effort === e
                            ? "border-green bg-green/15 text-green-bright"
                            : "border-hairline text-ink-faint hover:border-green hover:text-ink-primary",
                        )}
                      >
                        {e}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
          <div className="border-t border-hairline px-s3 py-s1.5 text-[10px] text-ink-faint">
            $ per 1M tokens, in/out · reasoning: auto = provider default (thinking on)
          </div>
        </div>
      )}
    </div>
  );
}

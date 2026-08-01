import { cn } from "@/lib/utils";
import { RAIL_STAGES, stageMeta } from "../lib/runMachine";
import type { RunStage } from "../types";

/** The readout rail: one lamp per stage, done = blue trace, current =
 *  pulsing green. Off-rail terminals (failed/stopped) show as a banner
 *  tone instead of a lamp. */
export function PipelineBar({ stage }: { stage: RunStage }) {
  const meta = stageMeta(stage);
  return (
    <div aria-label={`run stage: ${meta.label}`} className="px-1 pb-0.5 pt-2">
      <div className="flex items-start">
        {RAIL_STAGES.map((s, i) => {
          const state = meta.index === -1 ? "todo" : i < meta.index ? "done" : i === meta.index ? "current" : "todo";
          return (
            <div key={s} className="relative flex flex-1 flex-col items-center gap-1.5">
              {i < RAIL_STAGES.length - 1 && (
                <span
                  aria-hidden="true"
                  className={cn(
                    "absolute left-1/2 top-[4.5px] z-0 h-px w-full",
                    state === "done" ? "bg-blue" : "bg-hairline",
                  )}
                />
              )}
              <span
                aria-hidden="true"
                className={cn(
                  "z-[1] size-2.5 rounded-full border",
                  state === "done" && "border-blue-bright bg-blue",
                  state === "current" && "animate-glow border-green-bright bg-green text-green",
                  state === "todo" && "border-ink-ghost bg-jack",
                )}
              />
              <span
                className={cn(
                  "text-center font-mono text-[9px]",
                  state === "current" ? "text-green-bright" : "text-ink-faint",
                )}
              >
                {s.replace(/_/g, " ")}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

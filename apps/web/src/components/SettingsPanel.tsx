import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { CheckIcon, XIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "../lib/api";
import { qk } from "../lib/queryKeys";
import { useUi } from "../stores/ui";
import type { ModelOption } from "../types";

/** Settings sidebar (right edge). Currently one setting: the default model
 *  for swarm/subagent lanes — goal explorers and swarm slices always spawn
 *  on it when set, regardless of the composer's lane selection. Persisted
 *  (localStorage) and sent as swarm_model on run creation. */
export function SettingsPanel() {
  const { settingsOpen, setSettingsOpen, swarmModel, setSwarmModel } = useUi();
  const rootRef = useRef<HTMLDivElement>(null);
  const { data } = useQuery({
    queryKey: qk.models,
    queryFn: () => api.get<{ models: ModelOption[]; default: string }>("/models"),
    staleTime: Infinity,
    retry: false,
    enabled: settingsOpen,
  });

  useEffect(() => {
    if (!settingsOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSettingsOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [settingsOpen, setSettingsOpen]);

  if (!settingsOpen) return null;

  const Row = ({ value, label, hint }: { value: string | null; label: string; hint?: string }) => {
    const active = swarmModel === value;
    return (
      <button
        type="button"
        onClick={() => setSwarmModel(value)}
        className={cn(
          "flex w-full items-center gap-s2 rounded-md px-s3 py-[7px] text-left font-mono text-[12px] transition-colors duration-fast",
          active ? "bg-bg-module text-green-bright" : "text-ink-secondary hover:bg-bg-module/60 hover:text-ink-primary",
        )}
      >
        <span className="min-w-0 flex-1 truncate">{label}</span>
        {hint && <span className="text-[10.5px] text-ink-faint">{hint}</span>}
        {active && <CheckIcon className="size-3.5 shrink-0" aria-hidden="true" />}
      </button>
    );
  };

  return (
    <div
      ref={rootRef}
      role="dialog"
      aria-label="settings"
      className="fixed inset-y-0 right-0 z-overlay flex w-[280px] flex-col border-l border-hairline bg-bg-panel shadow-lg"
    >
      <div className="flex h-[52px] flex-none items-center justify-between border-b border-hairline px-s4">
        <span className="font-mono text-[13px] font-semibold tracking-[0.03em]">settings</span>
        <button
          type="button"
          onClick={() => setSettingsOpen(false)}
          aria-label="close settings"
          className="rounded-md p-1.5 text-ink-faint transition-colors duration-fast hover:bg-bg-module hover:text-ink-primary"
        >
          <XIcon className="size-4" aria-hidden="true" />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-s3">
        <div className="text-micro mb-s1 px-s1 text-ink-faint">agents</div>
        <div className="mb-s2 px-s1 font-mono text-[12px] font-semibold text-ink-primary">
          swarm agent model
        </div>
        <p className="mb-s3 px-s1 text-[11.5px] leading-relaxed text-ink-faint">
          Subagents (goal explorers, swarm slices) always spawn on this model.
          Off = they follow the run's lane/default model.
        </p>
        <div className="flex flex-col gap-[2px]">
          <Row value={null} label="follow lane model" hint={data ? `default: ${data.default}` : undefined} />
          {(data?.models ?? []).map((m) => (
            <Row key={m.alias} value={m.alias} label={m.label} hint={m.vision ? "vision" : undefined} />
          ))}
        </div>
        {data && swarmModel && !data.models.some((m) => m.alias === swarmModel) && (
          <p className="mt-s3 px-s1 text-[11px] text-warn">
            saved model '{swarmModel}' is no longer in the fleet — pick another.
          </p>
        )}
      </div>
    </div>
  );
}

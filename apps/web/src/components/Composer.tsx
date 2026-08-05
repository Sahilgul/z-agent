import { useState } from "react";
import { cn } from "@/lib/utils";

/** The two-row composer (plan §19).
 *
 * Row 1 (chips): context chips — repo, branch, files attached. Clickable to
 * remove. Plus mode / model / budget selectors.
 * Row 2 (input): the textarea + send button. The textarea grows; Enter sends
 * (Shift+Enter for newline).
 *
 * The mode selector: ask / plan / development / goal.
 * The model selector: the LiteLLM gateway models (kimi, qwen, llama, ...).
 * The budget selector: $5 / $10 / $20 / $40 (per-run cap).
 */

export type ComposerMode = "ask" | "plan" | "development" | "goal";

export interface ContextChip {
  id: string;
  kind: "repo" | "branch" | "file" | "url";
  label: string;
}

export interface ComposerPayload {
  prompt: string;
  mode: ComposerMode;
  model: string;
  budgetUsd: number;
  chips: ContextChip[];
}

const MODES: { id: ComposerMode; label: string; hint: string }[] = [
  { id: "ask", label: "ask", hint: "read-only investigation" },
  { id: "plan", label: "plan", hint: "produce a plan, no writes" },
  { id: "development", label: "dev", hint: "implement with approvals" },
  { id: "goal", label: "goal", hint: "user story → PR, autonomous" },
];

const MODELS = ["kimi-k2", "qwen-foundry", "llama-405b"];

const BUDGETS = [5, 10, 20, 40];

export function Composer({
  onSubmit,
  disabled,
  defaultMode = "ask",
  defaultModel = "kimi-k2",
  defaultBudget = 20,
}: {
  onSubmit: (payload: ComposerPayload) => void;
  disabled?: boolean;
  defaultMode?: ComposerMode;
  defaultModel?: string;
  defaultBudget?: number;
}) {
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState<ComposerMode>(defaultMode);
  const [model, setModel] = useState(defaultModel);
  const [budget, setBudget] = useState(defaultBudget);
  const [chips, setChips] = useState<ContextChip[]>([]);

  const canSend = prompt.trim().length > 0 && !disabled;

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (canSend) submit();
    }
  }

  function submit() {
    if (!canSend) return;
    onSubmit({ prompt: prompt.trim(), mode, model, budgetUsd: budget, chips });
    setPrompt("");
  }

  function removeChip(id: string) {
    setChips((c) => c.filter((x) => x.id !== id));
  }

  return (
    <div className="border-t border-hairline bg-bg-panel" data-testid="composer">
      {/* Row 1: chips + selectors */}
      <div className="flex flex-wrap items-center gap-s2 px-s4 py-s2">
        {/* Context chips */}
        {chips.map((chip) => (
          <span
            key={chip.id}
            className="inline-flex items-center gap-s1 rounded-pill border border-hairline bg-bg-module px-s2 py-s1 font-mono text-[11px] text-ink-secondary"
          >
            <span className="text-ink-faint">{chip.kind}:</span>
            <span className="max-w-[160px] truncate">{chip.label}</span>
            <button
              onClick={() => removeChip(chip.id)}
              className="text-ink-faint hover:text-danger-bright"
              aria-label={`remove ${chip.kind}`}
            >
              ×
            </button>
          </span>
        ))}

        {/* Mode selector */}
        <div className="ml-auto flex items-center gap-s1">
          <span className="text-micro text-ink-faint">mode</span>
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as ComposerMode)}
            className="rounded-sm border border-hairline bg-bg-module px-s2 py-s1 font-mono text-[11px] text-ink-primary"
            aria-label="mode"
          >
            {MODES.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>

          {/* Model selector */}
          <span className="text-micro text-ink-faint">model</span>
          <select
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className="rounded-sm border border-hairline bg-bg-module px-s2 py-s1 font-mono text-[11px] text-ink-primary"
            aria-label="model"
          >
            {MODELS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>

          {/* Budget selector */}
          <span className="text-micro text-ink-faint">budget</span>
          <select
            value={budget}
            onChange={(e) => setBudget(Number(e.target.value))}
            className="rounded-sm border border-hairline bg-bg-module px-s2 py-s1 font-mono text-[11px] text-ink-primary"
            aria-label="budget"
          >
            {BUDGETS.map((b) => (
              <option key={b} value={b}>
                ${b}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Row 2: textarea + send */}
      <div className="flex items-end gap-s2 px-s4 pb-s3">
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          rows={1}
          placeholder="describe the task — Enter to send, Shift+Enter for newline"
          className="flex-1 resize-none rounded-md border border-hairline bg-jack px-s3 py-s2 text-[13px] text-ink-primary placeholder:text-ink-faint focus:border-blue-bright"
          aria-label="prompt"
        />
        <button
          onClick={submit}
          disabled={!canSend}
          className={cn(
            "rounded-md px-s4 py-s2 font-mono text-[12px] font-semibold",
            canSend
              ? "bg-green text-ink-on-green hover:bg-green-bright"
              : "bg-bg-module text-ink-faint",
          )}
        >
          send
        </button>
      </div>
    </div>
  );
}

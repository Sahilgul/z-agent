import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

/** Lazy-loaded mermaid renderer. This module is dynamically imported by
 *  Mermaid.tsx so the ~300KB+ mermaid chunk stays out of the main bundle and
 *  out of sessions that never emit a diagram.
 *
 *  Themed native (not pasted-in): `theme: "base"` + themeVariables mapped to
 *  our tokens so the SVG looks like Collegium drew it. `securityLevel: "strict"`
 *  because agent-generated diagrams are untrusted content.
 *
 *  Graceful fallback: a parse error or a thrown render renders the raw source
 *  as a code block + a small note (never crash, never blank). This also
 *  self-heals streaming — a half-arrived diagram shows as code, then swaps to
 *  the rendered SVG when the source parses. */

let initialized = false;
async function ensureMermaid() {
  const mod = await import("mermaid");
  const mermaid = mod.default;
  if (!initialized) {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "base",
      fontFamily: '"IBM Plex Sans", sans-serif',
      themeVariables: {
        background: "transparent",
        primaryColor: "var(--color-bg-module)",
        primaryTextColor: "var(--color-ink-primary)",
        primaryBorderColor: "var(--color-green-bright)",
        secondaryColor: "var(--color-bg-module)",
        secondaryTextColor: "var(--color-ink-secondary)",
        secondaryBorderColor: "var(--color-hairline)",
        tertiaryColor: "var(--color-bg-module)",
        tertiaryTextColor: "var(--color-ink-secondary)",
        tertiaryBorderColor: "var(--color-hairline)",
        lineColor: "var(--color-ink-faint)",
        textColor: "var(--color-ink-primary)",
        mainBkg: "var(--color-bg-module)",
        nodeBorder: "var(--color-green-bright)",
        clusterBkg: "transparent",
        clusterBorder: "var(--color-hairline)",
        edgeLabelBackground: "var(--color-bg-base)",
        // Sequence / class / state diagrams:
        actorBkg: "var(--color-bg-module)",
        actorBorder: "var(--color-green-bright)",
        actorTextColor: "var(--color-ink-primary)",
        actorLineColor: "var(--color-ink-faint)",
        signalColor: "var(--color-ink-secondary)",
        signalTextColor: "var(--color-ink-primary)",
        labelBoxBkgColor: "var(--color-bg-module)",
        labelBoxBorderColor: "var(--color-green-bright)",
        labelTextColor: "var(--color-ink-primary)",
        noteBkgColor: "var(--color-bg-module)",
        noteBorderColor: "var(--color-hairline)",
        noteTextColor: "var(--color-ink-secondary)",
        activationBkgColor: "color-mix(in srgb, var(--color-green-bright) 18%, transparent)",
        activationBorderColor: "var(--color-green-bright)",
      },
    });
    initialized = true;
  }
  return mermaid;
}

let idCounter = 0;
function nextId() {
  idCounter += 1;
  return `mermaid-svg-${idCounter}`;
}

type RenderState =
  | { kind: "ok"; svg: string }
  | { kind: "fallback"; note?: string };

export default function MermaidRenderer({ code }: { code: string }) {
  const [state, setState] = useState<RenderState | null>(null);
  const lastCode = useRef<string>("");

  useEffect(() => {
    let cancelled = false;
    const trimmed = code.trim();
    // Empty or whitespace-only source: nothing to render yet (mid-stream).
    if (!trimmed) {
      setState(null);
      return;
    }
    // Skip re-rendering the same source (defensive — parent usually remounts).
    if (trimmed === lastCode.current && state?.kind === "ok") return;
    lastCode.current = trimmed;

    (async () => {
      try {
        const mermaid = await ensureMermaid();
        const { svg } = await mermaid.render(nextId(), trimmed);
        if (!cancelled) setState({ kind: "ok", svg });
      } catch (err) {
        if (!cancelled) {
          setState({
            kind: "fallback",
            note: err instanceof Error ? err.message : "diagram failed to parse",
          });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
    // Re-run when the source changes. `state` is intentionally excluded —
    // including it would re-render on every state transition.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  if (!state) {
    // Mid-stream, before the closing fence: show the partial source as code so
    // the bubble isn't empty while the chunk loads / the diagram streams in.
    return (
      <pre
        className={cn(
          "m-0 overflow-x-auto whitespace-pre-wrap break-words bg-jack px-s3 py-s2.5",
          "font-mono text-[11.5px] leading-[1.5] text-ink-secondary",
        )}
        data-testid="mermaid-streaming"
      >
        {code}
      </pre>
    );
  }
  if (state.kind === "fallback") {
    return (
      <div data-testid="mermaid-fallback">
        <pre
          className={cn(
            "m-0 overflow-x-auto whitespace-pre-wrap break-words bg-jack px-s3 py-s2.5",
            "font-mono text-[11.5px] leading-[1.5] text-ink-primary",
          )}
        >
          {code}
        </pre>
        <div className="border-t border-hairline bg-bg-module px-s3 py-s1.5 font-mono text-[10.5px] text-ink-faint">
          diagram failed to parse{state.note ? ` — ${state.note}` : ""}
        </div>
      </div>
    );
  }
  return (
    <div
      className="flex justify-center overflow-x-auto px-s3 py-s3"
      data-testid="mermaid-svg"
      // Mermaid returns a full SVG string; inject it. securityLevel: "strict"
      // encodes HTML in labels, so this is safe for agent-generated content.
      dangerouslySetInnerHTML={{ __html: state.svg }}
    />
  );
}

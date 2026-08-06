import { Suspense, lazy } from "react";
import { FileIcon } from "./ui/file-icon";

/** Mermaid is ~300KB+ gzipped — it must never touch the main chunk or sessions
 *  without diagrams. Lazy-import on first mount only; the browser caches the
 *  chunk for every diagram after. */
const MermaidRenderer = lazy(() => import("./MermaidRenderer"));

/** Three-stage clean UX for a ` ```mermaid ` fence:
 *  1. Chunk loading (first diagram only): the frame mounts instantly with the
 *     skeleton-sweep shimmer as the body (DESIGN.md: skeletons match final
 *     geometry, no spinners).
 *  2. Streaming: the import kicks off the moment the opening fence arrives;
 *     partial source shows as a code block until the diagram parses.
 *  3. Rendered / fallback: themed SVG; on parse error, permanent fallback to
 *     the code block + a small "diagram failed to parse" note (never crash).
 *
 * The frame shares chrome with CodeView: rounded hairline frame on jack, slim
 * "diagram" header with a file-type glyph. */
function Shimmer() {
  return (
    <div
      className="h-[180px] animate-skeleton rounded-sm bg-bg-module"
      style={{
        backgroundImage:
          "linear-gradient(90deg, transparent, color-mix(in srgb, var(--color-green-bright) 8%, transparent), transparent)",
        backgroundSize: "200px 100%",
        backgroundRepeat: "no-repeat",
      }}
      aria-hidden="true"
    />
  );
}

function DiagramHeader() {
  return (
    <div className="flex items-center gap-s2 border-b border-hairline bg-bg-module px-s3 py-s1.5">
      <FileIcon kind="markdown" />
      <span className="font-mono text-[10.5px] uppercase tracking-[0.12em] text-ink-faint">
        diagram
      </span>
    </div>
  );
}

export function Mermaid({ code }: { code: string }) {
  // Re-render when the source changes (streaming grows the text; the closing
  // fence lands a complete diagram). The renderer is memoized on `code`.
  return (
    <div
      className="overflow-hidden rounded-md border border-hairline bg-jack"
      data-testid="mermaid"
    >
      <DiagramHeader />
      <Suspense fallback={<Shimmer />}>
        <MermaidRenderer code={code} />
      </Suspense>
    </div>
  );
}

import type { CSSProperties, ReactNode } from "react";
import { PrismAsyncLight as SyntaxHighlighter } from "react-syntax-highlighter";
import { vscDarkPlus } from "react-syntax-highlighter/dist/esm/styles/prism";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import diff from "react-syntax-highlighter/dist/esm/languages/prism/diff";
import docker from "react-syntax-highlighter/dist/esm/languages/prism/docker";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import jsx from "react-syntax-highlighter/dist/esm/languages/prism/jsx";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import markup from "react-syntax-highlighter/dist/esm/languages/prism/markup";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import toml from "react-syntax-highlighter/dist/esm/languages/prism/toml";
import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import { cn } from "@/lib/utils";
import { FileIcon, fileKindFromLang, fileKindFromPath, type FileKind } from "./ui/file-icon";

/** The one code surface in the app: VS Code Dark+ via Prism, used by chat
 *  markdown fences and by the file_read / file_edit trace payloads. The
 *  async-light build keeps prism + these grammars out of the main chunk.
 *
 *  v2.4 split: syntax speaks VS Code (vscDarkPlus token colors stay — devs'
 *  muscle memory), chrome speaks z-agent (jack surface + hairline frame +
 *  slim icon header). The block sits *in* the theme instead of floating as
 *  a foreign card. */
const LANGUAGES: Record<string, unknown> = {
  bash,
  css,
  diff,
  docker,
  javascript,
  json,
  jsx,
  markdown,
  markup,
  python,
  sql,
  toml,
  tsx,
  typescript,
  yaml,
};
for (const [name, lang] of Object.entries(LANGUAGES)) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  SyntaxHighlighter.registerLanguage(name, lang as any);
}

const EXT_LANG: Record<string, string> = {
  ts: "typescript",
  mts: "typescript",
  cts: "typescript",
  tsx: "tsx",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "jsx",
  py: "python",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  json: "json",
  jsonc: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  md: "markdown",
  mdx: "markdown",
  sql: "sql",
  css: "css",
  html: "markup",
  htm: "markup",
  xml: "markup",
  svg: "markup",
  diff: "diff",
  patch: "diff",
};

/** Language from any loose reference to a file — a path ("src/app.py"), or a
 *  trace title ("read backend/app/main.py L1-40"). Scans every ".ext" token,
 *  last known one wins, so a trailing line-range never masks the real ext. */
export function langFromPath(path: string): string | undefined {
  const s = path.toLowerCase();
  if (s.includes("dockerfile")) return "docker";
  const exts = [...s.matchAll(/\.([a-z0-9]{1,10})\b/g)].map((m) => m[1]).reverse();
  for (const ext of exts) {
    if (EXT_LANG[ext]) return EXT_LANG[ext];
  }
  return undefined;
}

/** Fence info strings arrive raw ("```sh", "```shell", "```ts") — normalize
 *  the common aliases onto the registered grammar names. */
const LANG_ALIAS: Record<string, string> = {
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  "shell-session": "bash",
  console: "bash",
  js: "javascript",
  ts: "typescript",
  py: "python",
  yml: "yaml",
  md: "markdown",
};

/** The slim header strip — VS Code editor-tab language: file-type icon +
 *  filename or language label in mono micro ink-faint. Same chrome family
 *  for code blocks and the terminal frame. */
function CodeHeader({ icon, label }: { icon: FileKind; label: string }) {
  return (
    <div className="flex items-center gap-s2 border-b border-hairline bg-bg-module px-s3 py-s1.5">
      <FileIcon kind={icon} />
      <span className="truncate font-mono text-[10.5px] uppercase tracking-[0.12em] text-ink-faint">
        {label}
      </span>
    </div>
  );
}

/** Terminal chrome: a slim shell-prompt header (no traffic-light dots — they
 *  are macOS window chrome, off-palette ornament; DESIGN.md deletes them),
 *  Prism bash colors below. Testid preserved for codeView.test.tsx. */
function TerminalFrame({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-hidden rounded-md border border-hairline font-mono" data-testid="terminal-frame">
      <CodeHeader icon="bash" label="terminal" />
      {children}
    </div>
  );
}

/** Long file dumps get highlighted only up to a budget — Prism is fast on
 *  snippets but a 5k-line file dump would stall the stream. */
export function CodeView({
  code,
  lang,
  maxLines,
  filename,
}: {
  code: string;
  lang?: string;
  maxLines?: number;
  /** Optional filename for the header label (trace context). Falls back to
   *  the language id, then "code". */
  filename?: string;
}) {
  let shown = code.replace(/\n$/, "");
  let hidden = 0;
  if (maxLines) {
    const lines = shown.split("\n");
    if (lines.length > maxLines) {
      shown = lines.slice(0, maxLines).join("\n");
      hidden = lines.length - maxLines;
    }
  }
  const wanted = lang ? (LANG_ALIAS[lang] ?? lang) : undefined;
  const resolved = wanted && wanted in LANGUAGES ? wanted : undefined;
  const moreFooter = hidden > 0 && (
    <div className="bg-jack px-s3 pb-s2.5 text-[10.5px] text-ink-faint">
      … {hidden} more lines not shown
    </div>
  );
  const headerLabel = filename ?? resolved ?? "code";
  const headerIcon = filename ? fileKindFromPath(filename) : fileKindFromLang(resolved);
  // No language (bare fence) or no registered grammar: the highlighter must
  // NEVER see an undefined/unregistered language — refractor throws
  // "Expected `string` for `aliasOrLanguage`" and the error boundary eats the
  // whole screen. Plain dark pre: same chrome, zero risk.
  if (!resolved) {
    return (
      <div className="overflow-hidden rounded-md border border-hairline font-mono">
        <CodeHeader icon={headerIcon} label={headerLabel} />
        <pre data-testid="code-plain" style={PRE_STYLE}>{shown}</pre>
        {moreFooter}
      </div>
    );
  }
  const body = (
    <SyntaxHighlighter
      language={resolved}
      style={vscDarkPlus}
      customStyle={{
        margin: 0,
        padding: hidden > 0 ? "10px 12px 4px" : "10px 12px",
        background: "var(--color-jack)",
        fontSize: "11.5px",
        lineHeight: 1.5,
      }}
      codeTagProps={{ style: { fontFamily: "inherit" } }}
    >
      {shown}
    </SyntaxHighlighter>
  );
  if (resolved === "bash") {
    return (
      <TerminalFrame>
        {body}
        {moreFooter}
      </TerminalFrame>
    );
  }
  return (
    <div className={cn("overflow-hidden rounded-md border border-hairline font-mono")}>
      <CodeHeader icon={headerIcon} label={headerLabel} />
      {body}
      {moreFooter}
    </div>
  );
}

const PRE_STYLE: CSSProperties = {
  margin: 0,
  padding: "10px 12px",
  background: "var(--color-jack)",
  fontSize: "11.5px",
  lineHeight: 1.5,
  color: "var(--color-ink-primary)",
  overflow: "auto",
};

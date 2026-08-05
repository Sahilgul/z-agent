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

/** The one code surface in the app: VS Code Dark+ via Prism, used by chat
 *  markdown fences and by the file_read / file_edit trace payloads. The
 *  async-light build keeps prism + these grammars out of the main chunk. */
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

const PRE_STYLE: CSSProperties = {
  margin: 0,
  padding: "10px 12px",
  background: "#1e1e1e",
  fontSize: "11.5px",
  lineHeight: 1.5,
  color: "#d4d4d4",
  overflow: "auto",
};

/** Terminal chrome: shell blocks read as a little terminal window — traffic
 *  lights + label on top, Prism bash colors below (commands yellow, strings
 *  green) instead of flat white text. */
function TerminalFrame({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-hidden rounded-md font-mono" data-testid="terminal-frame">
      <div className="flex items-center gap-1.5 bg-[#3c3c3c] px-3 py-1.5">
        <span className="h-2 w-2 rounded-full bg-[#ff5f57]" />
        <span className="h-2 w-2 rounded-full bg-[#febc2e]" />
        <span className="h-2 w-2 rounded-full bg-[#28c840]" />
        <span className="ml-1.5 text-[10.5px] text-[#a8a8a8]">terminal</span>
      </div>
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
}: {
  code: string;
  lang?: string;
  maxLines?: number;
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
    <div className="bg-[#1e1e1e] px-3 pb-2.5 text-[10.5px] text-ink-faint">
      … {hidden} more lines not shown
    </div>
  );
  // No language (bare fence) or no registered grammar: the highlighter must
  // NEVER see an undefined/unregistered language — refractor throws
  // "Expected `string` for `aliasOrLanguage`" and the error boundary eats the
  // whole screen. Plain dark pre: same chrome, zero risk.
  if (!resolved) {
    return (
      <div className="overflow-hidden rounded-md font-mono">
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
        background: "#1e1e1e",
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
    <div className="overflow-hidden rounded-md font-mono">
      {body}
      {moreFooter}
    </div>
  );
}

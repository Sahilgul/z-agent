import { cn } from "@/lib/utils";

/** Minimal file-type glyph set — the languages the agent actually streams.
 *  Tinted with the recognizable VS Code Material-Icon colors (TS blue, JS
 *  yellow, JSON orange, Python blue/yellow, shell green…) so a trace row reads
 *  at a glance like a VS Code editor tab. No icon-pack dependency. */

export type FileKind =
  | "typescript"
  | "tsx"
  | "javascript"
  | "jsx"
  | "python"
  | "json"
  | "yaml"
  | "toml"
  | "markdown"
  | "bash"
  | "docker"
  | "sql"
  | "css"
  | "markup"
  | "diff"
  | "env"
  | "generic";

const KIND_FROM_EXT: Record<string, FileKind> = {
  ts: "typescript",
  mts: "typescript",
  cts: "typescript",
  tsx: "tsx",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "jsx",
  py: "python",
  json: "json",
  jsonc: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  md: "markdown",
  mdx: "markdown",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  sql: "sql",
  css: "css",
  html: "markup",
  htm: "markup",
  xml: "markup",
  svg: "markup",
  diff: "diff",
  patch: "diff",
};

/** Resolve a FileKind from any loose file reference — a path, a fence info
 *  string, or a trace title. Mirrors CodeView.langFromPath so the icon and the
 *  grammar always agree. */
export function fileKindFromPath(path: string): FileKind {
  const s = path.toLowerCase();
  if (s.includes("dockerfile")) return "docker";
  if (/(^|\/)\.env(\.|$)/.test(s) || /\.(env|ini|conf)$/.test(s)) return "env";
  const exts = [...s.matchAll(/\.([a-z0-9]{1,10})\b/g)].map((m) => m[1]).reverse();
  for (const ext of exts) {
    if (KIND_FROM_EXT[ext]) return KIND_FROM_EXT[ext];
  }
  return "generic";
}

const KIND_FROM_LANG: Record<string, FileKind> = {
  typescript: "typescript",
  tsx: "tsx",
  javascript: "javascript",
  jsx: "jsx",
  python: "python",
  json: "json",
  yaml: "yaml",
  toml: "toml",
  markdown: "markdown",
  bash: "bash",
  docker: "docker",
  sql: "sql",
  css: "css",
  markup: "markup",
  diff: "diff",
};

/** A single normalized glyph per kind. Each is a small inline SVG drawn on a
 *  16x16 box; the kind's tint comes from a per-kind text color utility so the
 *  icon inherits the palette cleanly. */
const GLYPHS: Record<FileKind, { tint: string; path: string }> = {
  typescript: { tint: "#3178c6", path: "M2 3.5h12v9.5H2zM7 7v6h1.4V8.4h1.7V7H7zm5.2 1.1c-.2-.5-.7-.8-1.4-.8-.7 0-1.2.3-1.4.8-.2.5 0 .9.4 1.2l.9.5c.5.3.7.6.7 1 0 .5-.4.9-1 .9-.5 0-.9-.2-1.1-.6l-.9.5c.4.7 1 1 1.9 1 1.1 0 1.9-.6 1.9-1.5 0-.8-.4-1.2-1.3-1.6-.7-.3-.9-.5-.9-.8 0-.3.3-.6.7-.6.4 0 .7.2.8.5l.7-.5z" },
  tsx: { tint: "#3178c6", path: "M2 3.5h12v9.5H2zM7 7v6h1.4V8.4h1.7V7H7zm5.5 1.1c-.2-.5-.7-.8-1.4-.8-.8 0-1.3.4-1.3 1 0 .5.3.8 1.1 1.1.6.2.8.4.8.7 0 .4-.3.6-.7.6-.4 0-.7-.2-.9-.6l-.9.5c.3.7.9 1 1.8 1 1.1 0 1.8-.6 1.8-1.5 0-.7-.4-1.1-1.3-1.5-.6-.2-.8-.4-.8-.6 0-.3.2-.4.5-.4.3 0 .5.1.7.4l.6-.4z" },
  javascript: { tint: "#f7df1e", path: "M2 3.5h12v9.5H2zM8.4 11.4c.3.5.7.8 1.4.8.7 0 1.2-.4 1.2-1.1 0-.6-.4-.9-1-1.2l-.3-.1c-.3-.1-.4-.2-.4-.4 0-.2.2-.3.4-.3.3 0 .4.1.5.3l.7-.5c-.3-.4-.7-.6-1.3-.6-.7 0-1.1.4-1.1 1 0 .6.4.9 1 1.1l.3.1c.3.1.5.2.5.5 0 .2-.2.4-.5.4-.4 0-.6-.2-.8-.5l-.6.5zM5 11.7c0 .8-.4 1.1-1 1.1-.5 0-.8-.1-1.1-.6l.7-.5c.1.2.2.3.4.3.2 0 .3-.1.3-.4V8.4H5v3.3z" },
  jsx: { tint: "#f7df1e", path: "M2 3.5h12v9.5H2zM9.5 11.4c.3.5.7.8 1.4.8.7 0 1.2-.4 1.2-1.1 0-.6-.4-.9-1-1.2l-.3-.1c-.3-.1-.4-.2-.4-.4 0-.2.2-.3.4-.3.3 0 .4.1.5.3l.7-.5c-.3-.4-.7-.6-1.3-.6-.7 0-1.1.4-1.1 1 0 .6.4.9 1 1.1l.3.1c.3.1.5.2.5.5 0 .2-.2.4-.5.4-.4 0-.6-.2-.8-.5l-.6.5zM5.5 8.4c-1.1 0-1.8.7-1.8 1.7 0 .9.5 1.5 1.4 1.6l-.6 1h.9l.5-.9h.4v.9h.8V8.4h-1.6zm.1.7h.7v1.1h-.7c-.5 0-.8-.3-.8-.6 0-.3.3-.5.8-.5z" },
  python: { tint: "#4584b6", path: "M8 3c-1.7 0-2.5.6-2.5 1.7v1h2.5v.3H4.2C3 6 2 6.7 2 8.3s.9 2.4 2.2 2.4h1.3v-1c0-1 .8-1.6 1.7-1.6h2.5c.8 0 1.3-.5 1.3-1.3V4.7C10.5 3.6 9.5 3 8 3zm-.6.7c.3 0 .6.3.6.6 0 .3-.3.6-.6.6-.3 0-.6-.3-.6-.6 0-.3.3-.6.6-.6zM8 13c1.7 0 2.5-.6 2.5-1.7v-1H8v-.3h3.3c1.2 0 2.2-.7 2.2-2.3s-.9-2.4-2.2-2.4h-1.3v1c0 1-.8 1.6-1.7 1.6H6.2c-.8 0-1.3.5-1.3 1.3v2.5C4.5 12.4 5.5 13 8 13zm.6-.7c-.3 0-.6-.3-.6-.6 0-.3.3-.6.6-.6.3 0 .6.3.6.6 0 .3-.3.6-.6.6z" },
  json: { tint: "#cbcb41", path: "M5 3c-1.5 0-2 .8-2 2v1c0 .6-.3 1-1 1v1c.7 0 1 .4 1 1v1c0 1.2.5 2 2 2h.5v-1H5c-.6 0-1-.3-1-1V9c0-.7-.4-1.2-1-1.5.6-.3 1-.8 1-1.5V4c0-.7.4-1 1-1h.5V3H5zm6 0c1.5 0 2 .8 2 2v1c0 .6.3 1 1 1v1c-.7 0-1 .4-1 1v1c0 1.2-.5 2-2 2h-.5v-1h.5c.6 0 1-.3 1-1V9c0-.7.4-1.2 1-1.5-.6-.3-1-.8-1-1.5V4c0-.7-.4-1-1-1h-.5V3h.5z" },
  yaml: { tint: "#cb171e", path: "M3 3h3l2 4 2-4h3v9h-2V6L8 10 5 6v6H3V3zm10 0h2v9h-2V3z" },
  toml: { tint: "#9c4221", path: "M3 3h10v2h-4v7H7V5H3V3z" },
  markdown: { tint: "#519aba", path: "M3 4h10v8H3V4zm1 1v6h8V5H4zm1 1h1.5l1.5 2 1.5-2H10v4H9V8L7.5 10 6 8v2H5V6z" },
  bash: { tint: "#4eaa25", path: "M3 4h10v8H3V4zm1 1v6h8V5H4zm.5 1c.3.3.6.4 1 .4l-.3.5c-.5 0-.9-.2-1.2-.5L4.5 6zm4 2.5h2v.5h-2v-.5z" },
  docker: { tint: "#2496ed", path: "M5 6h2v2H5V6zm3 0h2v2H8V6zm3 0h2v2h-2V6zM3 9h2v2H3V9zm3 0h2v2H6V9zm3 0h2v2H9V9zm3 0h2v2h-2V9zM2 6h2v2H2V6zm11-1c1 0 1.5.4 1.7 1 .3-.2.7-.3 1.1-.3.4 0 .8.1 1.1.4.4.4.5.9.4 1.4-.2 1.3-1.5 2.3-3 2.3-1.6 0-3-1.1-3-2.5 0-.7.4-1.3 1-1.7-.2-.4-.5-.6-.5-.6z" },
  sql: { tint: "#e38c00", path: "M8 3C5 3 3 3.7 3 4.5v7C3 12.3 5 13 8 13s5-.7 5-1.5v-7C13 3.7 11 3 8 3zm-4 1.5C4 4.2 5.6 4 8 4s4 .2 4 .5S10.4 5 8 5 4 4.8 4 4.5zM4 6c1 .5 2.4.7 4 .7s3-.2 4-.7v2c0 .3-1.6.5-4 .5s-4-.2-4-.5V6zm0 3.5c1 .5 2.4.7 4 .7s3-.2 4-.7v2c0 .3-1.6.5-4 .5s-4-.2-4-.5v-2z" },
  css: { tint: "#519aba", path: "M3 3l.7 8.3L8 13l4.3-1.7L13 3H3zm2.4 2h5.2l-.2 1.6H5.6l.1 1h6.7l-.4 4-3.9 1.4-4-1.4-.2-2h1.5l.1 1 1.9.7 2-.7.2-2.2H5.3L5.4 5z" },
  markup: { tint: "#e44d26", path: "M3 3l1.5 9.5L8 14l3.5-1.5L13 3H3zm2.4 2h5.2l-.2 1H6.5l.1 1h3.7l-.3 3.3L8 11l-1.9-.7L5.9 8h.9l.1 1.3.9.3 1-.3.1-1.6H5.7L5.4 5z" },
  diff: { tint: "#6a9955", path: "M3 4h2v3H3V4zm0 5h2v3H3V9zm5-5h5v1H8V4zm0 3h5v1H8V7zm0 3h5v1H8v-1z" },
  env: { tint: "#ecd53f", path: "M3 3h10v10H3V3zm1 1v8h8V4H4zm1 1h6v1H5V5zm0 2h6v1H5V7zm0 2h4v1H5V9z" },
  generic: { tint: "#9aa39a", path: "M4 3h6l3 3v9H4V3zm5 1v3h3L9 4z" },
};

/** Resolve a FileKind from a Prism-style language id (the fence info string
 *  after alias normalization). Falls back to `generic`. */
export function fileKindFromLang(lang?: string): FileKind {
  if (!lang) return "generic";
  return KIND_FROM_LANG[lang] ?? "generic";
}

export function FileIcon({
  kind,
  className,
  size = 14,
}: {
  kind: FileKind;
  className?: string;
  size?: number;
}) {
  const glyph = GLYPHS[kind];
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      className={cn("inline-block flex-none", className)}
      aria-hidden="true"
      data-testid="file-icon"
      data-kind={kind}
    >
      <path d={glyph.path} fill={glyph.tint} />
    </svg>
  );
}

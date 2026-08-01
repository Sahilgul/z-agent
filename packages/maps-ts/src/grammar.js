// Canonical map grammar for Zagent's repo-map (layer 1 of the context system).
//
// This module is the SINGLE source of truth for the shape of a `map.json`
// document on the TypeScript side. It MUST stay byte-identical in key order and
// sorting with the Python grammar in `packages/maps/src/zagent_maps/grammar.py`
// so that a TS repo mapped by either generator produces the same structure for
// equivalent inputs. That parity is what makes the map diff-stable across runs
// and across the two language ecosystems.
//
// Grammar (top-level keys, in this exact order):
//
//   version        : number  — grammar schema version (currently 1).
//   generated_at   : string  — ISO-8601 UTC timestamp; pinned by tests for
//                              byte-stable golden comparison.
//   repo           : string  — repository name (basename of the repo path).
//   modules        : Module[]   — directory tree with one-line inferred purpose.
//   symbols        : Symbol[]   — public classes/functions per module.
//   edges          : Edge[]     — intra-repo import edges between top-level modules.
//   hot_files      : HotFile[]  — files most imported by others, ranked desc.
//   entrypoints    : string[]   — known entry files (index.ts / main.ts / ...).
//   tests          : string[]   — test file paths.
//
// Sub-object shapes (field order is canonical and MUST be preserved):
//
//   Module  : { path, language, file_count, purpose }
//   Symbol  : { module, file, name, kind, signature, line }
//   Edge    : { from, to, kind }
//   HotFile : { file, importers }
//
// Determinism rules enforced by `canonicalize()`:
//   * every list is sorted by a stable, total key;
//   * every path is forward-slash POSIX regardless of host OS;
//   * `generated_at` is the ONLY non-deterministic field.

export const SCHEMA_VERSION = 1;

export const LANGUAGE_TYPESCRIPT = "typescript";
export const LANGUAGE_JAVASCRIPT = "javascript";
export const EDGE_IMPORT = "import";

export const ROOT_MODULE = ".";

/**
 * @typedef {Object} Module
 * @property {string} path
 * @property {string} language
 * @property {number} file_count
 * @property {string} purpose
 */

/**
 * @typedef {Object} Symbol_
 * @property {string} module
 * @property {string} file
 * @property {string} name
 * @property {string} kind
 * @property {string} signature
 * @property {number} line
 */

/**
 * @typedef {Object} Edge
 * @property {string} from
 * @property {string} to
 * @property {string} kind
 */

/**
 * @typedef {Object} HotFile
 * @property {string} file
 * @property {number} importers
 */

/**
 * @typedef {Object} RepoMap
 * @property {number} version
 * @property {string} generated_at
 * @property {string} repo
 * @property {Module[]} modules
 * @property {Symbol_[]} symbols
 * @property {Edge[]} edges
 * @property {HotFile[]} hot_files
 * @property {string[]} entrypoints
 * @property {string[]} tests
 */

/** Normalize a path to forward-slash POSIX form for diff-stable output. */
export function toPosix(p) {
  return p.replace(/\\/g, "/");
}

/** Stable sort keys — total orderings so output is byte-stable across runs. */
export const moduleSortKey = (m) => [m.path];
export const symbolSortKey = (s) => [s.file, s.line, s.name, s.kind];
export const edgeSortKey = (e) => [e.from, e.to, e.kind];
export const hotFileSortKey = (h) => [-h.importers, h.file];

function cmp(a, b) {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const av = a[i] === undefined ? null : a[i];
    const bv = b[i] === undefined ? null : b[i];
    if (typeof av === "number" && typeof bv === "number") {
      if (av < bv) return -1;
      if (av > bv) return 1;
    } else {
      const as = String(av), bs = String(bv);
      if (as < bs) return -1;
      if (as > bs) return 1;
    }
  }
  return 0;
}

function sortBy(arr, keyFn) {
  return arr.slice().sort((x, y) => cmp(keyFn(x), keyFn(y)));
}

/** Return a copy of `m` with every list deterministically sorted. Idempotent. */
export function canonicalize(m) {
  return {
    version: m.version,
    generated_at: m.generated_at,
    repo: m.repo,
    modules: sortBy(m.modules ?? [], moduleSortKey),
    symbols: sortBy(m.symbols ?? [], symbolSortKey),
    edges: sortBy(m.edges ?? [], edgeSortKey),
    hot_files: sortBy(m.hot_files ?? [], hotFileSortKey),
    entrypoints: sortBy(m.entrypoints ?? [], (x) => [x]),
    tests: sortBy(m.tests ?? [], (x) => [x]),
  };
}

/** Serialize a RepoMap to a plain object with canonical key order. */
export function toDict(m) {
  return {
    version: m.version,
    generated_at: m.generated_at,
    repo: m.repo,
    modules: (m.modules ?? []).map((mod) => ({
      path: mod.path,
      language: mod.language,
      file_count: mod.file_count,
      purpose: mod.purpose,
    })),
    symbols: (m.symbols ?? []).map((s) => ({
      module: s.module,
      file: s.file,
      name: s.name,
      kind: s.kind,
      signature: s.signature,
      line: s.line,
    })),
    edges: (m.edges ?? []).map((e) => ({ from: e.from, to: e.to, kind: e.kind })),
    hot_files: (m.hot_files ?? []).map((h) => ({ file: h.file, importers: h.importers })),
    entrypoints: (m.entrypoints ?? []).slice(),
    tests: (m.tests ?? []).slice(),
  };
}

/** Parse a plain object (e.g. loaded from map.json) back into a RepoMap. */
export function fromDict(d) {
  return {
    version: d.version ?? SCHEMA_VERSION,
    generated_at: d.generated_at ?? "",
    repo: d.repo ?? "",
    modules: (d.modules ?? []).map((m) => ({
      path: m.path,
      language: m.language,
      file_count: m.file_count,
      purpose: m.purpose ?? "",
    })),
    symbols: (d.symbols ?? []).map((s) => ({
      module: s.module,
      file: s.file,
      name: s.name,
      kind: s.kind,
      signature: s.signature ?? "",
      line: s.line,
    })),
    edges: (d.edges ?? []).map((e) => ({
      from: e.from,
      to: e.to,
      kind: e.kind ?? EDGE_IMPORT,
    })),
    hot_files: (d.hot_files ?? []).map((h) => ({
      file: h.file,
      importers: h.importers,
    })),
    entrypoints: (d.entrypoints ?? []).slice(),
    tests: (d.tests ?? []).slice(),
  };
}

/** Render a condensed MAP.md-style text view of the map. Pure + deterministic. */
export function renderMarkdown(m) {
  const lines = [];
  lines.push(`# ${m.repo} — repo map`);
  lines.push("");
  lines.push(`_generated: ${m.generated_at} · grammar v${m.version}_`);
  lines.push("");
  lines.push("## Modules");
  for (const mod of m.modules) {
    const purpose = mod.purpose || "(no inferred purpose)";
    lines.push(`- \`${mod.path}\` (${mod.language}, ${mod.file_count} files) — ${purpose}`);
  }
  lines.push("");
  lines.push("## Entrypoints");
  if (m.entrypoints.length) {
    for (const ep of m.entrypoints) lines.push(`- \`${ep}\``);
  } else {
    lines.push("- _(none detected)_");
  }
  lines.push("");
  lines.push("## Symbols");
  if (m.symbols.length) {
    for (const s of m.symbols) {
      lines.push(`- \`${s.file}:${s.line}\` ${s.kind} \`${s.name}\` — ${s.signature}`);
    }
  } else {
    lines.push("- _(none detected)_");
  }
  lines.push("");
  lines.push("## Edges");
  if (m.edges.length) {
    for (const e of m.edges) lines.push(`- \`${e.from}\` -> \`${e.to}\` (${e.kind})`);
  } else {
    lines.push("- _(none detected)_");
  }
  lines.push("");
  lines.push("## Hot files");
  if (m.hot_files.length) {
    for (const h of m.hot_files) lines.push(`- \`${h.file}\` (${h.importers} importers)`);
  } else {
    lines.push("- _(none detected)_");
  }
  lines.push("");
  lines.push("## Tests");
  if (m.tests.length) {
    for (const t of m.tests) lines.push(`- \`${t}\``);
  } else {
    lines.push("- _(none detected)_");
  }
  lines.push("");
  return lines.join("\n");
}

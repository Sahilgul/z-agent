// TypeScript repo-map generator — analyzes TS/JS repos via regex/token heuristics.
//
// This is the TypeScript half of the shared-grammar generator pair. It scans a
// repo, tokenizes every .ts/.tsx/.js/.jsx file with regex heuristics, and emits
// a RepoMap conforming to the canonical grammar in `grammar.js`. The Python
// half lives in `packages/maps` and emits the same shape for Python repos.
//
// Design notes:
//   * No npm runtime deps, no build step — plain ESM .js with JSDoc types.
//   * Never crashes on a single bad file: unreadable / binary files are skipped
//     and counted as `crashes` in the run report.
//   * Deterministic: output is canonicalized (sorted) before writing, so two
//     runs over the same tree produce byte-identical map.json (modulo
//     `generated_at`, which the CLI sets to wall-clock time and tests pin).

import { readdirSync, readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { join, relative, dirname, basename, resolve } from "node:path";

import {
  SCHEMA_VERSION, LANGUAGE_TYPESCRIPT, LANGUAGE_JAVASCRIPT, EDGE_IMPORT, ROOT_MODULE,
  canonicalize, renderMarkdown, toDict, toPosix,
} from "./grammar.js";

const IGNORED_DIRS = new Set([
  "node_modules", ".git", ".hg", ".svn",
  ".venv", "venv", "env",
  "dist", "build", "out", "coverage", ".next",
  ".zagent", "__pycache__",
  ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox",
]);

const SOURCE_EXTENSIONS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"];
const TEST_EXTENSIONS = [".spec.ts", ".test.ts", ".spec.tsx", ".test.tsx",
  ".spec.js", ".test.js", ".spec.jsx", ".test.jsx"];

const ENTRYPOINT_BASENAMES = new Set([
  "index.ts", "index.tsx", "index.js", "index.jsx", "index.mjs",
  "main.ts", "main.tsx", "main.js", "main.jsx",
  "app.ts", "app.js", "server.ts", "server.js",
  "cli.ts", "cli.js", "run.ts", "run.js",
]);

function iterSourceFiles(repoRoot) {
  const out = [];
  function walk(dir) {
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const ent of entries) {
      if (ent.name.toLowerCase() === ".git") continue;
      const full = join(dir, ent.name);
      if (ent.isDirectory()) {
        if (IGNORED_DIRS.has(ent.name.toLowerCase())) continue;
        walk(full);
      } else if (ent.isFile()) {
        const lower = ent.name.toLowerCase();
        if (SOURCE_EXTENSIONS.some((ext) => lower.endsWith(ext))) {
          if (lower.endsWith(".d.ts")) continue;
          out.push(full);
        }
      }
    }
  }
  walk(repoRoot);
  return out;
}

function relPosix(repoRoot, p) {
  return toPosix(relative(repoRoot, p));
}

function dirModule(repoRoot, p) {
  const parent = dirname(p);
  if (parent === repoRoot) return ROOT_MODULE;
  return relPosix(repoRoot, parent);
}

function languageOfFile(name) {
  const lower = name.toLowerCase();
  if (lower.endsWith(".ts") || lower.endsWith(".tsx") || lower.endsWith(".cts") || lower.endsWith(".mts")) {
    return LANGUAGE_TYPESCRIPT;
  }
  return LANGUAGE_JAVASCRIPT;
}

function inferDocstring(src) {
  // First top-of-file block comment /** ... */ or /* ... */ before any code.
  const m = src.match(/^\s*(\/\*\*?[\s\S]*?\*\/)/);
  if (!m) return null;
  const block = m[1];
  const inner = block.replace(/^\/\*\*?/, "").replace(/\*\/$/, "");
  const firstLine = inner.split("\n").map((l) => l.replace(/^\s*\*\s?/, "").trim())
    .find((l) => l.length > 0);
  return firstLine || null;
}

const RE_EXPORT_CLASS = /^\s*export\s+(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)/;
const RE_EXPORT_FUNCTION = /^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+?\*?\s*([A-Za-z_$][\w$]*)/;
const RE_EXPORT_INTERFACE = /^\s*export\s+interface\s+([A-Za-z_$][\w$]*)/;
const RE_EXPORT_TYPE = /^\s*export\s+type\s+([A-Za-z_$][\w$]*)\s*=/;
const RE_EXPORT_CONST = /^\s*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/;

function trimSig(line) {
  let s = line.replace(/^\s+/, "");
  const brace = s.indexOf("{");
  if (brace > 0) s = s.slice(0, brace).trim() + " { ... }";
  if (s.length > 120) s = s.slice(0, 117) + "...";
  return s;
}

function extractSymbols(src, rel, module) {
  const symbols = [];
  const lines = src.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m;
    if ((m = line.match(RE_EXPORT_CLASS))) {
      symbols.push({ module, file: rel, name: m[1], kind: "class", signature: trimSig(line), line: i + 1 });
    } else if ((m = line.match(RE_EXPORT_FUNCTION))) {
      symbols.push({ module, file: rel, name: m[1], kind: "function", signature: trimSig(line), line: i + 1 });
    } else if ((m = line.match(RE_EXPORT_INTERFACE))) {
      symbols.push({ module, file: rel, name: m[1], kind: "interface", signature: trimSig(line), line: i + 1 });
    } else if ((m = line.match(RE_EXPORT_TYPE))) {
      symbols.push({ module, file: rel, name: m[1], kind: "type", signature: trimSig(line), line: i + 1 });
    } else if ((m = line.match(RE_EXPORT_CONST))) {
      symbols.push({ module, file: rel, name: m[1], kind: "const", signature: trimSig(line), line: i + 1 });
    }
  }
  return symbols;
}

const RE_IMPORT_SPEC = /(?:import|export)\s+[\s\S]*?from\s+["']([^"']+)["']/g;
const RE_REQUIRE = /require\(\s*["']([^"']+)["']\s*\)/g;

function collectLocalImports(src, repoRoot, importerPath, fileSet) {
  const resolved = new Set();
  const importerDir = dirname(importerPath);
  const specs = [];
  let m;
  RE_IMPORT_SPEC.lastIndex = 0;
  while ((m = RE_IMPORT_SPEC.exec(src))) specs.push(m[1]);
  RE_REQUIRE.lastIndex = 0;
  while ((m = RE_REQUIRE.exec(src))) specs.push(m[1]);
  for (const spec of specs) {
    if (!spec.startsWith(".") && !spec.startsWith("/")) continue;
    const target = resolve(importerDir, spec);
    let found = null;
    for (const ext of SOURCE_EXTENSIONS) {
      const candidate = target.endsWith(ext) ? target : target + ext;
      const rel = relPosix(repoRoot, candidate);
      if (fileSet.has(rel)) { found = rel; break; }
    }
    if (!found) {
      for (const ext of SOURCE_EXTENSIONS) {
        const candidate = join(target, "index" + ext);
        const rel = relPosix(repoRoot, candidate);
        if (fileSet.has(rel)) { found = rel; break; }
      }
    }
    if (found) resolved.add(found);
  }
  return resolved;
}

function buildFileSet(repoRoot, files) {
  const set = new Set();
  for (const f of files) set.add(relPosix(repoRoot, f));
  return set;
}

function analyzeFile(repoRoot, path, fileSet) {
  const rel = relPosix(repoRoot, path);
  const module = dirModule(repoRoot, path);
  let src;
  try {
    src = readFileSync(path, "utf8");
  } catch {
    return null;
  }
  if (src.indexOf("\u0000") !== -1) return null;
  const docstring = inferDocstring(src);
  const symbols = extractSymbols(src, rel, module);
  const importedLocalFiles = collectLocalImports(src, repoRoot, path, fileSet);
  return { path: rel, module, docstring, symbols, importedLocalFiles };
}

function inferPurpose(analysesByModule) {
  const purpose = {};
  for (const [mod, analyses] of Object.entries(analysesByModule)) {
    for (const a of analyses) {
      if (a.docstring) {
        purpose[mod] = a.docstring;
        break;
      }
    }
  }
  return purpose;
}

function isTestFile(rel) {
  const lower = rel.toLowerCase();
  if (TEST_EXTENSIONS.some((ext) => lower.endsWith(ext))) return true;
  if (lower.includes("/__tests__/") || lower.includes("/tests/") || lower.includes("/test/")) return true;
  return false;
}

/**
 * Generate a RepoMap for the TS/JS repo at `repoPath`.
 * @param {string} repoPath
 * @param {string|null} [generatedAt=null]
 * @returns {{map: Object, report: Object}}
 */
export function generate(repoPath, generatedAt = null) {
  const repoRoot = resolve(repoPath);
  const report = { repo_path: repoRoot, files_scanned: 0, files_skipped: 0, crashes: [] };

  const files = iterSourceFiles(repoRoot);
  const fileSet = buildFileSet(repoRoot, files);

  const analyses = [];
  for (const f of files) {
    report.files_scanned += 1;
    const a = analyzeFile(repoRoot, f, fileSet);
    if (!a) {
      report.crashes.push(relPosix(repoRoot, f));
      report.files_skipped += 1;
      continue;
    }
    analyses.push(a);
  }

  const byModule = {};
  for (const a of analyses) {
    (byModule[a.module] ??= []).push(a);
  }
  const purpose = inferPurpose(byModule);

  const modules = Object.entries(byModule).map(([mod, an]) => ({
    path: mod,
    language: an.length ? languageOfFile(an[0].path.split("/").pop()) : LANGUAGE_TYPESCRIPT,
    file_count: an.length,
    purpose: purpose[mod] || "",
  }));

  const symbols = [];
  for (const a of analyses) symbols.push(...a.symbols);

  const edgePairs = new Set();
  const fileToModule = {};
  for (const a of analyses) fileToModule[a.path] = a.module;
  for (const a of analyses) {
    for (const imported of a.importedLocalFiles) {
      const targetMod = fileToModule[imported];
      if (targetMod && targetMod !== a.module) {
        edgePairs.add(`${a.module}\u0001${targetMod}`);
      }
    }
  }
  const edges = [...edgePairs].map((s) => {
    const [from, to] = s.split("\u0001");
    return { from, to, kind: EDGE_IMPORT };
  });

  const importerCounts = {};
  for (const a of analyses) {
    for (const imported of a.importedLocalFiles) {
      if (fileToModule[imported] !== undefined) {
        importerCounts[imported] = (importerCounts[imported] || 0) + 1;
      }
    }
  }
  const hotFiles = Object.entries(importerCounts).map(([file, importers]) => ({ file, importers }));

  const entrypoints = [];
  for (const f of files) {
    if (ENTRYPOINT_BASENAMES.has(basename(f).toLowerCase())) {
      entrypoints.push(relPosix(repoRoot, f));
    }
  }
  const tests = files.map((f) => relPosix(repoRoot, f)).filter(isTestFile);

  let repoMap = {
    version: SCHEMA_VERSION,
    generated_at: generatedAt || nowIso(),
    repo: basename(repoRoot),
    modules,
    symbols,
    edges,
    hot_files: hotFiles,
    entrypoints,
    tests,
  };
  repoMap = canonicalize(repoMap);

  report.modules_count = repoMap.modules.length;
  report.symbols_count = repoMap.symbols.length;
  report.edges_count = repoMap.edges.length;
  report.crash_count = report.crashes.length;
  report.hot_files_top = repoMap.hot_files.slice(0, 5).map((h) => ({ file: h.file, importers: h.importers }));
  return { map: repoMap, report };
}

function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** Write map.json + map.md under <repoPath>/<outDir>. Returns [jsonPath, mdPath]. */
export function writeMap(repoMap, repoPath, outDir = ".zagent") {
  const repoRoot = resolve(repoPath);
  const out = join(repoRoot, outDir);
  mkdirSync(out, { recursive: true });
  const jsonPath = join(out, "map.json");
  const mdPath = join(out, "map.md");
  writeFileSync(jsonPath, JSON.stringify(toDict(repoMap), null, 2) + "\n", "utf8");
  writeFileSync(mdPath, renderMarkdown(repoMap), "utf8");
  return [jsonPath, mdPath];
}

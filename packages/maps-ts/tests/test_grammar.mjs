// Unit tests for the TS grammar module (node:test runner).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  SCHEMA_VERSION, canonicalize, toDict, fromDict, renderMarkdown, toPosix,
  moduleSortKey, symbolSortKey, edgeSortKey, hotFileSortKey,
} from "../src/grammar.js";

const here = dirname(fileURLToPath(import.meta.url));

test("SCHEMA_VERSION is 1", () => {
  assert.equal(SCHEMA_VERSION, 1);
});

test("toPosix normalizes backslashes", () => {
  assert.equal(toPosix("src\\pkg\\mod.ts"), "src/pkg/mod.ts");
  assert.equal(toPosix("src/pkg/mod.ts"), "src/pkg/mod.ts");
});

test("canonicalize sorts all lists and is idempotent", () => {
  const m = {
    version: 1, generated_at: "T", repo: "r",
    modules: [
      { path: "b", language: "typescript", file_count: 1, purpose: "" },
      { path: "a", language: "typescript", file_count: 2, purpose: "" },
    ],
    symbols: [
      { module: "a", file: "a/z.ts", name: "z", kind: "function", signature: "z", line: 5 },
      { module: "a", file: "a/a.ts", name: "a", kind: "class", signature: "a", line: 1 },
      { module: "a", file: "a/a.ts", name: "a", kind: "function", signature: "a", line: 1 },
    ],
    edges: [
      { from: "b", to: "a", kind: "import" },
      { from: "a", to: "b", kind: "import" },
    ],
    hot_files: [
      { file: "x.ts", importers: 1 },
      { file: "y.ts", importers: 5 },
      { file: "z.ts", importers: 5 },
    ],
    entrypoints: ["b.ts", "a.ts"],
    tests: ["t2.spec.ts", "t1.spec.ts"],
  };
  const c = canonicalize(m);
  assert.deepEqual(c.modules.map((x) => x.path), ["a", "b"]);
  assert.deepEqual(c.symbols.map((s) => [s.file, s.line, s.name, s.kind]),
    [["a/a.ts", 1, "a", "class"], ["a/a.ts", 1, "a", "function"], ["a/z.ts", 5, "z", "function"]]);
  assert.deepEqual(c.edges.map((e) => [e.from, e.to]), [["a", "b"], ["b", "a"]]);
  assert.deepEqual(c.hot_files.map((h) => [h.file, h.importers]),
    [["y.ts", 5], ["z.ts", 5], ["x.ts", 1]]);
  assert.deepEqual(c.entrypoints, ["a.ts", "b.ts"]);
  assert.deepEqual(c.tests, ["t1.spec.ts", "t2.spec.ts"]);
  // idempotent
  assert.deepEqual(toDict(canonicalize(c)), toDict(c));
});

test("toDict preserves canonical key order", () => {
  const d = toDict({ version: 1, generated_at: "T", repo: "r" });
  assert.deepEqual(Object.keys(d),
    ["version", "generated_at", "repo", "modules", "symbols", "edges", "hot_files", "entrypoints", "tests"]);
});

test("fromDict roundtrips through toDict", () => {
  const m = {
    version: 1, generated_at: "T", repo: "r",
    modules: [{ path: "a", language: "typescript", file_count: 1, purpose: "p" }],
    symbols: [{ module: "a", file: "a/x.ts", name: "X", kind: "class", signature: "class X", line: 3 }],
    edges: [{ from: "a", to: "b", kind: "import" }],
    hot_files: [{ file: "a/x.ts", importers: 2 }],
    entrypoints: ["a/main.ts"],
    tests: ["a/x.spec.ts"],
  };
  const d = toDict(canonicalize(m));
  assert.deepEqual(toDict(canonicalize(fromDict(d))), d);
});

test("fromDict tolerant of missing fields", () => {
  const m = fromDict({ version: 1, repo: "r" });
  assert.deepEqual(m.modules, []);
  assert.deepEqual(m.symbols, []);
  assert.equal(m.generated_at, "");
});

test("renderMarkdown has all sections and is deterministic", () => {
  const m = canonicalize({
    version: 1, generated_at: "2026-01-01T00:00:00Z", repo: "r",
    modules: [{ path: ".", language: "typescript", file_count: 1, purpose: "root" }],
    entrypoints: ["main.ts"],
    symbols: [{ module: ".", file: "main.ts", name: "main", kind: "function", signature: "export function main()", line: 1 }],
    edges: [{ from: ".", to: "pkg", kind: "import" }],
    hot_files: [{ file: "pkg/x.ts", importers: 3 }],
    tests: ["tests/t.spec.ts"],
  });
  const out = renderMarkdown(m);
  assert.ok(out.startsWith("# r — repo map\n"));
  for (const s of ["## Modules", "## Entrypoints", "## Symbols", "## Edges", "## Hot files", "## Tests"]) {
    assert.ok(out.includes(s), `missing section ${s}`);
  }
  assert.ok(out.includes("`pkg/x.ts` (3 importers)"));
  assert.equal(renderMarkdown(m), out);
});

test("renderMarkdown handles empty sections", () => {
  const out = renderMarkdown(canonicalize({ version: 1, generated_at: "", repo: "r" }));
  assert.ok(out.includes("- _(none detected)_"));
});

test("sort key helpers are stable", () => {
  assert.deepEqual(moduleSortKey({ path: "a" }), ["a"]);
  assert.deepEqual(symbolSortKey({ file: "f", line: 1, name: "n", kind: "k" }), ["f", 1, "n", "k"]);
  assert.deepEqual(edgeSortKey({ from: "a", to: "b", kind: "import" }), ["a", "b", "import"]);
  assert.deepEqual(hotFileSortKey({ file: "a.ts", importers: 3 }), [-3, "a.ts"]);
});

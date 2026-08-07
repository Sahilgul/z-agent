// Unit tests for the TS citation linter (node:test runner).
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

import { parseCitation, lint, lintCitation, renderReport } from "../src/lint.js";

function smallRepo() {
  const tmp = mkdtempSync(join(tmpdir(), "collegium-ts-lint-"));
  writeFileSync(join(tmp, "mod.ts"),
    "/** Mod. */\nexport class C {\n  run(): void {}\n}\nexport function f(a: number, b: number): number {\n  return a + b;\n}\n",
    "utf8");
  writeFileSync(join(tmp, "empty_lines.ts"), "first\n\nthird\n", "utf8");
  // invalid UTF-8 so readFileSync utf8 throws -> unreadable
  writeFileSync(join(tmp, "bin.dat"), Buffer.from([0x00, 0xff, 0xfe, 0x62, 0x69, 0x6e]));
  mkdirSync(join(tmp, "dir"));
  return tmp;
}

test("parseCitation single line", () => {
  const c = parseCitation("mod.ts:3");
  assert.equal(c.path, "mod.ts");
  assert.equal(c.line_start, 3);
  assert.equal(c.line_end, 3);
});

test("parseCitation line range", () => {
  const c = parseCitation("mod.ts:3-5");
  assert.equal(c.line_start, 3);
  assert.equal(c.line_end, 5);
});

test("parseCitation windows drive path", () => {
  const c = parseCitation("C:\\foo\\bar.ts:12");
  assert.equal(c.path, "C:\\foo\\bar.ts");
  assert.equal(c.line_start, 12);
});

test("parseCitation rejects malformed", () => {
  for (const bad of ["", "mod.ts", "mod.ts:", ":3", "mod.ts:abc", "mod.ts:3-", "mod.ts:5-3", "mod.ts:0", "   "]) {
    assert.equal(parseCitation(bad), null, `expected null for ${JSON.stringify(bad)}`);
  }
});

test("parseCitation strips whitespace", () => {
  const c = parseCitation("  mod.ts:3  ");
  assert.equal(c.path, "mod.ts");
  assert.equal(c.line_start, 3);
});

test("lintCitation valid", () => {
  const repo = smallRepo();
  const r = lintCitation(repo, parseCitation("mod.ts:3"));
  assert.equal(r.ok, true);
  assert.equal(r.error, "");
});

test("lintCitation valid range", () => {
  const repo = smallRepo();
  const r = lintCitation(repo, parseCitation("mod.ts:3-6"));
  assert.equal(r.ok, true);
});

test("lintCitation file not found", () => {
  const repo = smallRepo();
  const r = lintCitation(repo, parseCitation("missing.ts:1"));
  assert.equal(r.ok, false);
  assert.equal(r.error, "file_not_found");
});

test("lintCitation line out of range", () => {
  const repo = smallRepo();
  const r = lintCitation(repo, parseCitation("mod.ts:999"));
  assert.equal(r.ok, false);
  assert.equal(r.error, "line_out_of_range");
  assert.ok(r.detail.includes("file has"));
});

test("lintCitation empty line", () => {
  const repo = smallRepo();
  const r = lintCitation(repo, parseCitation("empty_lines.ts:2"));
  assert.equal(r.ok, false);
  assert.equal(r.error, "empty_line");
});

test("lintCitation unreadable binary file", () => {
  const repo = smallRepo();
  const r = lintCitation(repo, parseCitation("bin.dat:1"));
  assert.equal(r.ok, false);
  assert.equal(r.error, "unreadable_file");
});

test("lintCitation directory not a file", () => {
  const repo = smallRepo();
  const r = lintCitation(repo, parseCitation("dir:1"));
  assert.equal(r.ok, false);
  assert.equal(r.error, "not_a_file");
});

test("lint batch counts", () => {
  const repo = smallRepo();
  const report = lint(repo, ["mod.ts:3", "mod.ts:999", "missing.ts:1", "mod.ts:abc", "empty_lines.ts:2"]);
  assert.equal(report.total, 5);
  assert.equal(report.valid, 1);
  assert.equal(report.invalid, 4);
  assert.equal(report.passed, false);
  assert.equal(report.results[0].ok, true);
  assert.equal(report.results[1].error, "line_out_of_range");
  assert.equal(report.results[2].error, "file_not_found");
  assert.equal(report.results[3].error, "malformed_citation");
  assert.equal(report.results[4].error, "empty_line");
});

test("lint all valid passes", () => {
  const repo = smallRepo();
  const report = lint(repo, ["mod.ts:3", "mod.ts:4", "mod.ts:6"]);
  assert.equal(report.valid, 3);
  assert.equal(report.invalid, 0);
  assert.equal(report.passed, true);
});

test("renderReport lists invalid", () => {
  const repo = smallRepo();
  const out = renderReport(lint(repo, ["mod.ts:3", "missing.ts:1"]));
  assert.ok(out.includes("citation lint report"));
  assert.ok(out.includes("passed=false"));
  assert.ok(out.includes("OK "));
  assert.ok(out.includes("XX "));
});

test("cli lint exits non-zero on invalid", () => {
  const repo = smallRepo();
  const citsFile = join(repo, "cits.json");
  writeFileSync(citsFile, JSON.stringify(["mod.ts:999", "missing.ts:1"]), "utf8");
  const cli = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "cli.js");
  const r = spawnSync(process.execPath, [cli, "lint", repo, citsFile], { encoding: "utf8" });
  assert.equal(r.status, 1);
  assert.ok(r.stdout.includes("invalid=2"));
});

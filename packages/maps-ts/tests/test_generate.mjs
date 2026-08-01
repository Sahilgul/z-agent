// Unit tests for the TS generator (node:test runner). Includes golden-fixture parity.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync, mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

import { generate, writeMap } from "../src/generate.js";
import { toDict } from "../src/grammar.js";

const here = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(here, "fixtures", "ts_repo");
const GOLDEN = join(here, "fixtures", "expected", "ts_repo_map.json");
const PINNED_AT = "2026-01-01T00:00:00Z";

function readJson(p) {
  return JSON.parse(readFileSync(p, "utf8"));
}

test("generate matches golden byte-for-byte", () => {
  const { map } = generate(FIXTURE, PINNED_AT);
  const actual = JSON.stringify(toDict(map), null, 2) + "\n";
  const expected = readFileSync(GOLDEN, "utf8");
  assert.equal(actual, expected,
    "Generated map.json diverged from the committed golden fixture. " +
    "Re-run packages/maps-ts/scripts/gen_golden_ts.mjs if the change is intended.");
});

test("generate report stats for fixture", () => {
  const { report } = generate(FIXTURE, PINNED_AT);
  assert.equal(report.files_scanned, 5);
  assert.equal(report.files_skipped, 0);
  assert.equal(report.crash_count, 0);
  assert.equal(report.modules_count, 3);
  assert.equal(report.symbols_count, 7);
  assert.equal(report.edges_count, 2);
  assert.deepEqual(report.hot_files_top[0], { file: "src/pkg/core.ts", importers: 2 });
});

test("generate is deterministic across runs", () => {
  const a = toDict(generate(FIXTURE, PINNED_AT).map);
  const b = toDict(generate(FIXTURE, PINNED_AT).map);
  assert.deepEqual(a, b);
});

test("generated_at defaults to ISO when not pinned", () => {
  const { map } = generate(FIXTURE);
  assert.ok(map.generated_at.endsWith("Z"));
  assert.ok(map.generated_at.includes("T"));
});

test("repo name is repo basename", () => {
  const { map } = generate(FIXTURE, PINNED_AT);
  assert.equal(map.repo, "ts_repo");
});

test("syntax/binary file recorded as crash, not thrown", () => {
  const tmp = mkdtempSync(join(tmpdir(), "zagent-ts-crash-"));
  mkdirSync(join(tmp, "src"), { recursive: true });
  writeFileSync(join(tmp, "src", "good.ts"), "export function f(): number { return 1; }\n");
  // binary content with a NUL byte triggers the unreadable path
  writeFileSync(join(tmp, "src", "bin.ts"), Buffer.from([0x00, 0x01, 0x02, 0x62, 0x69, 0x6e]));
  const { report } = generate(tmp, PINNED_AT);
  assert.equal(report.crash_count, 1);
  assert.ok(report.crashes.includes("src/bin.ts"));
});

test("empty repo produces empty map", () => {
  const tmp = mkdtempSync(join(tmpdir(), "zagent-ts-empty-"));
  const { map, report } = generate(tmp, PINNED_AT);
  assert.deepEqual(map.modules, []);
  assert.deepEqual(map.symbols, []);
  assert.deepEqual(map.edges, []);
  assert.deepEqual(map.hot_files, []);
  assert.deepEqual(map.entrypoints, []);
  assert.deepEqual(map.tests, []);
  assert.equal(report.files_scanned, 0);
});

test("iterSourceFiles skips node_modules and dist", () => {
  const tmp = mkdtempSync(join(tmpdir(), "zagent-ts-ignore-"));
  mkdirSync(join(tmp, "node_modules"), { recursive: true });
  writeFileSync(join(tmp, "node_modules", "skip.ts"), "export const x = 1;\n");
  mkdirSync(join(tmp, "dist"));
  writeFileSync(join(tmp, "dist", "skip.js"), "module.exports = 1;\n");
  writeFileSync(join(tmp, "keep.ts"), "export const y = 2;\n");
  const { report } = generate(tmp, PINNED_AT);
  assert.equal(report.files_scanned, 1);
  assert.equal(report.crash_count, 0);
});

test("writeMap writes json and md and creates out dir", () => {
  const tmp = mkdtempSync(join(tmpdir(), "zagent-ts-write-"));
  mkdirSync(join(tmp, "src"));
  writeFileSync(join(tmp, "src", "index.ts"), "/** entry. */\nexport function main(): void {}\n");
  const { map } = generate(tmp, PINNED_AT);
  const [jp, mp] = writeMap(map, tmp, ".zagent");
  assert.ok(existsSync(jp));
  assert.ok(existsSync(mp));
  const data = readJson(jp);
  assert.equal(data.repo, tmp.split(/[\\/]/).pop());
  const md = readFileSync(mp, "utf8");
  assert.ok(md.startsWith(`# ${data.repo} — repo map\n`));
});

test("cli generate writes files (smoke)", () => {
  const tmp = mkdtempSync(join(tmpdir(), "zagent-ts-cli-"));
  mkdirSync(join(tmp, "src"));
  writeFileSync(join(tmp, "src", "index.ts"), "/** entry. */\nexport function main(): void {}\n");
  const cli = join(here, "..", "src", "cli.js");
  const r = spawnSync(process.execPath, [cli, tmp, "--out", "mapout"], { encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr || r.stdout);
  assert.ok(existsSync(join(tmp, "mapout", "map.json")));
  assert.ok(existsSync(join(tmp, "mapout", "map.md")));
});

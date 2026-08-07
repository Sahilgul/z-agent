// Sanity check: run the TS generator against a real repo checkout.
// Does NOT write .collegium/ into the repo — writes map.json/map.md to a temp
// dir and prints module count, top-5 hot files, and crash count.
// Point SANITY_REPO at any local checkout (default: the golden tree layout).
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { generate } from "../src/generate.js";
import { toDict, renderMarkdown } from "../src/grammar.js";

const REPO = process.env.SANITY_REPO || "./golden/repos/ServerApp";

function main() {
  const { map, report } = generate(REPO);
  const tmp = mkdtempSync(join(tmpdir(), "collegium-sanity-ts-"));
  writeFileSync(join(tmp, "ServerApp_map.json"), JSON.stringify(toDict(map), null, 2) + "\n", "utf8");
  writeFileSync(join(tmp, "ServerApp_map.md"), renderMarkdown(map), "utf8");

  console.log("=== ServerApp (TypeScript generator) ===");
  console.log(`repo: ${REPO}`);
  console.log(`output dir: ${tmp}`);
  console.log(`files_scanned: ${report.files_scanned}`);
  console.log(`files_skipped: ${report.files_skipped}`);
  console.log(`crash_count: ${report.crash_count}`);
  console.log(`crashes (first 10): ${JSON.stringify(report.crashes.slice(0, 10))}`);
  console.log(`modules_count: ${report.modules_count}`);
  console.log(`symbols_count: ${report.symbols_count}`);
  console.log(`edges_count: ${report.edges_count}`);
  console.log("hot_files_top5:");
  for (const h of report.hot_files_top) {
    console.log(`  - ${h.file} (${h.importers} importers)`);
  }
  console.log("entrypoints:");
  for (const ep of map.entrypoints) {
    console.log(`  - ${ep}`);
  }
}

main();

// CLI entrypoint for the TS repo-map generator + citation linter.
//
//   node packages/maps-ts/src/cli.js <repo_path> [--out .collegium] [--report]
//   node packages/maps-ts/src/cli.js lint <repo_path> <citations.json> [--json]

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { generate, writeMap } from "./generate.js";
import { lint, renderReport } from "./lint.js";

function loadCitationsFile(path) {
  const data = JSON.parse(readFileSync(path, "utf8"));
  if (Array.isArray(data)) return data.map(String);
  if (data && Array.isArray(data.citations)) return data.citations.map(String);
  throw new Error(
    "citations file must be a JSON array of strings or an object with a 'citations' array"
  );
}

function main(argv) {
  argv = argv || process.argv.slice(2);
  if (argv.length === 0) {
    console.error("usage: cli.js <repo_path> [--out .collegium] [--report]");
    console.error("       cli.js lint <repo_path> <citations.json> [--json]");
    return 2;
  }
  if (argv[0] === "lint") {
    const rest = argv.slice(1);
    if (rest.length < 2) {
      console.error("usage: cli.js lint <repo_path> <citations.json> [--json]");
      return 2;
    }
    const repoPath = rest[0];
    const citationsFile = rest[1];
    const asJson = rest.includes("--json");
    const citations = loadCitationsFile(resolve(citationsFile));
    const report = lint(repoPath, citations);
    if (asJson) {
      console.log(JSON.stringify(report, null, 2));
    } else {
      process.stdout.write(renderReport(report));
    }
    return report.passed ? 0 : 1;
  }

  // generate (default)
  const rest = argv;
  const repoPath = rest[0];
  let outDir = ".collegium";
  const outIdx = rest.indexOf("--out");
  if (outIdx !== -1 && rest[outIdx + 1]) outDir = rest[outIdx + 1];
  const wantReport = rest.includes("--report");

  const { map, report } = generate(repoPath);
  writeMap(map, repoPath, outDir);
  if (wantReport) {
    console.error(JSON.stringify(report, null, 2));
  }
  return 0;
}

const code = main();
process.exit(code);

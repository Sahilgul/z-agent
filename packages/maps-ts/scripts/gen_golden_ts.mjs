// Generate the golden map.json for the TS fixture repo.
// Run once to (re)generate the committed golden fixture. Pinned generated_at.
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { writeFileSync, mkdirSync } from "node:fs";

import { generate } from "../src/generate.js";
import { toDict } from "../src/grammar.js";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = join(here, "..", "tests", "fixtures", "ts_repo");
const expected = join(here, "..", "tests", "fixtures", "expected", "ts_repo_map.json");
mkdirSync(dirname(expected), { recursive: true });

const { map, report } = generate(fixture, "2026-01-01T00:00:00Z");
writeFileSync(expected, JSON.stringify(toDict(map), null, 2) + "\n", "utf8");
console.log("wrote", expected);
console.log("report:", JSON.stringify(report, null, 2));

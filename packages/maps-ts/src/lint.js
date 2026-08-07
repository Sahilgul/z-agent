// Citation linter (TypeScript half) — drift detection for agent file:line citations.
//
// Mirrors the Python linter in `packages/maps/src/collegium_maps/lint.py`. Verifies
// that an agent's `path:line` / `path:line-line` citations actually exist in the
// repo right now: file exists, line number in range, and (best effort) the
// cited line is not empty/whitespace-only. Pure stdlib (node:fs), never throws
// on a single bad citation — every citation is checked and failures collected.

import { existsSync, readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";

/**
 * @typedef {Object} Citation
 * @property {string} raw
 * @property {string} path
 * @property {number} line_start
 * @property {number} line_end
 */

/**
 * @typedef {Object} CitationResult
 * @property {string} citation
 * @property {boolean} ok
 * @property {string} error
 * @property {string} detail
 */

/**
 * @typedef {Object} LintReport
 * @property {string} repo_path
 * @property {number} total
 * @property {number} valid
 * @property {number} invalid
 * @property {boolean} passed
 * @property {CitationResult[]} results
 */

/** Parse a `path:line` or `path:line-line` citation. Returns null if malformed. */
export function parseCitation(raw) {
  if (typeof raw !== "string") return null;
  raw = raw.trim();
  if (!raw) return null;
  // The line separator is the LAST colon so Windows drive-prefixed paths
  // (C:\foo\bar.py:12) parse correctly.
  const idx = raw.lastIndexOf(":");
  if (idx === -1) return null;
  const pathPart = raw.slice(0, idx).trim();
  const linePart = raw.slice(idx + 1).trim();
  if (!pathPart || !linePart) return null;
  let start, end;
  if (linePart.includes("-")) {
    const [a, b] = linePart.split("-");
    const as = (a || "").trim();
    const bs = (b || "").trim();
    if (!/^\d+$/.test(as) || !/^\d+$/.test(bs)) return null;
    start = parseInt(as, 10);
    end = parseInt(bs, 10);
    if (end < start) return null;
  } else {
    if (!/^\d+$/.test(linePart)) return null;
    start = end = parseInt(linePart, 10);
  }
  if (start < 1) return null;
  return { raw, path: pathPart, line_start: start, line_end: end };
}

function readLines(repoRoot, path) {
  const full = resolve(repoRoot, path);
  try {
    const buf = readFileSync(full);
    // A NUL byte is a strong binary signal — treat as unreadable, mirroring
    // the Python linter (which uses strict UTF-8 and raises on binary).
    if (buf.indexOf(0x00) !== -1) return null;
    return buf.toString("utf8").split("\n");
  } catch {
    return null;
  }
}

/** Verify one parsed citation against the repo at `repoRoot`. */
export function lintCitation(repoRoot, citation) {
  const full = resolve(repoRoot, citation.path);
  if (!existsSync(full)) {
    return { citation: citation.raw, ok: false, error: "file_not_found",
      detail: `no such file: ${citation.path}` };
  }
  let st;
  try {
    st = statSync(full);
  } catch {
    return { citation: citation.raw, ok: false, error: "file_not_found",
      detail: `no such file: ${citation.path}` };
  }
  if (!st.isFile()) {
    return { citation: citation.raw, ok: false, error: "not_a_file",
      detail: `not a file: ${citation.path}` };
  }
  const lines = readLines(repoRoot, citation.path);
  if (lines === null) {
    return { citation: citation.raw, ok: false, error: "unreadable_file",
      detail: `could not read as text: ${citation.path}` };
  }
  const n = lines.length;
  if (citation.line_start > n || citation.line_end > n) {
    return { citation: citation.raw, ok: false, error: "line_out_of_range",
      detail: `file has ${n} line(s); cited ${citation.line_start}-${citation.line_end}` };
  }
  const citedLine = lines[citation.line_start - 1];
  if (citedLine.trim() === "") {
    return { citation: citation.raw, ok: false, error: "empty_line",
      detail: `line ${citation.line_start} is empty/whitespace-only` };
  }
  return { citation: citation.raw, ok: true, error: "",
    detail: `lines ${citation.line_start}-${citation.line_end} of ${citation.path}` };
}

/** Lint a batch of raw citation strings against the repo at `repoPath`. */
export function lint(repoPath, citations) {
  const repoRoot = resolve(repoPath);
  const report = { repo_path: repoRoot, total: 0, valid: 0, invalid: 0, results: [] };
  for (const raw of citations) {
    report.total += 1;
    const parsed = parseCitation(String(raw));
    if (!parsed) {
      report.invalid += 1;
      report.results.push({ citation: String(raw), ok: false,
        error: "malformed_citation",
        detail: "expected 'path:line' or 'path:line-line'" });
      continue;
    }
    const result = lintCitation(repoRoot, parsed);
    report.results.push(result);
    if (result.ok) report.valid += 1;
    else report.invalid += 1;
  }
  report.passed = report.invalid === 0;
  return report;
}

/** Render a human-readable lint report. */
export function renderReport(report) {
  const lines = [];
  lines.push(`citation lint report for ${report.repo_path}`);
  lines.push(`total=${report.total} valid=${report.valid} invalid=${report.invalid} passed=${report.passed}`);
  if (report.results.length) {
    lines.push("");
    for (const r of report.results) {
      const mark = r.ok ? "OK " : "XX ";
      const extra = r.ok ? ` ${r.detail}` : ` [${r.error}] ${r.detail}`;
      lines.push(`  ${mark}${r.citation}${extra}`);
    }
  }
  return lines.join("\n") + "\n";
}

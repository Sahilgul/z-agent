"""Citation linter — drift detection for agent file:line citations.

Agents answering questions about a repo cite evidence as ``path:line`` or
``path:line-line``. This module verifies those citations actually exist in the
repo *right now*: the file exists, the line number is in range, and (best
effort) the cited line is not empty/whitespace-only. Stale citations signal
that the repo has drifted under the agent — the answer must be re-grounded
before surfacing to a user.

Pure stdlib, never raises on a single bad citation: every citation is checked
and the failures are collected into a :class:`LintReport`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from collegium_maps.grammar import to_posix


@dataclass
class Citation:
    """A single parsed citation ``path:line`` or ``path:line-line``."""

    raw: str
    path: str
    line_start: int
    line_end: int


@dataclass
class CitationResult:
    """Per-citation lint outcome."""

    citation: str
    ok: bool
    error: str = ""
    detail: str = ""


@dataclass
class LintReport:
    """Aggregate lint report for a batch of citations."""

    repo_path: str
    total: int = 0
    valid: int = 0
    invalid: int = 0
    results: list[CitationResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.invalid == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "total": self.total,
            "valid": self.valid,
            "invalid": self.invalid,
            "passed": self.passed,
            "results": [
                {
                    "citation": r.citation,
                    "ok": r.ok,
                    "error": r.error,
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }


def parse_citation(raw: str) -> Citation | None:
    """Parse a ``path:line`` or ``path:line-line`` citation.

    Returns ``None`` (a parse failure) for malformed input. A Windows drive
    letter colon (``C:\\...``) is handled by treating the LAST colon as the
    line separator, since paths may contain colons only as the drive prefix.
    """
    raw = raw.strip()
    if not raw:
        return None
    # The line separator is the LAST colon in the string so that Windows
    # drive-prefixed paths (C:\foo\bar.py:12) parse correctly.
    idx = raw.rfind(":")
    if idx == -1:
        return None
    path_part = raw[:idx]
    line_part = raw[idx + 1:]
    path_part = path_part.strip()
    line_part = line_part.strip()
    if not path_part or not line_part:
        return None
    if "-" in line_part:
        a, _, b = line_part.partition("-")
        a, b = a.strip(), b.strip()
        if not a.isdigit() or not b.isdigit():
            return None
        start, end = int(a), int(b)
        if end < start:
            return None
    else:
        if not line_part.isdigit():
            return None
        start = end = int(line_part)
    if start < 1:
        return None
    return Citation(raw=raw, path=path_part, line_start=start, line_end=end)


def _read_lines(repo_root: Path, path: str) -> list[str] | None:
    """Read a file's lines, or return None if unreadable (binary / missing)."""
    full = repo_root / path
    try:
        text = full.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None
    return text.splitlines()


def lint_citation(repo_root: Path, citation: Citation) -> CitationResult:
    """Verify one parsed citation against the repo at ``repo_root``."""
    full = repo_root / citation.path
    if not full.exists():
        return CitationResult(citation=citation.raw, ok=False,
                             error="file_not_found",
                             detail=f"no such file: {citation.path}")
    if not full.is_file():
        return CitationResult(citation=citation.raw, ok=False,
                             error="not_a_file",
                             detail=f"not a file: {citation.path}")
    lines = _read_lines(repo_root, citation.path)
    if lines is None:
        # File exists but unreadable as text (binary). We still validate the
        # line range structurally but cannot check content.
        return CitationResult(citation=citation.raw, ok=False,
                              error="unreadable_file",
                              detail=f"could not read as text: {citation.path}")
    n = len(lines)
    if citation.line_start > n or citation.line_end > n:
        return CitationResult(citation=citation.raw, ok=False,
                             error="line_out_of_range",
                             detail=f"file has {n} line(s); cited {citation.line_start}-{citation.line_end}")
    # Best-effort: cited start line must not be empty/whitespace-only.
    cited_line = lines[citation.line_start - 1]
    if cited_line.strip() == "":
        return CitationResult(citation=citation.raw, ok=False,
                              error="empty_line",
                              detail=f"line {citation.line_start} is empty/whitespace-only")
    return CitationResult(citation=citation.raw, ok=True,
                          detail=f"lines {citation.line_start}-{citation.line_end} of {citation.path}")


def lint(repo_path: str | os.PathLike[str], citations: Iterable[str]) -> LintReport:
    """Lint a batch of raw citation strings against the repo at ``repo_path``."""
    repo_root = Path(repo_path).resolve()
    report = LintReport(repo_path=str(repo_root))
    for raw in citations:
        report.total += 1
        parsed = parse_citation(str(raw))
        if parsed is None:
            report.invalid += 1
            report.results.append(CitationResult(
                citation=str(raw), ok=False,
                error="malformed_citation",
                detail="expected 'path:line' or 'path:line-line'",
            ))
            continue
        result = lint_citation(repo_root, parsed)
        report.results.append(result)
        if result.ok:
            report.valid += 1
        else:
            report.invalid += 1
    return report


def load_citations_file(path: str | os.PathLike[str]) -> list[str]:
    """Load citations from a JSON file: either a JSON array of strings or an
    object with a ``citations`` array. Raises ``ValueError`` on bad shape.
    """
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict) and isinstance(data.get("citations"), list):
        return [str(x) for x in data["citations"]]
    raise ValueError(
        "citations file must be a JSON array of strings or an object with a "
        "'citations' array"
    )


def render_report(report: LintReport) -> str:
    """Render a human-readable lint report (printed to stdout by the CLI)."""
    lines: list[str] = []
    lines.append(f"citation lint report for {report.repo_path}")
    lines.append(f"total={report.total} valid={report.valid} invalid={report.invalid} "
                 f"passed={report.passed}")
    if report.results:
        lines.append("")
        for r in report.results:
            mark = "OK " if r.ok else "XX "
            extra = f" [{r.error}] {r.detail}" if not r.ok else f" {r.detail}"
            lines.append(f"  {mark}{r.citation}{extra}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m collegium_maps.lint <repo_path> <citations.json>``."""
    parser = argparse.ArgumentParser(prog="collegium-maps-lint", description=__doc__)
    parser.add_argument("repo_path", help="Path to the repo to lint citations against.")
    parser.add_argument("citations", help="JSON file of citations (array or {citations:[]}).")
    parser.add_argument("--json", action="store_true",
                        help="Emit the report as JSON instead of human-readable text.")
    args = parser.parse_args(argv)

    citations = load_citations_file(args.citations)
    report = lint(args.repo_path, citations)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(render_report(report), end="")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

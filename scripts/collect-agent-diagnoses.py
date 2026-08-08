#!/usr/bin/env python3
"""Collect the full final report from each audit subagent transcript and
paste them into the round's diagnosis markdown under the matching agent slot.

Rounds (10 agents per section):
  seam30 (default) — worker/backend/integration audit, 30 slots total
                     → 30-agents-diagnosis.md   (`agentN:` slots, 3 sections)
  web              — web frontend audit, 10 slots
                     → web-harness-diagnosis.md (`agentWN:` slots, 1 section)

Extraction rule (per round):
  - "last":    the LAST non-prompt text block. Background audit agents deliver
               the full report as their final message, while intermediate
               working messages can be even longer — so "longest" misfires.
               Falls back to the longest block when the last one is a short
               pointer (< MIN_REPORT_CHARS).
  - "longest": the longest non-prompt text block (the seam30 round, whose
               final responses were sometimes short pointers like "report
               delivered above", so the last message could not be trusted).
Task prompts are excluded via their `<timestamp>` wrapper.

Dedup rule: when a slot has multiple transcripts (errored/aborted retries),
the transcript with the longest report wins.

The output file is rebuilt from its own skeleton (title, section headers,
separator lines, agent labels); previously inserted report bodies — including
any markdown headings inside them — are dropped, so reruns are idempotent.
Manual notes inside a slot are NOT preserved across reruns. If the target
file is missing or empty, a fresh skeleton is generated first.

Usage:
    python3 scripts/collect-agent-diagnoses.py [--round seam30|web] [--dry-run]
    python3 scripts/collect-agent-diagnoses.py --round web --subagents-dir PATH
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPTS_ROOT = (
    Path.home()
    / ".cursor/projects/home-sahil-My-Projects-Portfolio-z-agent"
    / "agent-transcripts"
)

MIN_REPORT_CHARS = 1500
SEPARATOR_RE = re.compile(r"^-{5,}\s*$")


@dataclass(frozen=True)
class Round:
    name: str
    chat_id: str
    template: str
    patterns: tuple[tuple[str, re.Pattern], ...]
    section_headers: dict[str, str]
    label_re: re.Pattern
    label_prefix: str
    sections: tuple[str, ...]
    agents_per_section: int
    extract: str  # "last" | "longest"
    skeleton_title: str

    @property
    def subagents_dir(self) -> Path:
        return TRANSCRIPTS_ROOT / self.chat_id / "subagents"

    @property
    def template_path(self) -> Path:
        return REPO_ROOT / self.template


ROUNDS = {
    "seam30": Round(
        name="seam30",
        chat_id="882d6fd1-b35c-48e5-82fb-1e160d8b99e9",
        template="30-agents-diagnosis.md",
        patterns=(
            ("worker", re.compile(r"Agent (\d+) of 10 auditing the Collegium worker harness")),
            ("backend", re.compile(r"Agent (\d+) of 10 auditing the Collegium BACKEND")),
            ("integration", re.compile(r"auditor A(\d+) in a 10-agent audit of the INTEGRATION BOUNDARY")),
        ),
        section_headers={
            "10 worker parallel agents diagnosis": "worker",
            "10 backend parallel agents diagnosis": "backend",
            "10 backend & worker combine parallel agents diagnosis": "integration",
        },
        label_re=re.compile(r"^agent(\d+):\s*$"),
        label_prefix="agent",
        sections=("worker", "backend", "integration"),
        agents_per_section=10,
        extract="longest",
        skeleton_title="# 30 Agents Diagnosis",
    ),
    "web": Round(
        name="web",
        chat_id="fd29836c-17b7-41c2-9e94-ba3517748ed0",
        template="web-harness-diagnosis.md",
        patterns=(
            ("web", re.compile(r"agent W(\d+) of 10 parallel agents auditing the \*\*web frontend\*\*")),
        ),
        section_headers={
            "10 web parallel agents diagnosis": "web",
        },
        label_re=re.compile(r"^agentW(\d+):\s*$"),
        label_prefix="agentW",
        sections=("web",),
        agents_per_section=10,
        extract="last",
        skeleton_title="# Web Harness Diagnosis — 10 Agents",
    ),
}


def iter_text_blocks(obj):
    """Yield every text block found anywhere in a transcript event."""
    if isinstance(obj, dict):
        if obj.get("type") == "text" and isinstance(obj.get("text"), str):
            yield obj["text"]
        for value in obj.values():
            yield from iter_text_blocks(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from iter_text_blocks(value)


def transcript_texts(path: Path) -> list[str]:
    blocks: list[str] = []
    with path.open() as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            blocks.extend(iter_text_blocks(event))
    return blocks


def identify(prompt: str, round_: Round) -> tuple[str, int] | None:
    for section, pattern in round_.patterns:
        match = pattern.search(prompt)
        if match:
            return section, int(match.group(1))
    return None


def pick_report(candidates: list[str], strategy: str) -> str:
    if strategy == "last":
        report = candidates[-1]
        if len(report) >= MIN_REPORT_CHARS:
            return report
    return max(candidates, key=len)


def collect_reports(round_: Round, subagents_dir: Path) -> dict[tuple[str, int], tuple[str, str, int]]:
    """Return {(section, agent_num): (report_text, transcript_id, char_count)}."""
    best: dict[tuple[str, int], tuple[str, str]] = {}
    for path in sorted(subagents_dir.glob("*.jsonl")):
        texts = transcript_texts(path)
        if not texts:
            continue
        prompt_idx = next(
            (i for i, t in enumerate(texts) if t.lstrip().startswith("<timestamp>")),
            None,
        )
        if prompt_idx is None:
            continue
        slot = identify(texts[prompt_idx], round_)
        if slot is None:
            continue
        candidates = [
            t
            for i, t in enumerate(texts)
            if i != prompt_idx and not t.lstrip().startswith("<timestamp>")
        ]
        if not candidates:
            continue
        report = pick_report(candidates, round_.extract).strip()
        current = best.get(slot)
        if current is None or len(report) > len(current[0]):
            best[slot] = (report, path.stem)
    return {slot: (report, tid, len(report)) for slot, (report, tid) in best.items()}


def build_skeleton(round_: Round) -> list[str]:
    lines = [round_.skeleton_title, ""]
    for header in round_.section_headers:
        lines.append(header)
        lines.append("")
        for num in range(1, round_.agents_per_section + 1):
            lines.append(f"{round_.label_prefix}{num}:")
            lines.append("")
        lines.append("-----")
        lines.append("")
    return lines


def rebuild(template_lines: list[str], reports: dict, round_: Round) -> tuple[list[str], list[str]]:
    out: list[str] = []
    warnings: list[str] = []
    section: str | None = None
    skipping_body = False

    for line in template_lines:
        stripped = line.strip()

        if stripped in round_.section_headers:
            section = round_.section_headers[stripped]
            skipping_body = False
            out.append(line)
            continue

        if SEPARATOR_RE.match(stripped):
            section = None
            skipping_body = False
            out.append(line)
            continue

        label = round_.label_re.match(stripped)
        if label and section is not None:
            num = int(label.group(1))
            out.append(line)
            out.append("")
            slot = (section, num)
            if slot in reports:
                out.append(reports[slot][0])
            else:
                out.append(f"_(no report found for {section} agent {num})_")
                warnings.append(f"MISSING: {section} agent {num}")
            out.append("")
            skipping_body = True
            continue

        # Previously inserted report body: drop it (markdown headings inside
        # reports included — keeping them broke idempotency on reruns).
        if skipping_body:
            continue

        # Skeleton lines (title, blanks, stray notes outside slots) are kept.
        out.append(line)

    return out, warnings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect audit subagent reports into the round's diagnosis markdown.",
    )
    parser.add_argument(
        "--round",
        choices=sorted(ROUNDS),
        default="seam30",
        help="which audit round to collect (default: seam30)",
    )
    parser.add_argument(
        "--subagents-dir",
        type=Path,
        default=None,
        help="override the transcripts subagents directory",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    round_ = ROUNDS[args.round]
    subagents_dir = args.subagents_dir or round_.subagents_dir
    template = round_.template_path

    if not subagents_dir.is_dir():
        print(f"error: subagents dir not found: {subagents_dir}")
        return 1

    reports = collect_reports(round_, subagents_dir)

    total_slots = len(round_.sections) * round_.agents_per_section
    print(f"{'slot':<16}{'chars':>8}  transcript")
    for section in round_.sections:
        for num in range(1, round_.agents_per_section + 1):
            slot = (section, num)
            if slot in reports:
                _, tid, chars = reports[slot]
                print(f"{section + ' ' + str(num):<16}{chars:>8}  {tid[:8]}")
            else:
                print(f"{section + ' ' + str(num):<16}{'—':>8}  MISSING")

    filled = sum(
        1
        for section in round_.sections
        for num in range(1, round_.agents_per_section + 1)
        if (section, num) in reports
    )
    print(f"\nfilled {filled}/{total_slots} slots")

    if template.is_file() and template.read_text().strip():
        template_lines = template.read_text().splitlines()
    else:
        template_lines = build_skeleton(round_)
        print(f"note: {template.name} missing or empty — generated a fresh skeleton")

    new_lines, warnings = rebuild(template_lines, reports, round_)
    for warning in warnings:
        print(f"warning: {warning}")

    if args.dry_run:
        print("dry run: file not written")
        return 0

    template.write_text("\n".join(new_lines).rstrip() + "\n")
    print(f"wrote {template}")
    return 0 if not warnings else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Append-only JSONL transcript per run — the portable session record.

The events table stays the queryable source of truth; this is the flat mirror:
one JSON object per line, appended in ingest order, so a session can be opened,
exported, diffed, or fed to another tool without a database. Lines are written
under ``transcripts_dir`` and NOT under ``sessions_dir``, because the session
volume is purged at 30 days (replay-only decay) while the transcript should
outlive it — it decays with the events TTL instead.

Writes are best-effort: a transcript failure must never lose an event that the
database already committed, so callers log and continue.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from app.core.config import get_settings


def transcript_path(run_id: str) -> Path:
    """<transcripts_dir>/<run_id>.jsonl — created lazily on first append."""
    settings = get_settings()
    settings.transcripts_dir.mkdir(parents=True, exist_ok=True)
    return settings.transcripts_dir / f"{run_id}.jsonl"


def append(run_id: str, record: dict) -> None:
    """Append one event as a single JSON line. Newlines inside payloads are
    escaped by json.dumps, so the one-object-per-line contract holds."""
    path = transcript_path(run_id)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read(run_id: str, after_seq: int | None = None) -> Iterator[dict]:
    """Stream the transcript back. Malformed lines are skipped rather than
    failing the whole read — a truncated tail must not hide the history."""
    path = transcript_path(run_id)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            # L-19: a record without `seq` got record.get("seq", 0)=0, so when
            # after_seq was set the record was dropped (0 <= after_seq) — even
            # though it has no position to compare against the cursor. Only
            # filter records that actually HAVE a seq; yield the rest.
            seq = record.get("seq")
            if after_seq is not None and seq is not None and seq <= after_seq:
                continue
            yield record


def delete(run_id: str) -> bool:
    """Used by the events TTL purge so the flat mirror decays with the rows."""
    path = transcript_path(run_id)
    if path.exists():
        path.unlink()
        return True
    return False

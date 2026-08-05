"""Memory tiers — thread artifact + episodic FTS.

Working     = the live context window (messages) — managed by compaction.py.
Session     = the run's ephemeral state (mode, budget, task tracker) — EngineState.
Thread artifact = handoff.md — the living organ the next context READS.
              (worker/handoff.py writes it; this module reads + indexes it.)
Episodic    = per-turn summaries, full-text searchable for memory.search.
Knowledge   = the team knowledge base (proposals, playbooks) — backend-side.
Procedural = skills (.cursor/skills) — loaded into the prompt, not here.

This module owns thread-artifact indexing + episodic FTS + the memory.search
tool. Knowledge/Procedural are served by the backend; the engine queries them
via the gateway at turn start.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

# --- episodic store (SQLite FTS5, per-thread) ---

@dataclass
class EpisodicMemory:
    """Per-thread episodic memory with full-text search."""
    db_path: Path
    _db: Any = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                task_id TEXT,
                turn INTEGER,
                ts REAL NOT NULL,
                kind TEXT,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                detail TEXT
            )
        """)
        # FTS5 for full-text search over title + summary
        try:
            self._db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
                    title, summary, content='episodes', content_rowid='id'
                )
            """)
        except sqlite3.OperationalError:
            pass  # FTS5 unavailable — fall back to LIKE
        self._db.commit()

    def record(self, *, run_id: str, thread_id: str, task_id: str | None,
                turn: int, kind: str, title: str, summary: str, detail: dict | None = None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO episodes (run_id, thread_id, task_id, turn, ts, kind, title, summary, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, thread_id, task_id, turn, time.time(), kind, title, summary,
                 json.dumps(detail) if detail else None),
            )
            rowid = cur.lastrowid
            try:
                self._db.execute(
                    "INSERT INTO episodes_fts (rowid, title, summary) VALUES (?, ?, ?)",
                    (rowid, title, summary),
                )
            except sqlite3.OperationalError:
                pass  # FTS5 unavailable — the search LIKE fallback covers retrieval (H-12)
            self._db.commit()
            return rowid

    def search(self, query: str, *, run_id: str | None = None,
                thread_id: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Full-text search over episodic memory. Returns ranked matches."""
        with self._lock:
            if not query.strip():
                return []
            sql = (
                "SELECT e.id, e.run_id, e.thread_id, e.task_id, e.turn, e.ts, e.kind, "
                "e.title, e.summary, bm25(episodes_fts) AS rank "
                "FROM episodes_fts JOIN episodes e ON e.id = episodes_fts.rowid "
                "WHERE episodes_fts MATCH ? "
            )
            params: list[Any] = [query]
            if run_id:
                sql += " AND e.run_id = ?"
                params.append(run_id)
            if thread_id:
                sql += " AND e.thread_id = ?"
                params.append(thread_id)
            sql += " ORDER BY rank LIMIT ?"
            params.append(limit)
            try:
                rows = self._db.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                # FTS5 unavailable — fall back to LIKE
                like = f"%{query}%"
                # H-13: parenthesize the OR so the run_id/thread_id AND filters
                # apply to the whole match group. The old unparenthesized form
                # parsed as `title LIKE ? OR (summary LIKE ? AND run_id=? AND thread_id=?)`
                # — a row from ANY thread matched on title alone, leaking memory
                # across threads.
                fallback = ("SELECT id, run_id, thread_id, task_id, turn, ts, kind, title, summary "
                            "FROM episodes WHERE (title LIKE ? OR summary LIKE ?)")
                params2: list[Any] = [like, like]
                if run_id:
                    fallback += " AND run_id = ?"
                    params2.append(run_id)
                if thread_id:
                    fallback += " AND thread_id = ?"
                    params2.append(thread_id)
                fallback += " LIMIT ?"
                params2.append(limit)
                rows = self._db.execute(fallback, params2).fetchall()
                return [self._row_to_dict(r) for r in rows]
            return [self._row_to_dict(r) for r in rows]

    def _row_to_dict(self, row: tuple) -> dict[str, Any]:
        return {
            "id": row[0], "run_id": row[1], "thread_id": row[2], "task_id": row[3],
            "turn": row[4], "ts": row[5], "kind": row[6], "title": row[7], "summary": row[8],
        }

    def close(self) -> None:
        self._db.close()


# --- thread-artifact reader (handoff.md) ---

def read_handoff(workspace: Path) -> str | None:
    """Read the living artifact (handoff.md) if it exists."""
    handoff = workspace / "handoff.md"
    if not handoff.exists():
        return None
    return handoff.read_text(encoding="utf-8")


# --- memory.search tool (exposed to the agent) ---

_global_episodic: EpisodicMemory | None = None


def set_episodic_memory(ep: EpisodicMemory) -> None:
    """Wire the per-thread episodic store so the @tool can find it."""
    global _global_episodic
    _global_episodic = ep


@tool
def memory_search(query: str, limit: int = 5) -> str:
    """Search the thread's episodic memory for past turns matching the query.

    Use this when you need to recall what was investigated or decided in an
    earlier turn of this thread. Returns ranked matches with turn numbers and
    summaries.
    """
    if _global_episodic is None:
        return "episodic memory not available for this thread"
    results = _global_episodic.search(query, limit=limit)
    if not results:
        return "no matches"
    lines = []
    for r in results:
        lines.append(f"[turn {r.get('turn', '?')}] {r.get('title', '')}\n{r.get('summary', '')}")
    return "\n\n".join(lines) + f"\n[{len(results)} matches]"


__all__ = [
    "EpisodicMemory",
    "memory_search",
    "read_handoff",
    "set_episodic_memory",
]

"""Checkpointer — LangGraph persistence for fork/resume (plan §6 checkpointer.py).

BUILD ON: langgraph-checkpoint-postgres AsyncPostgresSaver (durable, supports
get_state_history for fork). BUILD CUSTOM: a DeltaChannel that mirrors every
checkpoint to a versioned JSONL file so the transcript is replayable without
Postgres (the §7a PHI-grade fallback + the edit-and-resend bridge source).

The checkpointer keys on context_id (= thread_id for top-level threads).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver


class DeltaChannel:
    """Mirrors checkpoint deltas to a versioned JSONL file (plan §7a fallback).

    Every checkpoint write appends one JSON line: {ts, thread_id, context_id,
    checkpoint_id, metadata}. The file is the replay source when Postgres is
    unavailable and the edit-and-resend fork source (replay up to a task_id).
    """

    def __init__(self, mirror_dir: Path) -> None:
        self.mirror_dir = mirror_dir
        self.mirror_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def append(self, thread_id: str, context_id: str, checkpoint_id: str,
                     metadata: dict[str, Any]) -> None:
        line = json.dumps({
            "ts": _now_iso(), "thread_id": thread_id, "context_id": context_id,
            "checkpoint_id": checkpoint_id, "metadata": metadata,
        }, default=str)
        path = self.mirror_dir / f"{thread_id}.jsonl"
        async with self._lock:
            await asyncio.to_thread(_append_line, path, line)

    def mirror_path(self, thread_id: str) -> Path:
        return self.mirror_dir / f"{thread_id}.jsonl"


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now(UTC).isoformat()


def make_checkpointer(*, use_postgres: bool = False, conn_string: str | None = None,
                      mirror_dir: Path | None = None) -> Any:
    """Build a LangGraph checkpointer synchronously (tests + tools).

    RA: Postgres is the production DEFAULT (plan §23 — MemorySaver = tests
    only). This sync factory cannot manage the AsyncPostgresSaver connection
    lifecycle, so production code uses `open_checkpointer()` below. This
    factory remains for unit tests that need a plain saver.
    """
    if use_postgres:
        raise RuntimeError(
            "use open_checkpointer() for Postgres — the saver needs an async "
            "connection lifecycle that this sync factory cannot manage."
        )
    return MemorySaver()


class _MemoryCtx:
    """Async-context wrapper so MemorySaver matches the Postgres path's API."""

    def __init__(self) -> None:
        self.saver = MemorySaver()

    async def __aenter__(self) -> Any:
        return self.saver

    async def __aexit__(self, *exc: Any) -> None:
        return None


def open_checkpointer(*, use_postgres: bool | None = None,
                      conn_string: str | None = None) -> Any:
    """Open the production checkpointer as an async context manager.

    RA doctrine (plan §23): Postgres is the DEFAULT. Resolution order:
      1. explicit use_postgres=... wins;
      2. DATABASE_URL set -> Postgres (langgraph-checkpoint-postgres);
      3. otherwise -> MemorySaver with a loud stderr warning (dev/test only —
         state dies with the process; approvals do NOT survive a restart).

    Usage:
        async with open_checkpointer() as saver:
            graph = build_graph(checkpointer=saver)
            ...
    """
    if use_postgres is None:
        use_postgres = bool(conn_string or os.environ.get("DATABASE_URL"))
    if not use_postgres:
        import sys
        print(
            "z-agent engine: WARNING — checkpointer is MemorySaver (in-memory). "
            "Set DATABASE_URL for the Postgres checkpointer; state and pending "
            "approvals do NOT survive a restart without it.",
            file=sys.stderr,
        )
        return _MemoryCtx()
    return _PostgresCtx(conn_string or os.environ.get("DATABASE_URL"))


class _PostgresCtx:
    """Owns the AsyncPostgresSaver connection lifecycle."""

    def __init__(self, conn_string: str | None) -> None:
        if not conn_string:
            raise RuntimeError(
                "DATABASE_URL required for the Postgres checkpointer "
                "(fail-closed: never silently fall back to in-memory)."
            )
        self.conn_string = conn_string
        self._ctx: Any = None

    async def __aenter__(self) -> Any:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        self._ctx = AsyncPostgresSaver.from_conn_string(self.conn_string)
        saver = await self._ctx.__aenter__()
        await saver.setup()
        return saver

    async def __aexit__(self, *exc: Any) -> None:
        if self._ctx is not None:
            await self._ctx.__aexit__(*exc)


__all__ = ["DeltaChannel", "make_checkpointer", "open_checkpointer"]

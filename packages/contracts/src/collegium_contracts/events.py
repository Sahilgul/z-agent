"""Canonical StepEvent schema — normalized at the worker edge, consumed by DB,
WebSocket relay, UI EventStream, drift detection, critical-path math, and fleet-bench.

Hard rules:
- A StepEvent is emitted ONCE, COMPLETE, at step end — the only thing ever stored.
- Live typing effects ride TypingDelta over pub/sub only (never stored, no seq).
- seq is monotonic per thread; WS relay and UI render strictly by seq.
- sdk_message_uuid is the edit-and-resend bridge (fork up to a task boundary).
  NULL = non-message / pre-instrumentation event; fork treats missing UUID as
  "cannot fork before this event"; replay handles null forever.

Protocol versioning — the schema is PINNED at v1:
- `schema_version` is monotonic. Breaking changes (field rename, type change,
  field removal) bump it; additive changes (new optional field) may stay.
- Consumers MUST guard on schema_version before reading any field not present
  in v1. The DB `events` table stores `schema_version` on every row.
- The version is NEVER silently changed; a bump ships a migration + a consumer
  guard in the same release.

Identifier contract (see identifiers.py): run_id → thread_id → context_id → task_id.
- thread_id replaces the former lane_id (rename locks in before any field is shipped).
- context_id == thread_id for top-level threads; derived for subagents.
- task_id identifies the turn within a context (the resumption/interrupt unit).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1


class StepKind(str, Enum):
    THINKING = "thinking"
    COMMAND = "command"
    FILE_READ = "file_read"
    FILE_EDIT = "file_edit"
    MCP_CALL = "mcp_call"
    TEST_RUN = "test_run"
    MESSAGE = "message"
    NOTEBOOK = "notebook"
    STATUS = "status"
    APPROVAL = "approval"  # dedicated approval kind (replaces STATUS/seq=0 cards)


class StepEvent(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    thread_id: str = Field(description="The work unit (was lane_id). One thread = one repo = one conversation.")
    context_id: str | None = Field(
        default=None,
        description="LangGraph checkpoint namespace. == thread_id for top-level threads; "
        "derived (`{thread_id}::worker-{n}`) for subagents. None = top-level (treated as thread_id).",
    )
    task_id: str | None = Field(
        default=None,
        description="The turn within a context (one query→ReAct→ResultMessage). "
        "The resumption/interrupt unit. None = pre-task / not yet assigned.",
    )
    seq: int = Field(ge=0, description="Monotonic per-thread sequence number")
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: StepKind
    title: str = Field(description="One-line summary shown collapsed")
    detail: dict[str, Any] = Field(default_factory=dict, description="Expandable payload")
    sdk_message_uuid: str | None = Field(
        default=None,
        description="SDK transcript UUID; null = non-message / pre-instrumentation",
    )

    def effective_context_id(self) -> str:
        """Resolve context_id, defaulting to thread_id for top-level threads."""
        return self.context_id or self.thread_id


class TypingDelta(BaseModel):
    """Transient in-progress fragment. Pub/sub only — NEVER stored, NEVER has seq."""

    schema_version: int = SCHEMA_VERSION
    run_id: str
    thread_id: str
    context_id: str | None = None
    kind: StepKind
    text: str
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))

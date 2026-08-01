"""Canonical StepEvent schema — normalized at the worker edge, consumed by DB,
WebSocket relay, UI EventStream, drift detection, critical-path math, and HAMI-bench.

Hard rules (plan §1b):
- A StepEvent is emitted ONCE, COMPLETE, at step end — the only thing ever stored.
- Live typing effects ride TypingDelta over pub/sub only (never stored, no seq).
- seq is monotonic per lane; WS relay and UI render strictly by seq.
- sdk_message_uuid is the edit-and-resend bridge (fork_session up_to_message_id).
  NULL = non-message / pre-instrumentation event; fork treats missing UUID as
  "cannot fork before this event"; replay handles null forever.
"""

from __future__ import annotations

from datetime import datetime, timezone
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


class StepEvent(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    lane_id: str
    seq: int = Field(ge=0, description="Monotonic per-lane sequence number")
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    kind: StepKind
    title: str = Field(description="One-line summary shown collapsed")
    detail: dict[str, Any] = Field(default_factory=dict, description="Expandable payload")
    sdk_message_uuid: str | None = Field(
        default=None,
        description="SDK transcript UUID; null = non-message / pre-instrumentation",
    )


class TypingDelta(BaseModel):
    """Transient in-progress fragment. Pub/sub only — NEVER stored, NEVER has seq."""

    schema_version: int = SCHEMA_VERSION
    run_id: str
    lane_id: str
    kind: StepKind
    text: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

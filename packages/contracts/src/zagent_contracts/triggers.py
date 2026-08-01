"""Canonical TriggerEvent (plan §6 triggers-as-data). ONE generic ingress normalizes
every source into this shape; the triggers engine matches it against `triggers` rows.
Idempotent on (source, external_id, revision). Identity resolution is fail-closed:
unresolved changed_by descriptor = no run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TriggerSource(str, Enum):
    ADO_WEBHOOK = "ado_webhook"
    CRON = "cron"
    MANUAL = "manual"


class TriggerEvent(BaseModel):
    schema_version: int = 1
    source: TriggerSource
    external_id: str = Field(description="Work item id, PR id, or pipeline run id from the source")
    revision: int = Field(ge=0, description="Source-side revision for idempotent dedupe")
    event_type: str = Field(description="e.g. work_item.updated, pr.comment, build.failed")
    changed_by_descriptor: str | None = Field(
        default=None,
        description="ADO identity descriptor; resolved to users.id FAIL-CLOSED by the identity service",
    )
    payload: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def idempotency_key(self) -> tuple[str, str, int]:
        return (self.source.value, self.external_id, self.revision)

"""modes — modes are DB rows (plan §6): a new mode = one blueprint file + one row.
Autonomy dial orthogonal: Supervised -> Gated -> Autonomous, promotion evidence-based.
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Mode(Base):
    __tablename__ = "modes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(64), unique=True)
    persona_prompt: Mapped[str] = mapped_column(sa.Text, default="")
    permission_mode: Mapped[str] = mapped_column(sa.String(24), default="default")
    topology: Mapped[str] = mapped_column(sa.String(24), default="single")
    model_tier: Mapped[str] = mapped_column(sa.String(16), default="strong")
    # Writable/read repo scope for the mode (plan §6 — modes as data): which repos
    # a lane spawned under this mode may stamp a writable clone for. Empty dict =
    # read-only. Topology stays code (selects the blueprint); this is data.
    permissions: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    playbook_ids: Mapped[list] = mapped_column(sa.JSON, default=list)
    evidence_contract: Mapped[dict] = mapped_column(sa.JSON, default=dict)
    autonomy_default: Mapped[str] = mapped_column(sa.String(16), default="supervised")
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

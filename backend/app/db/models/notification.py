"""notifications — PWA push subscriptions. Well-timed asks, never on
landing: opt-in after first AwaitingYou; A2HS after first phone approval.
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True)
    endpoint: Mapped[str] = mapped_column(sa.Text)
    keys: Mapped[dict] = mapped_column(sa.JSON, default=dict)  # VAPID p256dh/auth
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

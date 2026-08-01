"""Autonomy promotion (plan Phase 4): the dial unlocks on EVIDENCE, not on
request. Computed, never stored — a demotion needs no migration, and the cap
is always honest at read time.

Cap ladder: supervised → gated after N completed supervised runs → autonomous
after M completed gated runs. Triggered runs stay gated regardless (trust
guardrail) — this cap only governs what a human can pick in the composer.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.db.base import get_session
from app.db.models.run import Run

LADDER = ["supervised", "gated", "autonomous"]


def _completed_at(user_id: int, autonomy: str) -> int:
    session = get_session()
    try:
        return (session.query(Run)
                .filter_by(created_by=user_id, autonomy=autonomy, stage="completed")
                .count())
    finally:
        session.close()


def cap_for(user_id: int) -> dict:
    """The user's evidence-based autonomy ceiling + the trail that earned it."""
    settings = get_settings()
    supervised_done = _completed_at(user_id, "supervised")
    gated_done = _completed_at(user_id, "gated")
    cap = "supervised"
    if supervised_done >= settings.autonomy_promote_gated_after:
        cap = "gated"
    if gated_done >= settings.autonomy_promote_autonomous_after:
        cap = "autonomous"
    return {
        "cap": cap,
        "evidence": {
            "supervised_completed": supervised_done,
            "gated_completed": gated_done,
            "gated_unlocks_at": settings.autonomy_promote_gated_after,
            "autonomous_unlocks_at": settings.autonomy_promote_autonomous_after,
        },
    }


def clamp(requested: str | None, user_id: int) -> str:
    """A requested level above the cap is clamped DOWN, silently — the run card
    shows the effective level, and the composer greys out what's locked."""
    if not requested:
        return "gated"  # product default (§6)
    if requested not in LADDER:
        return "supervised"
    cap = cap_for(user_id)["cap"]
    return requested if LADDER.index(requested) <= LADDER.index(cap) else cap

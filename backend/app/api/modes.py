"""Mode routes: list enabled modes (modes are DB rows)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.deps import current_user
from app.db.base import get_session
from app.db.models.mode import Mode
from app.db.models.user import User

router = APIRouter(prefix="/modes", tags=["modes"])


@router.get("")
def list_modes(user: User = Depends(current_user)):
    session = get_session()
    try:
        modes = session.query(Mode).filter_by(enabled=True).all()
        return [{
            "name": m.name, "topology": m.topology, "model_tier": m.model_tier,
            "autonomy_default": m.autonomy_default,
        } for m in modes]
    finally:
        session.close()

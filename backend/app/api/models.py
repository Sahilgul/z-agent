"""GET /models — the selectable model fleet for the composer dropdown.

Served from the backend registry (app/core/models.py), NOT probed from the
gateway: the gateway may publish routes we don't offer for selection, and the
dropdown needs display labels + pricing the gateway config doesn't have.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.config import get_settings
from app.core.deps import current_user
from app.db.models.user import User

router = APIRouter(prefix="/models", tags=["models"])


@router.get("")
def list_models(user: User = Depends(current_user)):
    settings = get_settings()
    return {
        "models": [m.model_dump() for m in settings.available_models],
        # The alias a run gets when the user doesn't pick — the composer
        # pre-selects it so the control reads as a statement, not a blank.
        "default": settings.gateway_model,
    }

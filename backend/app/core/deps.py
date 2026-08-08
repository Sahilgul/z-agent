"""Shared FastAPI deps — re-exported for routers."""

from app.core.security import admin_user, current_user

__all__ = ["admin_user", "current_user"]

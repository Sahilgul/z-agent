"""Shared FastAPI deps — re-exported for routers."""

from app.core.security import admin_user, current_user

__all__ = ["current_user", "admin_user"]

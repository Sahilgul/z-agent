"""Engine/session setup. Dialect-neutrality discipline: portable types
only (sa.JSON, never JSONB/ARRAY), DB URL from config, WAL mode on SQLite, file on
the WSL2 Linux fs. Single-writer rule: ALL writes flow through this one backend
process — workers speak Redis/contracts only.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine():
    settings = get_settings()
    url = settings.db_url
    if url.startswith("sqlite"):
        settings.db_url  # noqa: B018 — keep settings import hot
        import pathlib

        path = url.split("///", 1)[-1]
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)


def get_session() -> Session:
    return SessionLocal()


def db_session() -> Session:
    """FastAPI request-scoped DB session (yield dependency).

    L-24: `current_user` opened its OWN session via get_session() (a second
    session per request) because get_session() is a plain call, not a
    FastAPI-managed dependency. This yield-dependency is cached per request
    by FastAPI, so `Depends(db_session)` in current_user AND in a route
    handler resolve to the SAME session — one session per request instead
    of two. The session is closed when the request ends.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

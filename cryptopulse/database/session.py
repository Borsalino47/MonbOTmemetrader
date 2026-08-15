"""Engine and session management.

SQLAlchemy's synchronous API is used deliberately: the write volume here is one
batch per scan interval, so an async driver would add dependency surface for no
measurable gain. Writes from async code go through `asyncio.to_thread`.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from cryptopulse.config.settings import DatabaseSettings
from cryptopulse.core.logging import get_logger
from cryptopulse.database.migrate import ensure_schema
from cryptopulse.database.models import Base

log = get_logger("database")

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def init_engine(cfg: DatabaseSettings) -> Engine:
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine

    url = cfg.url
    if url.startswith("sqlite:///"):
        path = Path(url.replace("sqlite:///", "", 1))
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)

    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    _engine = create_engine(url, echo=cfg.echo, future=True, connect_args=connect_args)
    Base.metadata.create_all(_engine)
    # create_all never alters an existing table, so a database predating a new
    # column would keep working right up until the first query touched it.
    added = ensure_schema(_engine)
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    log.info("database_ready", url=_redact(url), columns_added=len(added))
    return _engine


def get_session() -> Session:
    if _SessionFactory is None:
        raise RuntimeError("database not initialised — call init_engine() first")
    return _SessionFactory()


def reset_engine() -> None:
    """Test helper: drop the module-level engine so a fresh URL can be used."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


def _redact(url: str) -> str:
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url

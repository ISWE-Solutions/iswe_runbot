from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal = None


def configure(url: str | None = None):
    """(Re)bind the engine; tests call this with the test database URL."""
    global _engine, _SessionLocal
    _engine = create_engine(url or get_settings().database_url, pool_pre_ping=True, future=True)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def engine():
    if _engine is None:
        configure()
    return _engine


def SessionLocal() -> Session:
    if _SessionLocal is None:
        configure()
    return _SessionLocal()


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db():
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()

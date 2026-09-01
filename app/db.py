"""
Database plumbing: engine, session factory, and the FastAPI dependency that
hands out one Session per request.

Celery tasks do not go through FastAPI, so they use `session_scope()` instead --
a context manager that commits on success and rolls back on any exception.
A worker that dies mid-task must not leave a half-written status transition.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """Every model inherits from this; SQLAlchemy collects them on Base.metadata."""


def get_db() -> Iterator[Session]:
    """FastAPI dependency: a fresh session per request, always closed."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class session_scope:  # noqa: N801 - used as a context manager, reads better lowercase
    """
    Transactional session for Celery tasks.

        with session_scope() as db:
            ...

    Commits on a clean exit, rolls back on any exception, and always closes.
    Workers get killed mid-task -- on a deploy, on an OOM -- and a partially
    applied status transition would mean either a missed alert or a duplicate.
    """

    def __init__(self) -> None:
        self.db: Session = SessionLocal()

    def __enter__(self) -> Session:
        return self.db

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            if exc_type is None:
                self.db.commit()
            else:
                self.db.rollback()
        finally:
            self.db.close()

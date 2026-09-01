"""
Test fixtures.

The suite runs against in-memory SQLite and fakeredis, so it needs no services
installed. Celery tasks are exercised in eager mode where they are tested at
all -- but most of the logic lives in app/polling.py precisely so it can be
tested without a broker.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Environment must be set before app.config is imported: settings are cached
# the first time they are read.
os.environ["APP_ENV"] = "test"
os.environ["SCHEDULE_BASE_URL"] = "https://schedule.test/courses"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-bot-token"
os.environ["SMTP_HOST"] = "smtp.test"

from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Channel, Section, SectionStatus, Watch  # noqa: E402
from app.rate_limit import SlidingWindowRateLimiter  # noqa: E402
from app.redis_client import get_redis  # noqa: E402

# StaticPool keeps every connection on the same in-memory database; without it
# each connection would get its own empty one.
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture()
def db() -> Iterator[Session]:
    """A fresh schema per test, so no test can see another's rows."""
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def redis() -> fakeredis.FakeStrictRedis:
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture()
def client(db: Session, redis: fakeredis.FakeStrictRedis) -> Iterator[TestClient]:
    def override_get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = lambda: redis
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def limiter(redis: fakeredis.FakeStrictRedis) -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter(redis, "test", limit=3, window_seconds=60)


# --- Domain helpers --------------------------------------------------------

TERM = "202610"
CRN = "10432"


def make_section(
    db: Session,
    *,
    crn: str = CRN,
    term: str = TERM,
    status: SectionStatus = SectionStatus.closed,
    seats_open: int = 0,
    seats_total: int = 30,
    course_code: str = "CS 320",
    title: str = "Software Engineering",
    next_check_offset_seconds: int = 0,
) -> Section:
    section = Section(
        term=term,
        crn=crn,
        course_code=course_code,
        title=title,
        instructor="A. Rivera",
        status=status,
        seats_open=seats_open,
        seats_total=seats_total,
        next_check_at=datetime.now(UTC) + timedelta(seconds=next_check_offset_seconds),
    )
    db.add(section)
    db.commit()
    db.refresh(section)
    return section


def make_watch(
    db: Session,
    section: Section,
    *,
    channel: Channel = Channel.telegram,
    destination: str = "123456789",
    active: bool = True,
) -> Watch:
    watch = Watch(
        section_id=section.id,
        channel=channel,
        destination=destination,
        active=active,
    )
    db.add(watch)
    db.commit()
    db.refresh(watch)
    return watch


def schedule_html(
    *,
    crn: str = CRN,
    seats_open: int = 0,
    seats_total: int = 30,
    status_label: str = "Closed",
    course: str = "CS 320",
    title: str = "Software Engineering",
) -> str:
    """One-row schedule page in the markup the parser targets."""
    return f"""
    <html><body>
      <table class="section-list">
        <tr data-crn="{crn}">
          <td class="course">{course}</td>
          <td class="title">{title}</td>
          <td class="instructor">A. Rivera</td>
          <td class="seats-open">{seats_open}</td>
          <td class="seats-total">{seats_total}</td>
          <td class="status">{status_label}</td>
        </tr>
      </table>
    </body></html>
    """

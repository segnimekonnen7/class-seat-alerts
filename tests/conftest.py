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
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Environment must be set before app.config is imported: settings are cached
# the first time they are read.
os.environ["APP_ENV"] = "test"
os.environ["SCHEDULE_BASE_URL"] = "https://schedule.test/ClassSchedule"
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

TERM = "20273"  # MNSU yrtr code for Fall 2026
CRN = "005217"  # MNSU calls this the Course ID


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


FIXTURES = Path(__file__).parent / "fixtures"


def fixture_html(name: str) -> str:
    """A page saved from secure2.mnsu.edu, verbatim apart from stripped assets."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def schedule_html(
    *,
    crn: str = CRN,
    seats_open: int = 0,
    seats_total: int = 30,
    status_label: str = "Closed",
    course: str = "CIS 320",
    title: str = "Software Engineering",
    instructor: str = "Rivera, A",
) -> str:
    """
    A one-row results page in MNSU's real markup, with the numbers under test
    control.

    Note the status span carries `class="CloseSession"` regardless of the
    label: on the real site those class names are inverted, and hard-coding one
    of them here keeps the fake honest -- any test that accidentally started
    depending on the class would break against the real fixtures.
    """
    enrolled = max(seats_total - seats_open, 0)
    return f"""
    <html><body>
      <h5>{course}  - {title}                     (4        Credits)</h5>
      <table class="table table-striped table-sm table-bordered">
        <thead><tr>
          <th scope="col">Course ID</th><th scope="col">Sect</th>
          <th scope="col">Delivery Method</th><th scope="col">Grade<br/>Meth</th>
          <th scope="col">Days</th><th scope="col">Time</th>
          <th scope="col">Dates</th><th scope="col">Bldg/Room</th>
          <th scope="col">Instructor</th><th scope="col">Size</th>
          <th scope="col">Enrl</th><th scope="col">Status</th>
          <th scope="col">AddlInfo</th>
        </tr></thead>
        <tbody>
          <tr>
            <td>{crn}</td><td>01</td><td>Completely Online-Asynchronous</td>
            <td>OPT</td><td></td><td></td><td>08/24/26 - 12/11/26</td>
            <td>ON LINE</td><td>{instructor}</td>
            <td>{seats_total}</td><td>{enrolled}</td>
            <td><span class="CloseSession">{status_label}</span></td><td></td>
          </tr>
          <tr>
            <td aria-describedby="notes-{crn}" colspan="13">
              Notes for the previous row course id: {crn}
            </td>
          </tr>
        </tbody>
      </table>
    </body></html>
    """

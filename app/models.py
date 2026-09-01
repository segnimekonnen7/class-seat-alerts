"""
The data model.

Four tables:

  sections       -- a class section being tracked, and its current seat count
  watches        -- someone who wants to hear about one section
  status_events  -- append-only log of every status change a poll observed
  notifications  -- one row per (watch, status_event), which is what makes
                    delivery exactly-once instead of every-time-we-poll

The last one is the whole trick. A notification is not "we noticed the section
is open" -- it is "we are telling this person about this specific transition".
Because a transition happens once and the pair is unique, a person cannot be
told twice about the same opening no matter how many times a task is retried.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    """Timezone-aware UTC now. Naive datetimes are a bug waiting to happen."""
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    """
    Attach UTC to a naive datetime.

    Postgres returns aware datetimes for a timestamptz column; SQLite (which
    the test suite runs on) drops the offset. Comparing naive to aware raises,
    so normalization happens here rather than at every call site.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class SectionStatus(str, enum.Enum):
    """
    `unknown` is the state a section starts in, before its first successful
    poll. It matters: without it a brand-new watch on an already-open section
    would look like a closed -> open transition and fire an alert for an
    opening that never happened.
    """

    unknown = "unknown"
    open = "open"
    closed = "closed"


class Channel(str, enum.Enum):
    telegram = "telegram"
    email = "email"


class NotificationStatus(str, enum.Enum):
    pending = "pending"
    sending = "sending"
    sent = "sent"
    failed = "failed"


class Section(Base):
    """
    One class section on the schedule.

    Identified by (term, crn) -- a CRN is only unique within a term, so the
    pair is what the unique constraint covers. Two students watching the same
    section share this row, and therefore share one poll.
    """

    __tablename__ = "sections"
    __table_args__ = (
        UniqueConstraint("term", "crn", name="uq_sections_term_crn"),
        Index("ix_sections_due", "status", "next_check_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    term: Mapped[str] = mapped_column(String(16), nullable=False)
    crn: Mapped[str] = mapped_column(String(16), nullable=False)

    course_code: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    instructor: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    status: Mapped[SectionStatus] = mapped_column(
        Enum(SectionStatus, native_enum=False, length=16),
        nullable=False,
        default=SectionStatus.unknown,
    )
    seats_open: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seats_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_change_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # When this section is next eligible for a poll. The scheduler selects on
    # this column, so backing off a failing section is just pushing it out.
    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    watches: Mapped[list[Watch]] = relationship(
        back_populates="section", cascade="all, delete-orphan"
    )
    status_events: Mapped[list[StatusEvent]] = relationship(
        back_populates="section", cascade="all, delete-orphan"
    )

    @property
    def next_check_at_utc(self) -> datetime:
        normalized = _as_utc(self.next_check_at)
        assert normalized is not None  # noqa: S101 - column is NOT NULL
        return normalized

    @property
    def is_due(self) -> bool:
        return self.next_check_at_utc <= utcnow()

    def schedule_next_check(self, *, closed_seconds: int, open_seconds: int) -> None:
        """
        A closed section is the one that might open, so it gets the short
        interval. An open section is polled slowly -- it is only tracked so
        that a later close-then-open is caught properly.
        """
        interval = open_seconds if self.status is SectionStatus.open else closed_seconds
        self.next_check_at = utcnow() + timedelta(seconds=interval)

    def back_off(self, *, base_seconds: int, cap_seconds: int = 3600) -> None:
        """
        Exponential backoff after a failed poll, capped.

        A section whose CRN was removed from the schedule would otherwise be
        retried on the short interval forever, burning the rate-limit budget
        that the sections people are actually waiting on need.
        """
        delay = min(base_seconds * (2 ** min(self.consecutive_failures, 10)), cap_seconds)
        self.next_check_at = utcnow() + timedelta(seconds=delay)


class Watch(Base):
    """
    One person's interest in one section.

    The unique constraint means re-submitting the same watch is a no-op rather
    than a second row -- students do hit the button twice.
    """

    __tablename__ = "watches"
    __table_args__ = (
        UniqueConstraint(
            "section_id", "channel", "destination", name="uq_watches_section_channel_dest"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section_id: Mapped[int] = mapped_column(
        ForeignKey("sections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[Channel] = mapped_column(
        Enum(Channel, native_enum=False, length=16), nullable=False
    )
    destination: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    section: Mapped[Section] = relationship(back_populates="watches")


class StatusEvent(Base):
    """
    Append-only history: one row per observed status change.

    Kept even for open -> closed, which nobody is notified about, because the
    history is what lets you answer "how often does this section actually
    open, and for how long" after the fact.
    """

    __tablename__ = "status_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section_id: Mapped[int] = mapped_column(
        ForeignKey("sections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[SectionStatus] = mapped_column(
        Enum(SectionStatus, native_enum=False, length=16), nullable=False
    )
    to_status: Mapped[SectionStatus] = mapped_column(
        Enum(SectionStatus, native_enum=False, length=16), nullable=False
    )
    seats_open: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seats_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )

    section: Mapped[Section] = relationship(back_populates="status_events")

    @property
    def is_opening(self) -> bool:
        """Only a closed -> open transition is worth waking somebody up for."""
        return self.from_status is SectionStatus.closed and self.to_status is SectionStatus.open


class Notification(Base):
    """
    One delivery attempt to one person about one transition.

    The unique constraint on (watch_id, status_event_id) is the idempotency
    guarantee. A retried task, a duplicated Celery message, a worker that
    crashed after sending but before committing -- none of them can produce a
    second alert for the same opening, because the second insert has nowhere
    to go.

    `status` is claimed with a conditional UPDATE rather than a read-then-write,
    so two workers racing on the same row cannot both decide they own it.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("watch_id", "status_event_id", name="uq_notifications_watch_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(
        ForeignKey("watches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status_event_id: Mapped[int] = mapped_column(
        ForeignKey("status_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[Channel] = mapped_column(
        Enum(Channel, native_enum=False, length=16), nullable=False
    )
    destination: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, native_enum=False, length=16),
        nullable=False,
        default=NotificationStatus.pending,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

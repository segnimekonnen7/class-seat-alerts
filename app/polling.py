"""
The state machine: what a poll result does to a section.

This module holds every decision about status transitions and notification
queueing, and it deliberately does not know that Celery exists. It takes a
session and an Observation and returns what happened. That means the rules
that matter -- when an alert fires, when it does not -- are tested directly,
without a broker, a worker, or a clock to wait on.

The rule the whole service rests on:

    an alert fires on a closed -> open transition, once, per watch

Not "while the section is open", which would re-alert on every poll for as
long as the seat lasts. Not "when seats_open > 0", which fires again after a
poll that merely failed. A transition is a discrete event, it gets a row, and
a notification is tied to that row.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Notification,
    NotificationStatus,
    Section,
    StatusEvent,
    Watch,
    utcnow,
)
from app.scraper.parser import Observation

logger = logging.getLogger(__name__)


def record_observation(
    db: Session, section: Section, observation: Observation
) -> StatusEvent | None:
    """
    Apply a successful poll to a section.

    Returns the StatusEvent if the status changed, or None if nothing did.
    The caller checks `.is_opening` to decide whether anyone gets woken up --
    an open -> closed change is recorded but notifies nobody.
    """
    settings = get_settings()
    previous_status = section.status

    section.course_code = observation.course_code or section.course_code
    section.title = observation.title or section.title
    section.instructor = observation.instructor or section.instructor
    section.seats_open = observation.seats_open
    section.seats_total = observation.seats_total
    section.status = observation.status
    section.last_checked_at = utcnow()

    # A successful poll clears the failure streak, so a section that had a bad
    # afternoon goes straight back to the fast interval instead of staying
    # backed off.
    section.consecutive_failures = 0
    section.last_error = ""
    section.schedule_next_check(
        closed_seconds=settings.poll_interval_closed_seconds,
        open_seconds=settings.poll_interval_open_seconds,
    )

    if observation.status == previous_status:
        return None

    section.last_status_change_at = utcnow()
    event = StatusEvent(
        section_id=section.id,
        from_status=previous_status,
        to_status=observation.status,
        seats_open=observation.seats_open,
        seats_total=observation.seats_total,
    )
    db.add(event)
    db.flush()
    return event


def record_failure(db: Session, section: Section, error: str) -> None:
    """
    Apply a failed poll.

    The status is left alone on purpose. A timeout is not evidence that a
    section closed, and overwriting `open` with `unknown` here would manufacture
    a fake unknown -> open transition on the next successful poll -- an alert
    for an opening that never happened.
    """
    section.consecutive_failures += 1
    section.last_error = error[:500]
    section.last_checked_at = utcnow()
    section.back_off(base_seconds=get_settings().poll_interval_closed_seconds)
    db.flush()


def queue_notifications(db: Session, event: StatusEvent) -> list[Notification]:
    """
    Create one pending notification per active watch on this section.

    The unique constraint on (watch_id, status_event_id) is what makes this
    safe to call twice. If the task that called it crashed after committing and
    got retried, the second run's inserts collide and are skipped -- so a
    student is told about an opening exactly once, not once per retry.
    """
    if not event.is_opening:
        return []

    watches = (
        db.execute(
            select(Watch).where(Watch.section_id == event.section_id, Watch.active.is_(True))
        )
        .scalars()
        .all()
    )

    created: list[Notification] = []
    for watch in watches:
        notification = Notification(
            watch_id=watch.id,
            status_event_id=event.id,
            channel=watch.channel,
            destination=watch.destination,
            status=NotificationStatus.pending,
        )
        try:
            # Savepoint per row: one duplicate must not roll back the
            # notifications for everyone else watching the same section.
            #
            # The add() has to be *inside* the savepoint. Adding it outside
            # leaves the failed object pending in the session after the
            # savepoint unwinds, and the next flush re-raises the same
            # IntegrityError -- which turns one already-queued watch into a
            # dead session and no alerts for anybody.
            with db.begin_nested():
                db.add(notification)
                db.flush()
        except IntegrityError:
            logger.info("notification already queued for watch=%s event=%s", watch.id, event.id)
            continue
        created.append(notification)

    return created


def claim_notification(db: Session, notification_id: int) -> Notification | None:
    """
    Take ownership of a pending notification, or return None if someone else
    already has it.

    This is a conditional UPDATE, not a read-then-write. Two workers handed the
    same message both read `pending`; only one of them gets a rowcount of 1
    from `WHERE status = 'pending'`, and the other backs off. Checking in
    Python and then writing would let both through the gap.
    """
    result = db.execute(
        update(Notification)
        .where(
            Notification.id == notification_id,
            Notification.status == NotificationStatus.pending,
        )
        .values(status=NotificationStatus.sending)
    )

    if result.rowcount != 1:
        return None

    db.flush()
    notification = db.get(Notification, notification_id)
    if notification is not None:
        db.refresh(notification)
    return notification


def mark_sent(db: Session, notification: Notification) -> None:
    notification.status = NotificationStatus.sent
    notification.attempts += 1
    notification.sent_at = utcnow()
    notification.last_error = ""
    db.flush()


def mark_failed(db: Session, notification: Notification, error: str, *, retryable: bool) -> None:
    """
    Record a delivery failure.

    A retryable failure goes back to `pending` so the next attempt can claim it
    again; a permanent one (a bad email address, a Telegram chat that blocked
    the bot) is left `failed` rather than retried into the void.
    """
    notification.status = NotificationStatus.pending if retryable else NotificationStatus.failed
    notification.attempts += 1
    notification.last_error = error[:500]
    db.flush()


def due_sections(db: Session, limit: int = 200) -> list[Section]:
    """
    Sections eligible for a poll right now, most overdue first.

    Ordering by next_check_at means a backlog drains fairly: the section that
    has been waiting longest goes first, rather than whichever the database
    felt like returning.
    """
    stmt = (
        select(Section)
        .where(Section.next_check_at <= utcnow())
        .order_by(Section.next_check_at.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def sections_with_watchers(db: Session, sections: list[Section]) -> list[Section]:
    """
    Filter to sections somebody is actually waiting on.

    A section whose last watch was removed stays in the table for its history,
    but polling it would spend rate-limit budget on nobody's behalf.
    """
    if not sections:
        return []

    watched_ids = set(
        db.execute(
            select(Watch.section_id).where(
                Watch.section_id.in_([s.id for s in sections]), Watch.active.is_(True)
            )
        )
        .scalars()
        .all()
    )
    return [section for section in sections if section.id in watched_ids]

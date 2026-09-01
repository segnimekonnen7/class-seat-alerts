"""
The background tasks.

Three of them, and the split matters:

  dispatch_due_sections   -- beat runs this; it picks what to poll and fans out
  poll_section            -- one section, one HTTP request, one transaction
  send_notification       -- one message to one person

Fanning out means a slow or failing section delays only itself. A single task
that looped over every section would let one timeout hold up every other
student's alert, which for a service measured in seconds is the failure that
matters most.

The tasks stay thin on purpose: they handle retries and transactions, and hand
the actual decisions to app/polling.py, which is testable without a broker.
"""

from __future__ import annotations

import logging

from celery import shared_task
from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import Notification, NotificationStatus, Section, StatusEvent, Watch
from app.notifications import (
    PermanentDeliveryError,
    TransientDeliveryError,
    build_opening_message,
    get_notifier,
)
from app.polling import (
    claim_notification,
    due_sections,
    mark_failed,
    mark_sent,
    queue_notifications,
    record_failure,
    record_observation,
    sections_with_watchers,
)
from app.redis_client import get_scrape_limiter
from app.scraper.client import RateLimited, ScheduleClient, ScheduleUnavailable
from app.scraper.parser import ScheduleFormatChanged, SectionNotOnPage

logger = logging.getLogger(__name__)


@shared_task(name="app.tasks.poll.dispatch_due_sections")
def dispatch_due_sections() -> int:
    """
    Find sections due for a check and queue one poll each.

    Only sections with an active watch are dispatched -- polling a section
    nobody is waiting on spends rate-limit budget that the watched ones need.
    """
    with session_scope() as db:
        candidates = due_sections(db)
        targets = sections_with_watchers(db, candidates)
        section_ids = [section.id for section in targets]

    for section_id in section_ids:
        poll_section.delay(section_id)

    logger.info("dispatched %d section polls", len(section_ids))
    return len(section_ids)


@shared_task(
    name="app.tasks.poll.poll_section",
    bind=True,
    max_retries=5,
    # Exponential backoff with jitter. Without jitter, a hundred sections that
    # failed together during one outage would all retry at the same instant and
    # reproduce the outage.
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def poll_section(self, section_id: int) -> str:  # type: ignore[no-untyped-def]
    """
    Check one section and, if it just opened, queue the alerts.

    Failure handling splits three ways, because the right response differs:

      RateLimited          -- not an error. Retry shortly; the window will clear.
      ScheduleUnavailable  -- probably transient. Retry with backoff.
      SectionNotOnPage     -- the CRN is gone from the schedule. Recording it as
                              a failure backs the section off exponentially
                              instead of retrying a dead CRN every minute.
    """
    with session_scope() as db:
        section = db.get(Section, section_id)
        if section is None:
            return "missing"

        term, crn = section.term, section.crn

        try:
            client = ScheduleClient(get_scrape_limiter())
            observation = client.fetch_section(term, crn)
        except RateLimited as exc:
            raise self.retry(exc=exc, countdown=5) from exc
        except ScheduleUnavailable as exc:
            record_failure(db, section, str(exc))
            raise self.retry(exc=exc) from exc
        except (SectionNotOnPage, ScheduleFormatChanged) as exc:
            # Not retried: another request would return the same page.
            record_failure(db, section, f"{type(exc).__name__}: {exc}")
            logger.warning("section %s (%s/%s) not parseable: %s", section_id, term, crn, exc)
            return "unparseable"

        event = record_observation(db, section, observation)
        if event is None:
            return "unchanged"

        notifications = queue_notifications(db, event)
        notification_ids = [n.id for n in notifications]
        transition = f"{event.from_status.value}->{event.to_status.value}"

    for notification_id in notification_ids:
        send_notification.delay(notification_id)

    logger.info(
        "section %s %s, queued %d notifications", section_id, transition, len(notification_ids)
    )
    return transition


@shared_task(
    name="app.tasks.poll.send_notification",
    bind=True,
    max_retries=5,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def send_notification(self, notification_id: int) -> str:  # type: ignore[no-untyped-def]
    """
    Deliver one alert.

    The notification is *claimed* with a conditional UPDATE first. If the claim
    fails, another worker already has this one -- which happens under
    `task_acks_late`, where a redelivered message can race the original -- and
    this run exits without sending a duplicate.
    """
    with session_scope() as db:
        notification = claim_notification(db, notification_id)
        if notification is None:
            logger.info("notification %s already claimed or sent", notification_id)
            return "already_handled"

        watch = db.get(Watch, notification.watch_id)
        event = db.get(StatusEvent, notification.status_event_id)
        if watch is None or event is None:  # pragma: no cover - FK-protected
            mark_failed(db, notification, "watch or event disappeared", retryable=False)
            return "orphaned"

        section = db.get(Section, watch.section_id)
        if section is None:  # pragma: no cover - FK-protected
            mark_failed(db, notification, "section disappeared", retryable=False)
            return "orphaned"

        message = build_opening_message(
            course_code=section.course_code or section.crn,
            title=section.title,
            crn=section.crn,
            term=section.term,
            seats_open=event.seats_open,
            seats_total=event.seats_total,
        )

        try:
            get_notifier(notification.channel).send(notification.destination, message)
        except PermanentDeliveryError as exc:
            mark_failed(db, notification, str(exc), retryable=False)
            logger.warning("permanent delivery failure for %s: %s", notification_id, exc)
            return "failed"
        except TransientDeliveryError as exc:
            # Back to pending so the next attempt can claim it again.
            mark_failed(db, notification, str(exc), retryable=True)
            raise self.retry(exc=exc) from exc

        mark_sent(db, notification)

    logger.info("notification %s sent", notification_id)
    return "sent"


@shared_task(name="app.tasks.poll.retry_pending_notifications")
def retry_pending_notifications(max_attempts: int = 5) -> int:
    """
    Safety net for notifications left pending.

    A worker killed between claiming and sending leaves a row in `sending`
    forever, and a transient failure on the last retry leaves one in `pending`.
    Beat sweeps both back into the queue, so a crash costs a minute of delay
    rather than a missed alert. `max_attempts` stops a permanently broken
    destination from being swept in a loop.
    """
    with session_scope() as db:
        stmt = select(Notification).where(
            Notification.status.in_([NotificationStatus.pending, NotificationStatus.sending]),
            Notification.attempts < max_attempts,
        )
        stale = list(db.execute(stmt).scalars().all())

        for notification in stale:
            # Put a stuck `sending` row back to `pending` so the conditional
            # claim in send_notification can pick it up again.
            notification.status = NotificationStatus.pending
        notification_ids = [n.id for n in stale]

    for notification_id in notification_ids:
        send_notification.delay(notification_id)

    if notification_ids:
        logger.info("re-queued %d stale notifications", len(notification_ids))
    return len(notification_ids)


def _settings_snapshot() -> dict[str, object]:
    """Small helper used by the health endpoint to report worker config."""
    settings = get_settings()
    return {
        "poll_interval_closed_seconds": settings.poll_interval_closed_seconds,
        "poll_interval_open_seconds": settings.poll_interval_open_seconds,
        "scrape_requests_per_window": settings.scrape_requests_per_window,
        "scrape_window_seconds": settings.scrape_window_seconds,
    }

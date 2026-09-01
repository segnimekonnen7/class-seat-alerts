"""
The state machine.

This is the file that matters most. The service's one promise is:

    an alert fires on a closed -> open transition, once, per watch

Everything here is a way that promise could be broken.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.models import (
    Channel,
    Notification,
    NotificationStatus,
    SectionStatus,
    StatusEvent,
    utcnow,
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
from app.scraper.parser import Observation
from tests.conftest import CRN, make_section, make_watch


def observation(
    *,
    status: SectionStatus = SectionStatus.open,
    seats_open: int = 3,
    seats_total: int = 30,
) -> Observation:
    return Observation(
        crn=CRN,
        course_code="CS 320",
        title="Software Engineering",
        instructor="A. Rivera",
        seats_open=seats_open,
        seats_total=seats_total,
        status=status,
    )


# --- Transitions -----------------------------------------------------------


def test_closed_to_open_produces_an_event(db):
    section = make_section(db, status=SectionStatus.closed)
    event = record_observation(db, section, observation(status=SectionStatus.open))

    assert event is not None
    assert event.is_opening is True
    assert section.status is SectionStatus.open


def test_no_change_produces_no_event(db):
    """
    The single most important negative case. If an unchanged poll produced an
    event, every student would get an alert every minute for as long as the
    section stayed open.
    """
    section = make_section(db, status=SectionStatus.open, seats_open=3)
    assert record_observation(db, section, observation(status=SectionStatus.open)) is None


def test_open_to_closed_is_recorded_but_is_not_an_opening(db):
    section = make_section(db, status=SectionStatus.open, seats_open=3)
    event = record_observation(db, section, observation(status=SectionStatus.closed))

    assert event is not None
    assert event.is_opening is False


def test_unknown_to_open_is_not_an_opening(db):
    """
    A brand-new watch on an already-open section must not fire. The section was
    not observed to open -- this is the first time anyone looked at it. Seeding
    a new section as `closed` instead of `unknown` is what would break this.
    """
    section = make_section(db, status=SectionStatus.unknown)
    event = record_observation(db, section, observation(status=SectionStatus.open))

    assert event is not None
    assert event.is_opening is False


def test_unknown_to_closed_is_not_an_opening(db):
    section = make_section(db, status=SectionStatus.unknown)
    event = record_observation(db, section, observation(status=SectionStatus.closed))
    assert event.is_opening is False


def test_a_reopening_fires_again(db):
    """
    Open, close, open again is two separate openings and two alerts. Suppressing
    the second would mean a student who missed the first window never hears
    about the second.
    """
    section = make_section(db, status=SectionStatus.closed)

    first = record_observation(db, section, observation(status=SectionStatus.open))
    record_observation(db, section, observation(status=SectionStatus.closed))
    second = record_observation(db, section, observation(status=SectionStatus.open))

    assert first.is_opening and second.is_opening
    assert first.id != second.id


def test_an_observation_updates_the_seat_counts(db):
    section = make_section(db, status=SectionStatus.closed, seats_open=0)
    record_observation(db, section, observation(seats_open=5, seats_total=40))

    assert section.seats_open == 5
    assert section.seats_total == 40


def test_metadata_is_backfilled_from_the_page(db):
    """A section created by a watch has no title until the first poll."""
    section = make_section(db, course_code="", title="")
    record_observation(db, section, observation())

    assert section.course_code == "CS 320"
    assert section.title == "Software Engineering"


# --- Scheduling and backoff ------------------------------------------------


def test_a_closed_section_gets_the_short_interval(db):
    section = make_section(db, status=SectionStatus.open)
    record_observation(db, section, observation(status=SectionStatus.closed))

    # Default closed interval is 60s, open is 600s.
    assert section.next_check_at_utc < utcnow() + timedelta(seconds=120)


def test_an_open_section_gets_the_long_interval(db):
    section = make_section(db, status=SectionStatus.closed)
    record_observation(db, section, observation(status=SectionStatus.open))

    assert section.next_check_at_utc > utcnow() + timedelta(seconds=120)


def test_a_failure_does_not_change_the_status(db):
    """
    A timeout is not evidence a section closed. Overwriting the status here
    would manufacture a fake transition on the next successful poll -- an alert
    for an opening that never happened.
    """
    section = make_section(db, status=SectionStatus.open, seats_open=3)
    record_failure(db, section, "connection reset")

    assert section.status is SectionStatus.open
    assert section.seats_open == 3


def test_failures_back_off_exponentially(db):
    section = make_section(db, status=SectionStatus.closed)

    record_failure(db, section, "boom")
    first_delay = section.next_check_at_utc - utcnow()

    record_failure(db, section, "boom")
    record_failure(db, section, "boom")
    third_delay = section.next_check_at_utc - utcnow()

    assert third_delay > first_delay


def test_backoff_is_capped(db):
    """A dead CRN must not back off to next week; an hour is far enough."""
    section = make_section(db, status=SectionStatus.closed)
    for _ in range(30):
        record_failure(db, section, "boom")

    assert section.next_check_at_utc <= utcnow() + timedelta(seconds=3601)


def test_a_success_clears_the_failure_streak(db):
    section = make_section(db, status=SectionStatus.closed)
    record_failure(db, section, "boom")
    record_failure(db, section, "boom")

    record_observation(db, section, observation(status=SectionStatus.closed))

    assert section.consecutive_failures == 0
    assert section.last_error == ""
    # Back on the short interval, not still backed off.
    assert section.next_check_at_utc < utcnow() + timedelta(seconds=120)


# --- Selecting what to poll ------------------------------------------------


def test_due_sections_returns_only_what_is_due(db):
    due = make_section(db, crn="1", next_check_offset_seconds=-10)
    make_section(db, crn="2", next_check_offset_seconds=600)

    assert [s.id for s in due_sections(db)] == [due.id]


def test_due_sections_puts_the_most_overdue_first(db):
    recent = make_section(db, crn="1", next_check_offset_seconds=-10)
    stale = make_section(db, crn="2", next_check_offset_seconds=-600)

    assert [s.id for s in due_sections(db)] == [stale.id, recent.id]


def test_sections_without_watchers_are_not_polled(db):
    """Polling a section nobody waits on spends budget the watched ones need."""
    watched = make_section(db, crn="1", next_check_offset_seconds=-10)
    unwatched = make_section(db, crn="2", next_check_offset_seconds=-10)
    make_watch(db, watched)

    result = sections_with_watchers(db, due_sections(db))
    assert [s.id for s in result] == [watched.id]
    assert unwatched.id not in [s.id for s in result]


def test_a_deactivated_watch_does_not_keep_a_section_polled(db):
    section = make_section(db, next_check_offset_seconds=-10)
    make_watch(db, section, active=False)

    assert sections_with_watchers(db, due_sections(db)) == []


# --- Notification fan-out and idempotency ----------------------------------


def test_an_opening_queues_one_notification_per_watch(db):
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section, destination="111")
    make_watch(db, section, destination="222")

    event = record_observation(db, section, observation(status=SectionStatus.open))
    assert len(queue_notifications(db, event)) == 2


def test_a_non_opening_queues_nothing(db):
    section = make_section(db, status=SectionStatus.open)
    make_watch(db, section)

    event = record_observation(db, section, observation(status=SectionStatus.closed))
    assert queue_notifications(db, event) == []


def test_an_inactive_watch_is_not_notified(db):
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section, destination="111", active=True)
    make_watch(db, section, destination="222", active=False)

    event = record_observation(db, section, observation(status=SectionStatus.open))
    created = queue_notifications(db, event)

    assert [n.destination for n in created] == ["111"]


def test_queueing_twice_for_the_same_event_creates_nothing_new(db):
    """
    The core idempotency case. `task_acks_late` means a task can run twice; the
    unique constraint on (watch_id, status_event_id) is what stops the second
    run from producing a second alert.
    """
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)
    event = record_observation(db, section, observation(status=SectionStatus.open))

    assert len(queue_notifications(db, event)) == 1
    assert queue_notifications(db, event) == []

    total = db.execute(select(func.count()).select_from(Notification)).scalar_one()
    assert total == 1


def test_a_duplicate_does_not_block_the_other_watchers(db):
    """
    Each insert gets its own savepoint. Without that, one already-queued watch
    would roll back the whole batch and everybody else would miss the alert.
    """
    section = make_section(db, status=SectionStatus.closed)
    first = make_watch(db, section, destination="111")
    event = record_observation(db, section, observation(status=SectionStatus.open))

    # Pre-queue one of the two, as a crashed earlier run would have left it.
    db.add(
        Notification(
            watch_id=first.id,
            status_event_id=event.id,
            channel=Channel.telegram,
            destination="111",
        )
    )
    db.flush()

    make_watch(db, section, destination="222")
    created = queue_notifications(db, event)

    assert [n.destination for n in created] == ["222"]


def test_two_openings_produce_two_notifications_for_one_watch(db):
    """
    Idempotency is per transition, not per watch. A section that opens twice
    should reach the same person twice.
    """
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)

    first = record_observation(db, section, observation(status=SectionStatus.open))
    queue_notifications(db, first)
    record_observation(db, section, observation(status=SectionStatus.closed))
    second = record_observation(db, section, observation(status=SectionStatus.open))
    queue_notifications(db, second)

    assert db.execute(select(func.count()).select_from(Notification)).scalar_one() == 2


# --- Claiming --------------------------------------------------------------


def _one_pending(db):
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)
    event = record_observation(db, section, observation(status=SectionStatus.open))
    notification = queue_notifications(db, event)[0]
    db.commit()
    return notification


def test_a_pending_notification_can_be_claimed(db):
    notification = _one_pending(db)
    claimed = claim_notification(db, notification.id)

    assert claimed is not None
    assert claimed.status is NotificationStatus.sending


def test_a_second_claim_returns_nothing(db):
    """
    Two workers handed the same redelivered message both see `pending`. The
    conditional UPDATE is what makes exactly one of them the owner -- a
    read-then-write would let both through.
    """
    notification = _one_pending(db)

    assert claim_notification(db, notification.id) is not None
    assert claim_notification(db, notification.id) is None


def test_a_sent_notification_cannot_be_reclaimed(db):
    notification = _one_pending(db)
    claimed = claim_notification(db, notification.id)
    mark_sent(db, claimed)
    db.commit()

    assert claim_notification(db, notification.id) is None


def test_a_transient_failure_returns_it_to_pending(db):
    notification = _one_pending(db)
    claimed = claim_notification(db, notification.id)
    mark_failed(db, claimed, "smtp timeout", retryable=True)
    db.commit()

    assert claimed.status is NotificationStatus.pending
    assert claim_notification(db, notification.id) is not None


def test_a_permanent_failure_is_not_retried(db):
    """A blocked bot or a dead address stays failed rather than looping."""
    notification = _one_pending(db)
    claimed = claim_notification(db, notification.id)
    mark_failed(db, claimed, "user blocked the bot", retryable=False)
    db.commit()

    assert claimed.status is NotificationStatus.failed
    assert claim_notification(db, notification.id) is None


def test_attempts_are_counted(db):
    notification = _one_pending(db)
    claimed = claim_notification(db, notification.id)
    mark_failed(db, claimed, "boom", retryable=True)
    db.commit()

    reclaimed = claim_notification(db, notification.id)
    mark_sent(db, reclaimed)

    assert reclaimed.attempts == 2


def test_sending_records_a_timestamp(db):
    notification = _one_pending(db)
    claimed = claim_notification(db, notification.id)
    mark_sent(db, claimed)

    assert claimed.sent_at is not None
    assert claimed.last_error == ""


def test_the_event_history_is_kept(db):
    """
    Every transition is retained, including the ones nobody was notified about.
    It is what answers "does this section ever actually open".
    """
    section = make_section(db, status=SectionStatus.closed)
    record_observation(db, section, observation(status=SectionStatus.open))
    record_observation(db, section, observation(status=SectionStatus.closed))
    record_observation(db, section, observation(status=SectionStatus.open))

    events = (
        db.execute(select(StatusEvent).where(StatusEvent.section_id == section.id)).scalars().all()
    )
    assert len(events) == 3

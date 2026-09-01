"""
Task-level tests: the full poll -> transition -> notify path.

The tasks are run directly rather than through a broker. What is being tested
here is the wiring the unit tests cannot reach: that a poll actually persists,
that a transition actually fans out to notifications, and -- the one that
matters most -- that running the same poll twice does not send two alerts.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import func, select

from app.models import Notification, NotificationStatus, SectionStatus
from app.notifications.base import PermanentDeliveryError, TransientDeliveryError
from app.polling import claim_notification, mark_failed, mark_sent
from app.scraper.client import ScheduleClient
from app.tasks import poll as poll_tasks
from tests.conftest import CRN, make_section, make_watch, schedule_html


@pytest.fixture()
def patched_tasks(monkeypatch, db, redis):
    """
    Point the tasks at the test session and a controllable page.

    The tasks open their own sessions via session_scope; here they get the test
    session instead, with commit/close made no-ops so the fixture's rollback
    still owns the transaction.
    """

    class TestScope:
        def __enter__(self):
            return db

        def __exit__(self, *args: object) -> None:
            db.flush()

    monkeypatch.setattr(poll_tasks, "session_scope", TestScope)

    state = {"html": schedule_html(seats_open=0, status_label="Closed"), "status": 200}

    # The schedule is a form POST: a GET serves the search form (carrying the
    # anti-forgery token) and the POST returns the results.
    form_page = (
        "<html><form>" '<input name="__RequestVerificationToken" value="tok"/>' "</form></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=form_page, request=request)
        return httpx.Response(state["status"], text=state["html"], request=request)

    from app.rate_limit import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(redis, "task-test", limit=1000, window_seconds=60)

    monkeypatch.setattr(
        poll_tasks,
        "ScheduleClient",
        lambda _limiter: ScheduleClient(limiter, transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(poll_tasks, "get_scrape_limiter", lambda: limiter)

    sent: list[tuple[str, str]] = []

    class RecordingNotifier:
        name = "recording"

        def send(self, destination: str, message: object) -> None:
            sent.append((destination, getattr(message, "body", "")))

    monkeypatch.setattr(poll_tasks, "get_notifier", lambda channel: RecordingNotifier())

    # Run .delay() inline so the fan-out is observable without a broker.
    monkeypatch.setattr(
        poll_tasks.send_notification, "delay", lambda nid: poll_tasks.send_notification(nid)
    )
    monkeypatch.setattr(poll_tasks.poll_section, "delay", lambda sid: poll_tasks.poll_section(sid))

    return state, sent


def test_a_poll_with_no_change_reports_unchanged(db, patched_tasks):
    state, _ = patched_tasks
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)

    assert poll_tasks.poll_section(section.id) == "unchanged"


def test_an_opening_is_detected_and_delivered(db, patched_tasks):
    state, sent = patched_tasks
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section, destination="123456789")

    state["html"] = schedule_html(seats_open=2, status_label="Open")
    result = poll_tasks.poll_section(section.id)

    assert result == "closed->open"
    assert len(sent) == 1
    assert sent[0][0] == "123456789"
    assert CRN in sent[0][1]


def test_polling_twice_after_an_opening_does_not_re_alert(db, patched_tasks):
    """
    The behaviour the whole design exists for. Once the section is open, every
    subsequent poll sees no change and nobody is told twice.
    """
    state, sent = patched_tasks
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)

    state["html"] = schedule_html(seats_open=2, status_label="Open")
    poll_tasks.poll_section(section.id)
    poll_tasks.poll_section(section.id)
    poll_tasks.poll_section(section.id)

    assert len(sent) == 1


def test_every_watcher_of_a_section_is_told(db, patched_tasks):
    state, sent = patched_tasks
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section, destination="111")
    make_watch(db, section, destination="222")

    state["html"] = schedule_html(seats_open=1, status_label="Open")
    poll_tasks.poll_section(section.id)

    assert {destination for destination, _ in sent} == {"111", "222"}


def test_a_section_closing_tells_nobody(db, patched_tasks):
    state, sent = patched_tasks
    section = make_section(db, status=SectionStatus.open, seats_open=3)
    make_watch(db, section)

    state["html"] = schedule_html(seats_open=0, status_label="Closed")
    assert poll_tasks.poll_section(section.id) == "open->closed"
    assert sent == []


def test_a_first_poll_of_an_open_section_tells_nobody(db, patched_tasks):
    """A new watch on an already-open section is not an opening."""
    state, sent = patched_tasks
    section = make_section(db, status=SectionStatus.unknown)
    make_watch(db, section)

    state["html"] = schedule_html(seats_open=5, status_label="Open")
    poll_tasks.poll_section(section.id)

    assert sent == []


def test_a_missing_crn_is_recorded_not_retried(db, patched_tasks):
    state, _ = patched_tasks
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)

    state["html"] = schedule_html(crn="999999")
    assert poll_tasks.poll_section(section.id) == "unparseable"
    assert section.consecutive_failures == 1


def test_polling_a_deleted_section_is_a_no_op(db, patched_tasks):
    assert poll_tasks.poll_section(999999) == "missing"


def test_dispatch_only_queues_watched_sections(db, patched_tasks, monkeypatch):
    state, sent = patched_tasks
    watched = make_section(db, crn="1", next_check_offset_seconds=-10)
    make_section(db, crn="2", next_check_offset_seconds=-10)
    make_watch(db, watched)

    dispatched: list[int] = []
    monkeypatch.setattr(poll_tasks.poll_section, "delay", dispatched.append)

    assert poll_tasks.dispatch_due_sections() == 1
    assert dispatched == [watched.id]


def test_sending_an_already_sent_notification_is_a_no_op(db, patched_tasks):
    state, sent = patched_tasks
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)

    state["html"] = schedule_html(seats_open=1, status_label="Open")
    poll_tasks.poll_section(section.id)

    notification = db.execute(select(Notification)).scalars().one()
    assert poll_tasks.send_notification(notification.id) == "already_handled"
    assert len(sent) == 1


def test_a_permanent_delivery_failure_is_not_retried(db, patched_tasks, monkeypatch):
    state, _ = patched_tasks
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)

    class BlockedNotifier:
        def send(self, destination: str, message: object) -> None:
            raise PermanentDeliveryError("user blocked the bot")

    monkeypatch.setattr(poll_tasks, "get_notifier", lambda channel: BlockedNotifier())

    state["html"] = schedule_html(seats_open=1, status_label="Open")
    poll_tasks.poll_section(section.id)

    notification = db.execute(select(Notification)).scalars().one()
    assert notification.status is NotificationStatus.failed
    assert "blocked" in notification.last_error


def test_a_stale_notification_is_swept_back_into_the_queue(db, patched_tasks, monkeypatch):
    """
    A worker killed between claiming and sending leaves a row stuck in
    `sending`. Beat's sweep puts it back so a crash costs a minute of delay
    rather than a missed alert.
    """
    state, sent = patched_tasks
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)

    state["html"] = schedule_html(seats_open=1, status_label="Open")

    # Deliver nothing on the first pass, leaving the row claimed but unsent.
    class DeadNotifier:
        def send(self, destination: str, message: object) -> None:
            raise TransientDeliveryError("worker died")

    monkeypatch.setattr(poll_tasks, "get_notifier", lambda channel: DeadNotifier())
    monkeypatch.setattr(poll_tasks.send_notification, "delay", lambda nid: None)
    poll_tasks.poll_section(section.id)

    notification = db.execute(select(Notification)).scalars().one()
    notification.status = NotificationStatus.sending
    db.flush()

    # Now the sweep runs with a working notifier.
    class WorkingNotifier:
        def send(self, destination: str, message: object) -> None:
            sent.append((destination, ""))

    monkeypatch.setattr(poll_tasks, "get_notifier", lambda channel: WorkingNotifier())
    monkeypatch.setattr(
        poll_tasks.send_notification, "delay", lambda nid: poll_tasks.send_notification(nid)
    )

    assert poll_tasks.retry_pending_notifications() == 1
    assert len(sent) == 1


def test_the_sweep_gives_up_after_enough_attempts(db, patched_tasks):
    """A permanently broken destination must not be swept in a loop forever."""
    state, _ = patched_tasks
    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)

    state["html"] = schedule_html(seats_open=1, status_label="Open")
    poll_tasks.poll_section(section.id)

    notification = db.execute(select(Notification)).scalars().one()
    notification.status = NotificationStatus.pending
    notification.attempts = 5
    db.flush()

    assert poll_tasks.retry_pending_notifications(max_attempts=5) == 0


def test_a_claimed_notification_survives_a_send_and_a_resend(db):
    """
    Direct check on the claim/mark cycle: a row that has been sent cannot be
    claimed again, which is what makes a duplicate Celery message harmless.
    """
    from app.polling import queue_notifications, record_observation
    from tests.test_polling import observation

    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)
    event = record_observation(db, section, observation(status=SectionStatus.open))
    notification = queue_notifications(db, event)[0]
    db.commit()

    claimed = claim_notification(db, notification.id)
    mark_sent(db, claimed)
    db.commit()

    assert claim_notification(db, notification.id) is None
    assert db.execute(select(func.count()).select_from(Notification)).scalar_one() == 1


def test_a_transient_failure_can_be_reclaimed_and_sent(db):
    from app.polling import queue_notifications, record_observation
    from tests.test_polling import observation

    section = make_section(db, status=SectionStatus.closed)
    make_watch(db, section)
    event = record_observation(db, section, observation(status=SectionStatus.open))
    notification = queue_notifications(db, event)[0]
    db.commit()

    first = claim_notification(db, notification.id)
    mark_failed(db, first, "smtp timeout", retryable=True)
    db.commit()

    second = claim_notification(db, notification.id)
    assert second is not None
    mark_sent(db, second)
    assert second.status is NotificationStatus.sent

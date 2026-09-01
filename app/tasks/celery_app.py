"""
Celery configuration.

Two settings here do most of the work of keeping this service honest:

`task_acks_late = True` -- a message is acknowledged after the task finishes,
not when it is picked up. A worker killed mid-poll puts its message back
rather than dropping it. That means a task can run twice, which is exactly why
notifications are keyed on (watch, status_event) instead of trusting the queue.

`worker_prefetch_multiplier = 1` -- a worker takes one message at a time. The
default of 4 lets a single worker hoard a queue of sections while other workers
sit idle, which for a service whose value is measured in seconds is the wrong
trade.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab  # noqa: F401 - re-exported for schedule edits

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "class_seat_alerts",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.poll"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    # A poll that has not finished in a minute is not going to be useful --
    # the seat it was checking on is long gone either way.
    task_soft_time_limit=45,
    task_time_limit=60,
    result_expires=3600,
    beat_schedule={
        "dispatch-due-sections": {
            "task": "app.tasks.poll.dispatch_due_sections",
            # The dispatcher is cheap: one indexed query, then one message per
            # section that is actually due. Running it every 15s keeps the
            # worst-case detection lag well under the poll interval itself.
            "schedule": 15.0,
        },
        "retry-pending-notifications": {
            "task": "app.tasks.poll.retry_pending_notifications",
            "schedule": 60.0,
        },
    },
)

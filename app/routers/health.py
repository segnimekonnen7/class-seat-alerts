"""
Liveness, readiness, and a small operational view.

/health never touches a dependency, so a Postgres or Redis outage does not get
the container killed and restarted straight back into the same outage.
/readyz checks both, because a process that cannot reach Redis cannot queue a
single alert and should be pulled from the load balancer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from redis import Redis
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.config import get_settings
from app.db import get_db
from app.models import Notification, NotificationStatus, Section, SectionStatus, Watch
from app.redis_client import get_redis, get_scrape_limiter
from app.schemas import HealthOut, ReadinessOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(status="ok")


@router.get("/readyz", response_model=ReadinessOut)
def readyz(
    response: Response,
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ReadinessOut:
    database = "ok"
    redis_state = "ok"

    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - report the failure, do not raise
        database = type(exc).__name__

    try:
        redis.ping()
    except Exception as exc:  # noqa: BLE001
        redis_state = type(exc).__name__

    ready = database == "ok" and redis_state == "ok"
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessOut(
        status="ready" if ready else "unavailable",
        database=database,
        redis=redis_state,
    )


@router.get("/stats")
def stats(
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, object]:
    """
    What the service is currently doing.

    `scrape_window_used` is the one to watch: if it is pinned at the limit,
    more sections are being watched than the polite request budget can cover,
    and detection lag is growing even though nothing is erroring.
    """
    settings = get_settings()

    def count(model: type, *conditions: ColumnElement[bool]) -> int:
        stmt = select(func.count()).select_from(model)
        for condition in conditions:
            stmt = stmt.where(condition)
        return int(db.execute(stmt).scalar_one())

    try:
        window_used = get_scrape_limiter(redis).current_usage()
    except Exception:  # noqa: BLE001 - stats must not 500 because Redis blipped
        window_used = -1

    return {
        "sections": {
            "total": count(Section),
            "open": count(Section, Section.status == SectionStatus.open),
            "closed": count(Section, Section.status == SectionStatus.closed),
            "unknown": count(Section, Section.status == SectionStatus.unknown),
        },
        "watches": {"active": count(Watch, Watch.active.is_(True))},
        "notifications": {
            "sent": count(Notification, Notification.status == NotificationStatus.sent),
            "pending": count(Notification, Notification.status == NotificationStatus.pending),
            "failed": count(Notification, Notification.status == NotificationStatus.failed),
        },
        "scrape_window_used": window_used,
        "scrape_window_limit": settings.scrape_requests_per_window,
        "scrape_window_seconds": settings.scrape_window_seconds,
    }

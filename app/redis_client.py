"""
One Redis connection pool for the process, plus the scraper's rate limiter
built on top of it.

`get_redis` is written as a function rather than a module-level constant so
FastAPI can override it with fakeredis in tests.
"""

from __future__ import annotations

from functools import lru_cache
from typing import cast

from redis import Redis

from app.config import get_settings
from app.rate_limit import SlidingWindowRateLimiter


@lru_cache
def get_redis() -> Redis:
    settings = get_settings()
    # redis-py 5.0 leaves from_url's return annotation off, so mypy infers None.
    return cast(Redis, Redis.from_url(settings.redis_url, decode_responses=True))


def get_scrape_limiter(redis: Redis | None = None) -> SlidingWindowRateLimiter:
    """
    The limiter guarding requests to the school's schedule.

    One key for the whole fleet -- the point is a cap on what *they* see, not
    a cap per worker.
    """
    settings = get_settings()
    return SlidingWindowRateLimiter(
        redis or get_redis(),
        "schedule-scrape",
        limit=settings.scrape_requests_per_window,
        window_seconds=settings.scrape_window_seconds,
    )

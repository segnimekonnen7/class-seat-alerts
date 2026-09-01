"""
A sliding-window rate limiter, shared across every worker process.

The school's course schedule is not mine to hammer. Whether one worker is
running or ten, the total request rate to their servers has to stay under one
number -- which is why the counter lives in Redis and not in a process-local
variable.

Sliding window rather than a fixed window: a fixed window lets a caller spend
its whole budget in the last second of one window and the whole next budget in
the first second of the next, which is a burst of double the configured rate
aimed at exactly the servers I promised not to hammer.

The implementation is a Redis sorted set keyed by timestamp:

  1. add this attempt
  2. drop everything older than the window
  3. count what is left
  4. if the count is over the limit, remove the attempt just added and refuse

Steps 1-4 run inside MULTI/EXEC, so two workers cannot both see room for the
last request.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, cast

from redis import Redis


class SlidingWindowRateLimiter:
    def __init__(
        self,
        redis: Redis,
        key: str,
        *,
        limit: int,
        window_seconds: int,
    ) -> None:
        self.redis = redis
        self.key = f"ratelimit:{key}"
        self.limit = limit
        self.window_seconds = window_seconds

    def acquire(self, *, now: float | None = None) -> bool:
        """
        Try to take one slot. Returns False if the window is full -- the caller
        decides whether to wait, skip, or reschedule, because the right answer
        differs between "poll a section" and "send an alert".
        """
        moment = time.time() if now is None else now
        cutoff = moment - self.window_seconds
        token = f"{moment}:{uuid.uuid4().hex}"

        pipe = self.redis.pipeline(transaction=True)
        pipe.zadd(self.key, {token: moment})
        pipe.zremrangebyscore(self.key, 0, cutoff)
        pipe.zcard(self.key)
        # Expire slightly past the window so an idle key cleans itself up
        # instead of sitting in Redis forever.
        pipe.expire(self.key, self.window_seconds * 2)
        # redis-py types execute() as a sync/async union; this client is sync.
        results = cast(list[Any], pipe.execute())

        count = int(results[2])
        if count > self.limit:
            self.redis.zrem(self.key, token)
            return False
        return True

    def current_usage(self, *, now: float | None = None) -> int:
        """How many slots are used in the window right now. Used by /health."""
        moment = time.time() if now is None else now
        self.redis.zremrangebyscore(self.key, 0, moment - self.window_seconds)
        return int(cast(int, self.redis.zcard(self.key)))

    def reset(self) -> None:
        self.redis.delete(self.key)

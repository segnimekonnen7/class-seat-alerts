"""
Rate limiter tests.

The limiter is what keeps this service from being a nuisance to the school, so
it gets tested on the property that matters: the number of requests allowed in
any rolling window, including across the boundary where a fixed-window counter
would let through double.
"""

from __future__ import annotations

import time

from app.rate_limit import SlidingWindowRateLimiter


def test_allows_up_to_the_limit(limiter):
    assert [limiter.acquire() for _ in range(3)] == [True, True, True]


def test_refuses_past_the_limit(limiter):
    for _ in range(3):
        limiter.acquire()
    assert limiter.acquire() is False


def test_a_refused_attempt_does_not_consume_a_slot(redis):
    """
    The implementation adds the attempt before counting, so a refusal has to
    remove it again. If it did not, a burst of refusals would keep the window
    permanently full and the limiter would never recover.
    """
    limiter = SlidingWindowRateLimiter(redis, "recovery", limit=2, window_seconds=60)
    now = time.time()

    assert limiter.acquire(now=now) is True
    assert limiter.acquire(now=now) is True
    for _ in range(10):
        assert limiter.acquire(now=now) is False

    # 61 seconds later the two real attempts have aged out and the ten
    # refusals left nothing behind.
    assert limiter.acquire(now=now + 61) is True


def test_the_window_slides(redis):
    limiter = SlidingWindowRateLimiter(redis, "slide", limit=2, window_seconds=60)
    start = time.time()

    assert limiter.acquire(now=start) is True
    assert limiter.acquire(now=start + 30) is True
    assert limiter.acquire(now=start + 40) is False

    # At start+61 the first attempt has left the window, freeing one slot --
    # but the one from start+30 is still inside it.
    assert limiter.acquire(now=start + 61) is True
    assert limiter.acquire(now=start + 62) is False


def test_a_fixed_window_burst_is_not_allowed(redis):
    """
    The failure a fixed-window counter has: spend the budget at the end of one
    window and the whole next budget at the start of the next, and the school
    sees double the configured rate in a couple of seconds. A sliding window
    counts the real rolling total instead.
    """
    limiter = SlidingWindowRateLimiter(redis, "burst", limit=3, window_seconds=60)
    start = time.time()

    for _ in range(3):
        assert limiter.acquire(now=start + 59) is True

    # One second later a fixed window would reset and allow three more.
    assert limiter.acquire(now=start + 60) is False


def test_usage_is_reported_for_monitoring(limiter):
    limiter.acquire()
    limiter.acquire()
    assert limiter.current_usage() == 2


def test_limiters_with_different_keys_do_not_share_a_budget(redis):
    one = SlidingWindowRateLimiter(redis, "a", limit=1, window_seconds=60)
    two = SlidingWindowRateLimiter(redis, "b", limit=1, window_seconds=60)

    assert one.acquire() is True
    assert two.acquire() is True
    assert one.acquire() is False


def test_the_budget_is_shared_across_limiter_instances(redis):
    """
    Two worker processes are two instances against one Redis. If the count were
    process-local, ten workers would mean ten times the promised request rate.
    """
    worker_one = SlidingWindowRateLimiter(redis, "shared", limit=2, window_seconds=60)
    worker_two = SlidingWindowRateLimiter(redis, "shared", limit=2, window_seconds=60)

    assert worker_one.acquire() is True
    assert worker_two.acquire() is True
    assert worker_one.acquire() is False


def test_reset_clears_the_window(limiter):
    limiter.acquire()
    limiter.reset()
    assert limiter.current_usage() == 0

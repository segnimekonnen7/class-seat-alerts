"""
Client tests.

These use httpx's MockTransport rather than patching the client, so the real
code path runs -- headers, status handling, rate-limit interaction -- against
a fake network instead of being replaced by a stub.
"""

from __future__ import annotations

import httpx
import pytest

from app.models import SectionStatus
from app.rate_limit import SlidingWindowRateLimiter
from app.scraper.client import RateLimited, ScheduleClient, ScheduleUnavailable
from tests.conftest import CRN, schedule_html


def transport_returning(status_code: int = 200, body: str = "") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body, request=request)

    return httpx.MockTransport(handler)


def test_fetches_and_parses_a_section(limiter):
    client = ScheduleClient(
        limiter,
        transport=transport_returning(200, schedule_html(seats_open=2, status_label="Open")),
    )
    observation = client.fetch_section("202610", CRN)

    assert observation.status is SectionStatus.open
    assert observation.seats_open == 2


def test_the_request_identifies_itself(limiter):
    """
    A scraper with no User-Agent is the kind that gets an IP banned. This
    asserts the header the config sets actually reaches the wire.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, text=schedule_html(), request=request)

    client = ScheduleClient(limiter, transport=httpx.MockTransport(handler))
    client.fetch_html("202610")

    assert "class-seat-alerts" in seen["user-agent"]


def test_the_term_is_in_the_url(limiter):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=schedule_html(), request=request)

    ScheduleClient(limiter, transport=httpx.MockTransport(handler)).fetch_html("202610")
    assert seen[0].endswith("/202610")


def test_a_server_error_is_schedule_unavailable(limiter):
    client = ScheduleClient(limiter, transport=transport_returning(503))
    with pytest.raises(ScheduleUnavailable):
        client.fetch_html("202610")


def test_a_not_found_is_schedule_unavailable(limiter):
    client = ScheduleClient(limiter, transport=transport_returning(404))
    with pytest.raises(ScheduleUnavailable):
        client.fetch_html("202610")


def test_a_network_error_is_schedule_unavailable(limiter):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    client = ScheduleClient(limiter, transport=httpx.MockTransport(handler))
    with pytest.raises(ScheduleUnavailable):
        client.fetch_html("202610")


def test_a_full_window_refuses_before_making_the_request(redis):
    """
    The limiter is checked before the request, not after. Checking afterwards
    would mean the request the limiter was supposed to prevent had already been
    sent.
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text=schedule_html(), request=request)

    limiter = SlidingWindowRateLimiter(redis, "tiny", limit=1, window_seconds=60)
    client = ScheduleClient(limiter, transport=httpx.MockTransport(handler))

    client.fetch_html("202610")
    with pytest.raises(RateLimited):
        client.fetch_html("202610")

    assert len(calls) == 1


def test_every_fetch_spends_a_slot(limiter):
    client = ScheduleClient(limiter, transport=transport_returning(200, schedule_html()))
    client.fetch_html("202610")
    client.fetch_html("202610")

    assert limiter.current_usage() == 2

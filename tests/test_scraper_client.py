"""
Client tests.

These use httpx's MockTransport rather than patching the client, so the real
code path runs -- the token round trip, the POST body, status handling,
rate-limit interaction -- against a fake network instead of being replaced by
a stub.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from app.models import SectionStatus
from app.rate_limit import SlidingWindowRateLimiter
from app.scraper.client import RateLimited, ScheduleClient, ScheduleUnavailable
from tests.conftest import CRN, TERM, fixture_html, schedule_html

TOKEN = "test-anti-forgery-token"
FORM_PAGE = f'<html><form><input name="__RequestVerificationToken" value="{TOKEN}"/></form></html>'


def mnsu_transport(
    results_html: str | None = None,
    *,
    form_html: str = FORM_PAGE,
    results_status: int = 200,
    form_status: int = 200,
    record: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    """
    Stands in for the schedule app: a GET serves the form, a POST the results.
    """
    body = results_html if results_html is not None else schedule_html()

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        if request.method == "GET":
            return httpx.Response(form_status, text=form_html, request=request)
        return httpx.Response(results_status, text=body, request=request)

    return httpx.MockTransport(handler)


# --- The happy path --------------------------------------------------------


def test_fetches_and_parses_a_section(limiter):
    client = ScheduleClient(
        limiter,
        transport=mnsu_transport(schedule_html(seats_open=2, status_label="Open")),
    )
    observation = client.fetch_section(TERM, CRN)

    assert observation.status is SectionStatus.open
    assert observation.seats_open == 2


def test_it_works_against_the_real_saved_page(limiter):
    """End to end against an actual response from secure2.mnsu.edu."""
    client = ScheduleClient(
        limiter, transport=mnsu_transport(fixture_html("mnsu_single_section.html"))
    )
    observation = client.fetch_section(TERM, "005217")

    assert observation.course_code == "CIS 113"
    assert observation.status is SectionStatus.open
    assert observation.seats_open == 1


def test_the_form_is_fetched_before_the_search(limiter):
    """The token and the session cookie are checked as a pair by ASP.NET."""
    seen: list[httpx.Request] = []
    ScheduleClient(limiter, transport=mnsu_transport(record=seen)).search_html(TERM)

    assert [r.method for r in seen] == ["GET", "POST"]


def test_the_anti_forgery_token_is_posted_back(limiter):
    seen: list[httpx.Request] = []
    ScheduleClient(limiter, transport=mnsu_transport(record=seen)).search_html(TERM)

    body = parse_qs(seen[1].content.decode())
    assert body["__RequestVerificationToken"] == [TOKEN]


def test_the_search_posts_the_term_and_course_id(limiter):
    seen: list[httpx.Request] = []
    ScheduleClient(limiter, transport=mnsu_transport(record=seen)).search_html(TERM, crn="005217")

    body = parse_qs(seen[1].content.decode())
    assert body["yrtr"] == [TERM]
    assert body["courseId"] == ["005217"]


def test_the_search_asks_for_all_sections_not_only_open_ones(limiter):
    """
    A closed section has to stay visible. "Open Sections Only" would make a
    full section vanish from results, which the parser would read as "your
    course id is gone" -- so the one thing being watched for could never be
    seen.
    """
    seen: list[httpx.Request] = []
    ScheduleClient(limiter, transport=mnsu_transport(record=seen)).search_html(TERM)

    assert parse_qs(seen[1].content.decode())["Command"] == ["All Sections"]


def test_the_request_identifies_itself(limiter):
    """A scraper with no User-Agent is the kind that gets an IP banned."""
    seen: list[httpx.Request] = []
    ScheduleClient(limiter, transport=mnsu_transport(record=seen)).search_html(TERM)

    assert "class-seat-alerts" in seen[0].headers["user-agent"]


# --- Failure handling ------------------------------------------------------


def test_a_server_error_is_schedule_unavailable(limiter):
    client = ScheduleClient(limiter, transport=mnsu_transport(results_status=503))
    with pytest.raises(ScheduleUnavailable):
        client.search_html(TERM)


def test_a_failed_form_fetch_is_schedule_unavailable(limiter):
    client = ScheduleClient(limiter, transport=mnsu_transport(form_status=500))
    with pytest.raises(ScheduleUnavailable):
        client.search_html(TERM)


def test_a_page_without_a_token_is_reported_as_unavailable(limiter):
    """
    No token means what came back was not the search form -- a maintenance
    notice or an SSO redirect. That is the site being down, not a section
    being missing, and the two are handled very differently upstream.
    """
    client = ScheduleClient(
        limiter, transport=mnsu_transport(form_html="<html><h1>Maintenance</h1></html>")
    )
    with pytest.raises(ScheduleUnavailable, match="anti-forgery"):
        client.search_html(TERM)


def test_a_network_error_is_schedule_unavailable(limiter):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    client = ScheduleClient(limiter, transport=httpx.MockTransport(handler))
    with pytest.raises(ScheduleUnavailable):
        client.search_html(TERM)


# --- Rate limiting ---------------------------------------------------------


def test_a_full_window_refuses_before_making_the_request(redis):
    """
    The limiter is checked before the request, not after. Checking afterwards
    would mean the request it was supposed to prevent had already been sent.
    """
    seen: list[httpx.Request] = []
    limiter = SlidingWindowRateLimiter(redis, "tiny", limit=1, window_seconds=60)
    client = ScheduleClient(limiter, transport=mnsu_transport(record=seen))

    with pytest.raises(RateLimited):
        client.search_html(TERM)

    assert seen == []


def test_a_search_spends_two_slots(redis):
    """One for the form, one for the results -- both hit the school."""
    limiter = SlidingWindowRateLimiter(redis, "count", limit=10, window_seconds=60)
    ScheduleClient(limiter, transport=mnsu_transport()).search_html(TERM)

    assert limiter.current_usage() == 2

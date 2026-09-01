"""
Fetching from the MNSU class schedule.

The schedule is not a GET-a-URL affair. It is an ASP.NET MVC form:

  1. GET /ClassSchedule/ -- returns the search form and sets a session cookie,
     carrying a hidden `__RequestVerificationToken`.
  2. POST /ClassSchedule/ -- with that token, the matching cookie, and the
     search fields. The token and the cookie are checked as a pair, so both
     have to come from the same httpx.Client.

Searching by Course ID returns just the one section (~23KB) instead of a whole
subject (~205KB). Since the poller checks one watched section at a time, that
is a tenth of the bytes off the school's servers per check -- which is the
difference between a service they would never notice and one they might.

The client is otherwise built to be a well-behaved robot:

  - it identifies itself in the User-Agent, with a contact address
  - it takes a slot from the shared rate limiter before every request, so the
    school sees one bounded request rate however many workers are running
  - it has a timeout, because a request that hangs forever holds a worker slot
    that a section which is actually opening needs

Retries are deliberately *not* here. Celery owns retry and backoff; doing it
in both places multiplies into far more requests than either intended.
"""

from __future__ import annotations

import logging

import httpx
from bs4 import BeautifulSoup, Tag

from app.config import get_settings
from app.rate_limit import SlidingWindowRateLimiter
from app.scraper.parser import Observation, parse_section

logger = logging.getLogger(__name__)

TOKEN_FIELD = "__RequestVerificationToken"  # noqa: S105 - a form field name, not a secret


class RateLimited(RuntimeError):
    """The shared window is full. Try again shortly -- this is not an error."""


class ScheduleUnavailable(RuntimeError):
    """The school's site could not be reached, or answered with an error."""


class ScheduleClient:
    """
    Fetches and parses one section.

    `transport` exists so tests can hand in an httpx MockTransport and exercise
    the real client -- the token round trip, the POST body, status handling --
    without a network. Mocking out the client itself would leave exactly this
    code untested.
    """

    def __init__(
        self,
        limiter: SlidingWindowRateLimiter,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self.limiter = limiter
        # The form posts back to the same path it was served from.
        self.url = settings.schedule_base_url.rstrip("/") + "/"
        self.timeout = settings.schedule_timeout_seconds
        self.headers = {"User-Agent": settings.schedule_user_agent}
        self._transport = transport

    def _client(self) -> httpx.Client:
        # follow_redirects because the app bounces / -> /ClassSchedule/.
        return httpx.Client(
            timeout=self.timeout,
            headers=self.headers,
            transport=self._transport,
            follow_redirects=True,
        )

    def _spend_slot(self) -> None:
        if not self.limiter.acquire():
            raise RateLimited("scrape window is full")

    @staticmethod
    def _extract_token(html: str) -> str:
        """
        Pull the anti-forgery token out of the search form.

        Its absence means the page served was not the search form -- a
        maintenance notice, an outage page, an SSO redirect -- so this is
        reported as the site being unavailable rather than as a parse failure
        of a section that may be perfectly fine.
        """
        field = BeautifulSoup(html, "html.parser").find("input", {"name": TOKEN_FIELD})
        if not isinstance(field, Tag) or not field.get("value"):
            raise ScheduleUnavailable(
                "Search form did not contain an anti-forgery token; "
                "the schedule site is probably down or redirecting."
            )
        return str(field["value"])

    def search_html(self, term: str, crn: str = "", subject: str = "") -> str:
        """
        Run one search and return the results HTML.

        Pass `crn` (an MNSU Course ID) for a single section, or `subject` for a
        whole department. The token GET and the results POST share one client
        so they share the session cookie the token is bound to.
        """
        # Both requests hit the school, so both are counted against the budget.
        self._spend_slot()
        self._spend_slot()

        try:
            with self._client() as client:
                form_page = client.get(self.url)
                if form_page.status_code >= 400:
                    raise ScheduleUnavailable(
                        f"schedule form returned HTTP {form_page.status_code}"
                    )

                response = client.post(
                    self.url,
                    data={
                        TOKEN_FIELD: self._extract_token(form_page.text),
                        "yrtr": term,
                        "subj": subject,
                        "courseId": crn,
                        "courseNumber": "",
                        "division_code": "",
                        "campusId": "1      ",
                        "classLevelId": "",
                        "startTimeId": "ARR",
                        "endTimeId": "ARR",
                        "days": "All",
                        # "All Sections", not "Open Sections Only": a closed
                        # section has to stay visible, because watching it
                        # close and then open again is the entire point.
                        "Command": "All Sections",
                    },
                )
        except httpx.HTTPError as exc:
            raise ScheduleUnavailable(f"request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ScheduleUnavailable(
                f"schedule search returned HTTP {response.status_code} for term {term}"
            )

        return response.text

    def fetch_section(self, term: str, crn: str) -> Observation:
        """Search for one Course ID and parse the section out of the result."""
        return parse_section(self.search_html(term, crn=crn), crn)

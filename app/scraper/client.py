"""
Fetching the schedule page.

The client's whole job is being a polite, well-behaved robot:

  - it identifies itself in the User-Agent, with a contact address
  - it takes a slot from the shared rate limiter before every request, so the
    school sees one bounded request rate regardless of how many workers are up
  - it has a timeout, because a request that hangs forever holds a worker slot
    that a section which is actually opening needs

Retries are deliberately *not* here. Celery owns retry and backoff -- doing it
in both places multiplies into far more requests than either intended.
"""

from __future__ import annotations

import logging

import httpx

from app.config import get_settings
from app.rate_limit import SlidingWindowRateLimiter
from app.scraper.parser import Observation, parse_section

logger = logging.getLogger(__name__)


class RateLimited(RuntimeError):
    """The shared window is full. Try again shortly -- this is not an error."""


class ScheduleUnavailable(RuntimeError):
    """The school's site could not be reached, or answered with an error."""


class ScheduleClient:
    """
    Fetches and parses one section.

    `transport` exists so tests can hand in an httpx MockTransport and exercise
    the real client -- headers, status handling, timeouts -- without a network.
    Mocking out the client itself would leave exactly this code untested.
    """

    def __init__(
        self,
        limiter: SlidingWindowRateLimiter,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self.limiter = limiter
        self.base_url = settings.schedule_base_url.rstrip("/")
        self.timeout = settings.schedule_timeout_seconds
        self.headers = {"User-Agent": settings.schedule_user_agent}
        self._transport = transport

    def _url(self, term: str) -> str:
        return f"{self.base_url}/{term}"

    def fetch_html(self, term: str) -> str:
        if not self.limiter.acquire():
            raise RateLimited("scrape window is full")

        try:
            with httpx.Client(
                timeout=self.timeout, headers=self.headers, transport=self._transport
            ) as client:
                response = client.get(self._url(term))
        except httpx.HTTPError as exc:
            raise ScheduleUnavailable(f"request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ScheduleUnavailable(
                f"schedule returned HTTP {response.status_code} for term {term}"
            )

        return response.text

    def fetch_section(self, term: str, crn: str) -> Observation:
        """Fetch the term page and pull one section out of it."""
        return parse_section(self.fetch_html(term), crn)

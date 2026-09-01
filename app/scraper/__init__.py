"""Fetching and parsing the university course schedule."""

from app.scraper.client import RateLimited, ScheduleClient, ScheduleUnavailable
from app.scraper.parser import (
    Observation,
    ScheduleFormatChanged,
    SectionNotOnPage,
    parse_all_sections,
    parse_section,
)

__all__ = [
    "Observation",
    "RateLimited",
    "ScheduleClient",
    "ScheduleFormatChanged",
    "ScheduleUnavailable",
    "SectionNotOnPage",
    "parse_all_sections",
    "parse_section",
]

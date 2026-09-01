"""
Turning a course-schedule page into a seat count.

This is the part of the system most likely to break, because it depends on
someone else's HTML. Two decisions follow from that:

  1. Parsing is separated from fetching. The parser is a pure function from
     HTML string to Observation, so it is tested against saved fixture pages
     with no network involved -- and when the school changes their markup, the
     fix is one function and the fixture that proves it.

  2. A page that does not contain the section is a distinct error from a page
     that could not be fetched. A removed CRN should stop being polled; a
     network blip should be retried. Collapsing both into "failed" would mean
     either retrying a dead CRN forever or dropping a section over one timeout.

The markup shape below is a table with one row per section:

    <table class="section-list">
      <tr data-crn="10432">
        <td class="course">CS 320</td>
        <td class="title">Software Engineering</td>
        <td class="instructor">A. Rivera</td>
        <td class="seats-open">3</td>
        <td class="seats-total">30</td>
        <td class="status">Open</td>
      </tr>
    </table>
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from app.models import SectionStatus


class SectionNotOnPage(LookupError):
    """The page parsed fine but does not list this CRN."""


class ScheduleFormatChanged(ValueError):
    """The page does not look like a schedule page at all."""


@dataclass(frozen=True)
class Observation:
    """What one poll saw. Deliberately plain -- no ORM objects in the parser."""

    crn: str
    course_code: str
    title: str
    instructor: str
    seats_open: int
    seats_total: int
    status: SectionStatus


_DIGITS = re.compile(r"-?\d+")


def _text(row: Tag, class_name: str) -> str:
    cell = row.find(class_=class_name)
    return cell.get_text(strip=True) if cell else ""


def _int(row: Tag, class_name: str) -> int:
    """
    Pull an integer out of a cell.

    Schedules write seat counts as "3", "3 of 30", and sometimes "FULL". A cell
    with no digits is 0 rather than an exception -- "FULL" is information, not
    a parse failure.
    """
    match = _DIGITS.search(_text(row, class_name))
    return int(match.group(0)) if match else 0


def _status(row: Tag, seats_open: int) -> SectionStatus:
    """
    Trust the page's own status label when it has one, and fall back to the
    seat count when it does not.

    The label is better than the arithmetic: a section can show a free seat and
    still be closed for registration -- reserved seats, a department hold, a
    waitlist that has to clear first. Telling a student to go register in that
    situation wastes the one thing the alert was supposed to save them.
    """
    label = _text(row, "status").lower()
    if "open" in label:
        return SectionStatus.open
    if "closed" in label or "full" in label or "waitlist" in label:
        return SectionStatus.closed
    return SectionStatus.open if seats_open > 0 else SectionStatus.closed


def parse_section(html: str, crn: str) -> Observation:
    """
    Find one CRN on a schedule page.

    Raises ScheduleFormatChanged if the page has no section table at all, and
    SectionNotOnPage if the table is there but this CRN is not.
    """
    soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table", class_="section-list")
    # NavigableString is a str subclass, so `is None` alone is not enough of a
    # check -- a stray text node would sail through and then fail on .find with
    # a confusing str method error instead of a clear format-change signal.
    if not isinstance(table, Tag):
        raise ScheduleFormatChanged(
            "No table.section-list on the page -- the schedule markup has changed."
        )

    row = table.find("tr", attrs={"data-crn": crn})
    if row is None or not isinstance(row, Tag):
        raise SectionNotOnPage(crn)

    seats_open = _int(row, "seats-open")
    return Observation(
        crn=crn,
        course_code=_text(row, "course"),
        title=_text(row, "title"),
        instructor=_text(row, "instructor"),
        seats_open=seats_open,
        seats_total=_int(row, "seats-total"),
        status=_status(row, seats_open),
    )


def parse_all_sections(html: str) -> list[Observation]:
    """Every section on the page. Used to seed a term without knowing CRNs."""
    soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table", class_="section-list")
    if not isinstance(table, Tag):
        raise ScheduleFormatChanged("No table.section-list on the page.")

    observations: list[Observation] = []
    for row in table.find_all("tr"):
        if not isinstance(row, Tag):
            continue
        crn = row.get("data-crn")
        if not crn:
            continue
        seats_open = _int(row, "seats-open")
        observations.append(
            Observation(
                crn=str(crn),
                course_code=_text(row, "course"),
                title=_text(row, "title"),
                instructor=_text(row, "instructor"),
                seats_open=seats_open,
                seats_total=_int(row, "seats-total"),
                status=_status(row, seats_open),
            )
        )
    return observations

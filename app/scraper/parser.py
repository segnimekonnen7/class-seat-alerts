"""
Parsing the Minnesota State Mankato class schedule.

Target: https://secure2.mnsu.edu/ClassSchedule/ -- an ASP.NET MVC app that
renders one <table> per course, with one <tr> per section.

    <h5>CIS  113  - Health Humanities and Health Informatics     (4  Credits)</h5>
    <table class="table table-striped ...">
      <thead><tr>
        <th>Course ID</th><th>Sect</th><th>Delivery Method</th><th>Grade Meth</th>
        <th>Days</th><th>Time</th><th>Dates</th><th>Bldg/Room</th>
        <th>Instructor</th><th>Size</th><th>Enrl</th><th>Status</th><th>AddlInfo</th>
      </tr></thead>
      <tbody>
        <tr>
          <td>002948</td><td>01</td> ... <td>80</td><td>80</td>
          <td><span class="openSession">Closed</span></td><td></td>
        </tr>
        <tr><td colspan="13">Notes for the previous row ...</td></tr>
      </tbody>
    </table>

Three things about this page drive the whole implementation.

**The CSS classes on the status are inverted.** This is not a typo here:

    <span class="openSession">Closed</span>      <- 4 on the sample page
    <span class="CloseSession">Open</span>       <- 56 on the sample page

Every single one. A parser that keyed on `class="openSession"` would read the
entire schedule exactly backwards -- alerting students the moment a section
*closed* and staying silent when one opened. So the status comes from the
span's **text**, and the class is deliberately never consulted.

**There is no seats-available column.** The page gives Size (capacity) and
Enrl (enrolled); open seats is the difference, floored at zero because a
section can be enrolled past its cap.

**Columns are located by header name, not position.** The 13 columns are
identical across every table today, but positional indexing fails silently if
the school inserts a column -- you would start reading "Bldg/Room" as the
instructor and "Enrl" as the capacity, and the numbers would still parse. The
header row is read first and the indices come from it, so a layout change
raises ScheduleFormatChanged instead of quietly producing wrong seat counts.

MNSU labels the section identifier "Course ID" (e.g. 002948). That is this
system's `crn` -- the same concept under the name most other schools use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from app.models import SectionStatus


class SectionNotOnPage(LookupError):
    """The page parsed fine but does not list this course id."""


class ScheduleFormatChanged(ValueError):
    """The page is not a schedule results page, or its columns moved."""


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
    section: str = ""


# Required columns, as they appear in the <thead>, normalized to lowercase.
REQUIRED_COLUMNS = ("course id", "sect", "instructor", "size", "enrl", "status")

# "CIS  113  - Health Humanities and Health Informatics    (4   Credits)"
_HEADING = re.compile(
    r"^\s*(?P<subject>[A-Z]{2,5})\s+(?P<number>[\w-]+)\s*-\s*(?P<title>.+?)"
    r"(?:\s*\(\s*[\d\-]+\s*Credits?\s*\))?\s*$"
)

_DIGITS = re.compile(r"-?\d+")


def _clean(text: str) -> str:
    """Collapse whitespace, including the non-breaking spaces this page uses."""
    return " ".join(text.replace("\xa0", " ").split())


def _cell_text(cells: list[Tag], position: int | None) -> str:
    if position is None or position >= len(cells):
        return ""
    return _clean(cells[position].get_text(" ", strip=True))


def _cell_int(cells: list[Tag], position: int | None) -> int:
    match = _DIGITS.search(_cell_text(cells, position))
    return int(match.group(0)) if match else 0


def _column_index(table: Tag) -> dict[str, int]:
    """
    Map header name -> column index for one course table.

    Raises ScheduleFormatChanged if a column this parser depends on is absent,
    which is the signal that the page layout moved and the parser needs
    updating -- far better than reading the wrong cell and believing it.
    """
    head = table.find("thead")
    if not isinstance(head, Tag):
        raise ScheduleFormatChanged("Results table has no <thead>.")

    index = {
        _clean(th.get_text(" ", strip=True)).lower(): position
        for position, th in enumerate(head.find_all("th"))
    }

    missing = [name for name in REQUIRED_COLUMNS if name not in index]
    if missing:
        raise ScheduleFormatChanged(
            f"Schedule table is missing expected column(s): {', '.join(missing)}. "
            f"Found: {', '.join(sorted(index))}"
        )
    return index


def _parse_heading(table: Tag) -> tuple[str, str]:
    """
    Course code and title from the <h5> above the table.

    The first <h5> on the page is "Your Course Search Results For Fall 2026",
    which is not a course; it simply fails the pattern and yields blanks.
    """
    heading = table.find_previous("h5")
    if not isinstance(heading, Tag):
        return "", ""

    match = _HEADING.match(_clean(heading.get_text(" ", strip=True)))
    if match is None:
        return "", ""

    course_code = f"{match.group('subject')} {match.group('number')}"
    return course_code, _clean(match.group("title"))


def _status_from_cell(cells: list[Tag], index: dict[str, int]) -> SectionStatus:
    """
    Read the status from the cell's text.

    Never from the CSS class: on this site `openSession` labels a *Closed*
    section and `CloseSession` labels an *Open* one, consistently. Keying on
    the class would invert every section on the schedule.
    """
    label = _cell_text(cells, index.get("status")).lower()

    if "open" in label:
        return SectionStatus.open
    if any(word in label for word in ("closed", "full", "wait", "cancel")):
        return SectionStatus.closed

    # No recognizable label: fall back to the seat arithmetic.
    size = _cell_int(cells, index.get("size"))
    enrolled = _cell_int(cells, index.get("enrl"))
    return SectionStatus.open if size - enrolled > 0 else SectionStatus.closed


def _is_section_row(row: Tag) -> bool:
    """
    Section rows only.

    A section may be followed by a notes row -- a single cell with
    colspan="13" carrying "Diverse Cultures - Purple" and the like. Those have
    to be skipped or they parse as a section with no id and zero seats.
    """
    cells = row.find_all("td")
    if not cells:
        return False
    return not cells[0].has_attr("colspan")


def _observation_from_row(row: Tag, index: dict[str, int], heading: tuple[str, str]) -> Observation:
    cells = row.find_all("td")
    course_code, title = heading

    size = _cell_int(cells, index.get("size"))
    enrolled = _cell_int(cells, index.get("enrl"))
    # A section can be enrolled past its cap, which would otherwise report a
    # negative number of free seats.
    seats_open = max(size - enrolled, 0)

    return Observation(
        crn=_cell_text(cells, index.get("course id")),
        section=_cell_text(cells, index.get("sect")),
        course_code=course_code,
        title=title,
        instructor=_cell_text(cells, index.get("instructor")),
        seats_open=seats_open,
        seats_total=size,
        status=_status_from_cell(cells, index),
    )


def _result_tables(soup: BeautifulSoup) -> list[Tag]:
    return [
        table
        for table in soup.find_all("table")
        if isinstance(table, Tag) and table.find("thead") is not None
    ]


def parse_all_sections(html: str) -> list[Observation]:
    """
    Every section on a results page.

    Raises ScheduleFormatChanged if the page carries no results table at all --
    a login redirect, a maintenance page, or an error body served with a 200,
    each of which must not be mistaken for "your section is gone".
    """
    soup = BeautifulSoup(html, "html.parser")

    tables = _result_tables(soup)
    if not tables:
        raise ScheduleFormatChanged(
            "No results table on the page -- the schedule layout changed, or "
            "this is not a search-results response."
        )

    observations: list[Observation] = []
    for table in tables:
        index = _column_index(table)
        heading = _parse_heading(table)
        for row in table.find_all("tr"):
            if not _is_section_row(row):
                continue
            observation = _observation_from_row(row, index, heading)
            if observation.crn:
                observations.append(observation)

    return observations


def parse_section(html: str, crn: str) -> Observation:
    """
    One section, by MNSU Course ID.

    Raises SectionNotOnPage if the results table exists but does not list this
    id -- a distinct outcome from a failed fetch, because a course id removed
    from the schedule should stop being polled while a network blip should
    simply be retried.
    """
    wanted = crn.strip()
    for observation in parse_all_sections(html):
        # Course IDs are zero-padded on the page ("002948"); accept "2948" too
        # so a watch created by hand still matches.
        if observation.crn == wanted or observation.crn.lstrip("0") == wanted.lstrip("0"):
            return observation

    raise SectionNotOnPage(crn)

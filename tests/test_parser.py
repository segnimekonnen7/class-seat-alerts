"""
Parser tests.

The parser is the part most likely to break, because it depends on someone
else's HTML. These run against fixture pages with no network involved.
"""

from __future__ import annotations

import pytest

from app.models import SectionStatus
from app.scraper.parser import (
    ScheduleFormatChanged,
    SectionNotOnPage,
    parse_all_sections,
    parse_section,
)
from tests.conftest import CRN, schedule_html


def test_parses_an_open_section():
    observation = parse_section(schedule_html(seats_open=3, status_label="Open"), CRN)
    assert observation.status is SectionStatus.open
    assert observation.seats_open == 3
    assert observation.seats_total == 30


def test_parses_a_closed_section():
    observation = parse_section(schedule_html(seats_open=0, status_label="Closed"), CRN)
    assert observation.status is SectionStatus.closed


def test_parses_course_metadata():
    observation = parse_section(schedule_html(), CRN)
    assert observation.course_code == "CS 320"
    assert observation.title == "Software Engineering"
    assert observation.instructor == "A. Rivera"


def test_the_page_label_beats_the_seat_arithmetic():
    """
    A section can show a free seat and still be closed -- reserved seats, a
    department hold, a waitlist that has to clear. Telling a student to go
    register in that situation wastes the seconds the alert exists to save.
    """
    observation = parse_section(schedule_html(seats_open=2, status_label="Closed"), CRN)
    assert observation.status is SectionStatus.closed


def test_falls_back_to_seat_count_when_there_is_no_label():
    html = schedule_html(seats_open=4, status_label="")
    assert parse_section(html, CRN).status is SectionStatus.open


def test_waitlist_counts_as_closed():
    html = schedule_html(seats_open=1, status_label="Waitlist")
    assert parse_section(html, CRN).status is SectionStatus.closed


def test_seat_counts_written_as_prose_still_parse():
    """Schedules write "3 of 30" as often as they write "3"."""
    html = schedule_html(seats_open=0, status_label="Open").replace(
        '<td class="seats-open">0</td>', '<td class="seats-open">3 of 30</td>'
    )
    assert parse_section(html, CRN).seats_open == 3


def test_a_non_numeric_seat_cell_is_zero_not_an_error():
    """ "FULL" in the seats column is information, not a parse failure."""
    html = schedule_html(status_label="Closed").replace(
        '<td class="seats-open">0</td>', '<td class="seats-open">FULL</td>'
    )
    assert parse_section(html, CRN).seats_open == 0


def test_a_missing_crn_raises_section_not_on_page():
    """Distinct from a fetch failure: a removed CRN should stop being polled."""
    with pytest.raises(SectionNotOnPage):
        parse_section(schedule_html(crn="99999"), CRN)


def test_a_page_without_the_table_raises_format_changed():
    with pytest.raises(ScheduleFormatChanged):
        parse_section("<html><body><p>Maintenance window</p></body></html>", CRN)


def test_an_error_page_is_a_format_change_not_a_missing_section():
    """
    A login redirect or a 200-with-error-body must not be read as "your CRN is
    gone", which would back the section off for an hour over a blip.
    """
    with pytest.raises(ScheduleFormatChanged):
        parse_section("<html><body><h1>Session expired</h1></body></html>", CRN)


def test_parse_all_sections_returns_every_row():
    html = """
    <table class="section-list">
      <tr data-crn="1"><td class="course">A 1</td><td class="seats-open">1</td>
          <td class="seats-total">10</td><td class="status">Open</td></tr>
      <tr data-crn="2"><td class="course">B 2</td><td class="seats-open">0</td>
          <td class="seats-total">10</td><td class="status">Closed</td></tr>
    </table>
    """
    observations = parse_all_sections(html)
    assert [o.crn for o in observations] == ["1", "2"]
    assert [o.status for o in observations] == [SectionStatus.open, SectionStatus.closed]


def test_parse_all_sections_skips_header_rows():
    """A <tr> with no data-crn is a header, not a section."""
    html = """
    <table class="section-list">
      <tr><th>Course</th><th>Seats</th></tr>
      <tr data-crn="1"><td class="course">A 1</td><td class="seats-open">1</td>
          <td class="seats-total">10</td><td class="status">Open</td></tr>
    </table>
    """
    assert len(parse_all_sections(html)) == 1

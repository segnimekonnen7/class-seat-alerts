"""
Parser tests, run against real pages saved from secure2.mnsu.edu.

The fixtures in tests/fixtures/ are actual responses from the MNSU class
schedule (scripts and styles stripped, results trimmed to a few courses). They
are the contract: when the school changes their markup, one of these tests
fails and the fixture gets refreshed alongside the fix.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from app.models import SectionStatus
from app.scraper.parser import (
    ScheduleFormatChanged,
    SectionNotOnPage,
    parse_all_sections,
    parse_section,
)
from tests.conftest import fixture_html

# From the saved page: CIS 113 section 01, capacity 80, enrolled 80 -> Closed.
CLOSED_CRN = "002948"
# CIS 113 section 03, capacity 50, enrolled 49 -> Open, 1 seat.
OPEN_CRN = "005217"


# --- Real pages ------------------------------------------------------------


def test_parses_a_closed_section_from_the_real_page():
    observation = parse_section(fixture_html("mnsu_search_results.html"), CLOSED_CRN)

    assert observation.status is SectionStatus.closed
    assert observation.seats_total == 80
    assert observation.seats_open == 0


def test_parses_an_open_section_from_the_real_page():
    observation = parse_section(fixture_html("mnsu_search_results.html"), OPEN_CRN)

    assert observation.status is SectionStatus.open
    assert observation.seats_total == 50
    assert observation.seats_open == 1


def test_seats_open_is_capacity_minus_enrolled():
    """The page has no seats-available column; it has Size and Enrl."""
    observation = parse_section(fixture_html("mnsu_search_results.html"), OPEN_CRN)
    assert observation.seats_open == observation.seats_total - 49


def test_parses_the_course_code_and_title_from_the_heading():
    observation = parse_section(fixture_html("mnsu_search_results.html"), CLOSED_CRN)

    assert observation.course_code == "CIS 113"
    assert observation.title == "Health Humanities and Health Informatics"


def test_the_credits_suffix_is_not_part_of_the_title():
    """The heading reads '... Informatics    (4        Credits)'."""
    observation = parse_section(fixture_html("mnsu_search_results.html"), CLOSED_CRN)
    assert "Credit" not in observation.title


def test_parses_the_instructor_and_section_number():
    observation = parse_section(fixture_html("mnsu_search_results.html"), CLOSED_CRN)

    assert observation.instructor == "Kruse, Sarah"
    assert observation.section == "01"


def test_a_single_course_id_search_parses():
    """The response the poller actually gets: one course, one row."""
    observation = parse_section(fixture_html("mnsu_single_section.html"), OPEN_CRN)

    assert observation.crn == OPEN_CRN
    assert observation.status is SectionStatus.open


def test_parse_all_sections_reads_every_row_on_the_page():
    observations = parse_all_sections(fixture_html("mnsu_search_results.html"))

    assert len(observations) >= 3
    assert all(o.crn for o in observations)


# --- The inverted CSS classes ----------------------------------------------


def test_the_status_classes_on_the_real_page_are_inverted():
    """
    Documents the trap rather than trusting a comment about it.

    On this site `class="openSession"` labels a *Closed* section and
    `class="CloseSession"` labels an *Open* one -- consistently, on every row.
    If MNSU ever fixes their class names this test fails, which is the signal
    to re-read the parser's assumption, not to "fix" the parser.
    """
    soup = BeautifulSoup(fixture_html("mnsu_search_results.html"), "html.parser")

    pairs = {
        (" ".join(span.get("class") or []), span.get_text(strip=True))
        for span in soup.find_all("span")
        if "ession" in " ".join(span.get("class") or [])
    }

    assert ("openSession", "Closed") in pairs
    assert ("CloseSession", "Open") in pairs


def test_status_comes_from_the_text_not_the_class():
    """
    The consequence of the above. A parser keying on the class name would read
    every section backwards -- alerting the moment a section closed and staying
    silent when one opened.
    """
    soup = BeautifulSoup(fixture_html("mnsu_search_results.html"), "html.parser")
    row = soup.find("span", class_="openSession").find_parent("tr")
    crn = row.find("td").get_text(strip=True)

    # The class says "openSession"; the section is closed.
    assert parse_section(fixture_html("mnsu_search_results.html"), crn).status is (
        SectionStatus.closed
    )


# --- Structural hazards ----------------------------------------------------


def test_notes_rows_are_not_mistaken_for_sections():
    """
    Sections are followed by a colspan="13" notes row ("Diverse Cultures -
    Purple"). Parsed as a section it would be a zero-seat row with no id.
    """
    observations = parse_all_sections(fixture_html("mnsu_search_results.html"))
    assert all(o.crn.isdigit() for o in observations)


def test_a_missing_column_is_a_format_change_not_a_silent_misread():
    """
    Columns are located by header name. If the school inserts one, positional
    indexing would read Bldg/Room as the instructor and Enrl as the capacity --
    and the numbers would still parse, so nothing would look wrong.
    """
    soup = BeautifulSoup(fixture_html("mnsu_search_results.html"), "html.parser")
    for header in soup.find_all("th"):
        if header.get_text(strip=True) == "Enrl":
            header.string = "Registered"

    with pytest.raises(ScheduleFormatChanged, match="enrl"):
        parse_all_sections(str(soup))


def test_a_missing_course_id_raises_section_not_on_page():
    """Distinct from a fetch failure: a removed id should stop being polled."""
    with pytest.raises(SectionNotOnPage):
        parse_section(fixture_html("mnsu_search_results.html"), "999999")


def test_a_page_with_no_results_table_is_a_format_change():
    with pytest.raises(ScheduleFormatChanged):
        parse_all_sections("<html><body><p>Scheduled maintenance</p></body></html>")


def test_a_login_redirect_is_not_read_as_a_missing_section():
    """
    An SSO page served with a 200 must not back a live section off for an hour
    on the theory that its id disappeared.
    """
    with pytest.raises(ScheduleFormatChanged):
        parse_section("<html><body><h1>Sign in to continue</h1></body></html>", OPEN_CRN)


def test_a_zero_padded_course_id_matches_an_unpadded_watch():
    """The page prints '002948'; a student typing '2948' still gets matched."""
    observation = parse_section(fixture_html("mnsu_search_results.html"), "2948")
    assert observation.crn == CLOSED_CRN


def test_non_breaking_spaces_are_cleaned_out():
    """The page uses \\xa0 liberally; they must not end up inside values."""
    for observation in parse_all_sections(fixture_html("mnsu_search_results.html")):
        assert "\xa0" not in observation.title
        assert "\xa0" not in observation.instructor


def test_an_overenrolled_section_reports_zero_seats_not_a_negative():
    soup = BeautifulSoup(fixture_html("mnsu_single_section.html"), "html.parser")
    # Size is column 9, Enrl column 10. Enrol 55 into a 50-seat section.
    row = soup.find("td", string=lambda t: t and t.strip() == OPEN_CRN).find_parent("tr")
    row.find_all("td")[10].string = "55"

    assert parse_section(str(soup), OPEN_CRN).seats_open == 0


def test_an_unlabelled_status_falls_back_to_the_seat_count():
    soup = BeautifulSoup(fixture_html("mnsu_single_section.html"), "html.parser")
    for span in soup.find_all("span", class_="CloseSession"):
        span.decompose()

    # Capacity 50, enrolled 49 -> a seat is free.
    assert parse_section(str(soup), OPEN_CRN).status is SectionStatus.open

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import SOURCES
from src.parser import UpstreamFormatError, parse_source
from src.tracker import location_matches, normalize_location


FIXTURES = Path(__file__).parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_internship_parser_extracts_every_category_and_apply_url(caplog: pytest.LogCaptureFixture) -> None:
    jobs = parse_source(read_fixture("internships.md"), SOURCES[0])

    assert {job.category for job in jobs} == {"FAANG+", "Quant", "Other"}
    assert {job.job_type for job in jobs} == {"Internship"}
    figma = next(job for job in jobs if job.company == "Figma")
    assert figma.application_url == "https://boards.example.test/figma/123?gh_jid=123&source=fixture"
    assert figma.salary == "$60/hr"
    assert figma.age == "1d"
    assert "https://www.figma.com" not in figma.application_url
    notion = next(job for job in jobs if job.company == "Notion")
    assert notion.category == "Other"
    assert notion.salary is None
    assert "missing company, position, location, or apply URL" in caplog.text


def test_new_grad_parser_preserves_type_and_optional_salary() -> None:
    jobs = parse_source(read_fixture("new_grads.md"), SOURCES[1])

    assert {job.category for job in jobs} == {"FAANG+", "Quant", "Other"}
    assert {job.job_type for job in jobs} == {"New Grad"}
    twitch = next(job for job in jobs if job.company == "Twitch")
    assert twitch.salary == "$193k/yr"
    notion = next(job for job in jobs if job.company == "Notion")
    assert notion.salary is None
    assert notion.application_url == "https://jobs.example.test/notion-grad/789"


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("San Francisco, CA", True),
        ("SF, CA", True),
        ("S.F., California", True),
        ("South SF, CA", True),
        ("San Francisco (Hybrid), CA", True),
        ("San Francisco County, CA", True),
        ("San Francisco, CA +1", True),
        ("South San Francisco, CA", True),
        ("San José, CA", True),
        ("San Mateo, CA", True),
        ("San Jose, CA", True),
        ("Santa Clara, CA", True),
        ("Berkeley, CA", True),
        ("Fremont, CA", True),
        ("Sunnyvale, CA", True),
        ("Remote - Palo Alto, CA", True),
        ("San Francisco Bay Area", True),
        ("S.F. Bay Area", True),
        ("San Francisco Bay Area, NY", False),
        ("Bay Area, CA", True),
        ("Silicon Valley, California", True),
        ("Seattle, WA", False),
        ("San Jose, Costa Rica", False),
        ("Fremont, NE", False),
        ("SF, NY", False),
        ("SFO, CA", False),
        ("Remote", False),
        ("California", False),
        ("Santa Cruz, CA", False),
        ("Morgan Hill, CA", False),
        ("Gilroy, CA", False),
        ("Livermore, CA", False),
    ],
)
def test_location_filter_normalizes_and_matches_the_bay_area_scope(
    location: str, expected: bool
) -> None:
    assert location_matches(location) is expected


def test_location_normalization_is_comparison_only_and_handles_accents() -> None:
    assert normalize_location("  S.F.,   California  ") == "s f california"
    assert normalize_location("San José, CA") == "san jose ca"


def test_missing_marker_fails_loudly() -> None:
    broken = read_fixture("internships.md").replace("<!-- TABLE_QUANT_END -->", "", 1)

    with pytest.raises(UpstreamFormatError, match="Quant marker pair"):
        parse_source(broken, SOURCES[0])


def test_missing_required_header_column_fails_loudly() -> None:
    broken = read_fixture("internships.md").replace("| Company | Position | Location | Salary | Posting | Age |", "| Company | Position | Salary | Posting | Age |", 1)

    with pytest.raises(UpstreamFormatError, match="compatible job table"):
        parse_source(broken, SOURCES[0])


def test_category_with_only_malformed_rows_fails_before_history_can_be_inactivated() -> None:
    broken = read_fixture("internships.md").replace('alt="Apply"', 'alt="Not apply"', 3)

    with pytest.raises(UpstreamFormatError, match="FAANG\\+ table contained 3 rows but none could be parsed"):
        parse_source(broken, SOURCES[0])

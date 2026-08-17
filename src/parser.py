"""Parser for SpeedyApply's marker-delimited generated Markdown tables."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable, Mapping

from .config import CATEGORY_MARKERS, SourceConfig
from .models import Job

LOGGER = logging.getLogger(__name__)


class UpstreamFormatError(RuntimeError):
    """Raised when the expected public Markdown contract has changed."""


@dataclass(frozen=True)
class CategoryParseStats:
    """Diagnostics used to reject a silently broken category import."""

    candidate_rows: int
    parsed_rows: int
    skipped_rows: int


@dataclass(frozen=True)
class ParsedSource:
    """Parsed jobs plus per-category health data for one upstream file."""

    jobs: tuple[Job, ...]
    category_stats: Mapping[str, CategoryParseStats]


class _TextExtractor(HTMLParser):
    """Collect visible text from a small HTML fragment without dependencies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class _ApplyLinkExtractor(HTMLParser):
    """Find an anchor which contains an Apply image or visible Apply text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._anchors: list[dict[str, object]] = []
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value for key, value in attrs}
        if tag.casefold() == "a":
            href = attributes.get("href")
            self._anchors.append({"href": href, "is_apply": False})
        elif tag.casefold() == "img" and self._anchors:
            alt = attributes.get("alt") or ""
            if alt.strip().casefold() == "apply":
                self._anchors[-1]["is_apply"] = True

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() == "a":
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._anchors and data.strip().casefold() == "apply":
            self._anchors[-1]["is_apply"] = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "a" or not self._anchors:
            return
        anchor = self._anchors.pop()
        href = anchor["href"]
        if anchor["is_apply"] and isinstance(href, str) and href.strip():
            self.urls.append(html.unescape(href.strip()))


def split_markdown_row(line: str) -> list[str]:
    """Split a Markdown table row while respecting escapes and HTML attributes.

    Generated SpeedyApply cells are normally simple, but this intentionally
    avoids blindly splitting pipes inside escaped text or quoted HTML values.
    """

    value = line.strip()
    if not value.startswith("|"):
        raise ValueError("Markdown table row does not start with a pipe")

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    quote: str | None = None
    in_tag = False
    in_code = False

    for character in value[1:]:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            current.append(character)
            continue
        if in_tag and quote:
            if character == quote:
                quote = None
            current.append(character)
            continue
        if in_tag and character in {"\"", "'"}:
            quote = character
            current.append(character)
            continue
        if character == "<" and not in_tag:
            in_tag = True
            current.append(character)
            continue
        if character == ">" and in_tag:
            in_tag = False
            current.append(character)
            continue
        if character == "`" and not in_tag:
            in_code = not in_code
            current.append(character)
            continue
        if character == "|" and not in_tag and not in_code:
            cells.append("".join(current).strip())
            current = []
            continue
        current.append(character)

    trailing = "".join(current).strip()
    if trailing:
        cells.append(trailing)
    elif not value.endswith("|"):
        cells.append(trailing)
    return cells


def clean_cell(value: str) -> str:
    """Turn a Markdown/HTML table cell into readable plain text."""

    extractor = _TextExtractor()
    try:
        extractor.feed(value)
        extractor.close()
        text = "".join(extractor.parts) or value
    except Exception:  # HTMLParser is forgiving, but keep malformed rows usable.
        text = value
    text = html.unescape(text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`]", "", text)
    return " ".join(text.split())


def extract_application_url(posting_cell: str) -> str | None:
    """Extract the apply-link href, never an unrelated first anchor href."""

    extractor = _ApplyLinkExtractor()
    try:
        extractor.feed(posting_cell)
        extractor.close()
    except Exception:
        LOGGER.debug("Could not parse Posting cell HTML: %r", posting_cell)
    if extractor.urls:
        return extractor.urls[0]

    markdown_match = re.search(
        r"\[\s*apply(?:\s+now)?\s*\]\(\s*<?([^\s>)]+)>?(?:\s+[^)]*)?\)",
        posting_cell,
        flags=re.IGNORECASE,
    )
    if markdown_match:
        return html.unescape(markdown_match.group(1))
    return None


def _normalise_header(value: str) -> str:
    return " ".join(clean_cell(value).casefold().split())


def _is_separator_row(cells: Iterable[str], expected_count: int) -> bool:
    values = list(cells)
    if len(values) != expected_count:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in values)


def _column_index(headers: list[str], names: tuple[str, ...]) -> int | None:
    for name in names:
        if name in headers:
            return headers.index(name)
    return None


def _find_table(lines: list[str], category: str) -> tuple[list[str], list[str], int]:
    """Find a compatible header and the following table-row lines."""

    for index, line in enumerate(lines[:-1]):
        if not line.lstrip().startswith("|"):
            continue
        headers = [_normalise_header(cell) for cell in split_markdown_row(line)]
        required = {"company", "position", "location"}
        has_posting = any(name in headers for name in ("posting", "application", "apply"))
        if not required.issubset(headers) or not has_posting:
            continue
        separator = split_markdown_row(lines[index + 1]) if lines[index + 1].lstrip().startswith("|") else []
        if not _is_separator_row(separator, len(headers)):
            raise UpstreamFormatError(
                f"{category} table has a header but no valid Markdown separator row"
            )
        row_lines: list[str] = []
        for row_line in lines[index + 2 :]:
            if not row_line.strip():
                continue
            if not row_line.lstrip().startswith("|"):
                raise UpstreamFormatError(
                    f"{category} table has unexpected non-table content after its header"
                )
            row_lines.append(row_line)
        return headers, row_lines, index
    raise UpstreamFormatError(f"{category} section does not contain a compatible job table")


def _section_lines(markdown: str, category: str) -> list[str]:
    start_marker, end_marker = CATEGORY_MARKERS[category]
    start_count = markdown.count(start_marker)
    end_count = markdown.count(end_marker)
    if start_count != 1 or end_count != 1:
        raise UpstreamFormatError(
            f"Expected exactly one {category} marker pair; found {start_count} start and {end_count} end"
        )
    start = markdown.find(start_marker) + len(start_marker)
    end = markdown.find(end_marker, start)
    if end == -1:
        raise UpstreamFormatError(f"{category} end marker occurs before its start marker")
    return markdown[start:end].splitlines()


def _parse_category(
    markdown: str, source: SourceConfig, category: str
) -> tuple[list[Job], CategoryParseStats]:
    lines = _section_lines(markdown, category)
    headers, row_lines, header_line = _find_table(lines, category)

    company_index = _column_index(headers, ("company",))
    position_index = _column_index(headers, ("position", "role", "job title"))
    location_index = _column_index(headers, ("location",))
    posting_index = _column_index(headers, ("posting", "application", "apply"))
    salary_index = _column_index(headers, ("salary", "compensation", "pay"))
    age_index = _column_index(headers, ("age", "posted", "posting age"))

    required_indices = (company_index, position_index, location_index, posting_index)
    if any(index is None for index in required_indices):
        raise UpstreamFormatError(f"{category} table is missing one or more required columns")

    jobs: list[Job] = []
    skipped_rows = 0
    for offset, row_line in enumerate(row_lines, start=header_line + 3):
        try:
            cells = split_markdown_row(row_line)
        except ValueError as error:
            LOGGER.warning("Skipping malformed %s row at section line %s: %s", category, offset, error)
            skipped_rows += 1
            continue
        if len(cells) != len(headers):
            LOGGER.warning(
                "Skipping malformed %s row at section line %s: expected %s cells, got %s",
                category,
                offset,
                len(headers),
                len(cells),
            )
            skipped_rows += 1
            continue

        company = clean_cell(cells[company_index])
        position = clean_cell(cells[position_index])
        location = clean_cell(cells[location_index])
        application_url = extract_application_url(cells[posting_index])
        salary = clean_cell(cells[salary_index]) if salary_index is not None else None
        age = clean_cell(cells[age_index]) if age_index is not None else None
        salary = salary or None
        age = age or None

        if not all((company, position, location, application_url)):
            LOGGER.warning(
                "Skipping malformed %s %s row at section line %s: missing company, position, location, or apply URL",
                source.job_type,
                category,
                offset,
            )
            skipped_rows += 1
            continue

        jobs.append(
            Job(
                company=company,
                position=position,
                location=location,
                salary=salary,
                application_url=application_url,
                age=age,
                category=category,
                job_type=source.job_type,
                source_file=source.source_file,
                source_id=source.id,
                source_label=source.label,
                # The direct apply URL is also the only per-row provenance
                # SpeedyApply exposes in its generated table.
                source_url=application_url,
            )
        )
    return jobs, CategoryParseStats(
        candidate_rows=len(row_lines), parsed_rows=len(jobs), skipped_rows=skipped_rows
    )


def parse_source_with_diagnostics(markdown: str, source: SourceConfig) -> ParsedSource:
    """Parse a source and reject a category whose rows are mostly malformed."""

    jobs: list[Job] = []
    category_stats: dict[str, CategoryParseStats] = {}
    for category in CATEGORY_MARKERS:
        parsed, stats = _parse_category(markdown, source, category)
        category_stats[category] = stats
        if stats.candidate_rows and not stats.parsed_rows:
            raise UpstreamFormatError(
                f"{source.source_file} {category} table contained {stats.candidate_rows} rows but none could be parsed"
            )
        if stats.skipped_rows >= 5 and stats.skipped_rows > stats.parsed_rows:
            raise UpstreamFormatError(
                f"{source.source_file} {category} table skipped {stats.skipped_rows} malformed rows "
                f"while parsing only {stats.parsed_rows}; refusing to inactivate its history"
            )
        LOGGER.info("Parsed %s %s rows: %s", source.job_type, category, len(parsed))
        jobs.extend(parsed)
    return ParsedSource(jobs=tuple(jobs), category_stats=category_stats)


def parse_source(markdown: str, source: SourceConfig) -> list[Job]:
    """Parse all required categories from one USA Markdown source.

    Missing marker pairs or required table columns are hard errors. Individual
    malformed rows are logged and skipped so one bad row cannot hide the rest.
    """

    return list(parse_source_with_diagnostics(markdown, source).jobs)


def parse_configured_source_with_diagnostics(document: str, source: SourceConfig) -> ParsedSource:
    """Dispatch a configured document to its intentionally small source adapter."""

    if source.parser_id == "speedyapply":
        return parse_source_with_diagnostics(document, source)
    # Imported lazily so the adapter can reuse the public diagnostics types
    # above without creating a module import cycle.
    from .adapters import parse_applyguy_source, parse_simplify_source

    if source.parser_id in {"applyguy_internships", "applyguy_new_grad"}:
        return parse_applyguy_source(document, source)
    if source.parser_id == "simplify":
        return parse_simplify_source(document, source)
    raise UpstreamFormatError(f"No parser is configured for source {source.id}")

"""Source-specific adapters for ApplyGuy JSON and Simplify's HTML table."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import urlsplit

from .config import SourceConfig
from .models import Job
from .parser import CategoryParseStats, ParsedSource, UpstreamFormatError


LOGGER = logging.getLogger(__name__)


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _applyguy_metadata(record: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, str]:
    return {name: item for name in names if (item := _string(record.get(name))) is not None}


def _looks_like_swe_title(title: str) -> bool:
    """Conservative fallback for ApplyGuy's category-less new-grad feed."""

    normalized = title.casefold()
    return any(
        token in normalized
        for token in (
            "software engineer",
            "software development",
            "software developer",
            "swe",
            "full stack",
            "full-stack",
            "frontend",
            "front end",
            "backend",
            "back end",
            "mobile engineer",
            "android engineer",
            "ios engineer",
            "site reliability",
            "sre",
        )
    )


def parse_applyguy_source(document: str, source: SourceConfig) -> ParsedSource:
    """Parse one verified ApplyGuy JSON feed into Software Engineering jobs."""

    try:
        payload = json.loads(document)
    except json.JSONDecodeError as error:
        raise UpstreamFormatError(f"{source.id} is not valid JSON: {error}") from error
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
        raise UpstreamFormatError(f"{source.id} JSON must contain a jobs array")
    jobs: list[Job] = []
    skipped = 0
    updated_at = _string(payload.get("updatedAt"))
    for index, raw_record in enumerate(payload["jobs"], start=1):
        if not isinstance(raw_record, Mapping):
            skipped += 1
            LOGGER.warning("Skipping malformed %s row %s: not an object", source.id, index)
            continue
        company = _string(raw_record.get("company"))
        title = _string(raw_record.get("title"))
        location = _string(raw_record.get("location"))
        source_job_id = _string(raw_record.get("id"))
        listing_url = _string(raw_record.get("listingUrl"))
        source_url = _string(raw_record.get("url"))
        application_url = listing_url or source_url

        if source.parser_id == "applyguy_internships":
            category = _string(raw_record.get("category"))
            if not category or category.casefold() != "software engineering":
                continue
            season = _string(raw_record.get("season"))
            metadata = _applyguy_metadata(raw_record, ("id", "url", "listingUrl", "category", "season", "posted", "age"))
        elif source.parser_id == "applyguy_new_grad":
            eligibility = _string(raw_record.get("eligibility"))
            match_kind = _string(raw_record.get("matchKind"))
            # This dedicated feed currently classifies both of these as
            # early-career roles. Do not include unexpected senior labels.
            if eligibility not in {"New Grad", "Entry Level"}:
                continue
            if not title or not _looks_like_swe_title(title):
                # This feed is early-career focused, not exclusively SWE. Its
                # lack of a structured category makes a documented title
                # fallback necessary to keep PM/non-engineering rows out.
                continue
            category = "Unknown"
            season = None
            metadata = _applyguy_metadata(
                raw_record,
                ("id", "url", "listingUrl", "eligibility", "matchKind", "posted", "age"),
            )
            if eligibility:
                metadata["eligibility"] = eligibility
            if match_kind:
                metadata["matchKind"] = match_kind
        else:  # pragma: no cover - guarded by config and dispatcher
            raise UpstreamFormatError(f"Unsupported ApplyGuy adapter id: {source.parser_id}")

        if updated_at:
            metadata["feedUpdatedAt"] = updated_at
        if not all((company, title, location, application_url)):
            skipped += 1
            LOGGER.warning(
                "Skipping malformed %s row %s: missing company, title, location, or application URL",
                source.id,
                index,
            )
            continue
        jobs.append(
            Job(
                company=company,
                position=title,
                location=location,
                salary=None,
                application_url=application_url,
                age=_string(raw_record.get("age")),
                category=category,
                job_type=source.job_type,
                source_file=source.source_file,
                source_id=source.id,
                source_label=source.label,
                source_job_id=source_job_id,
                source_url=source_url,
                posted=_string(raw_record.get("posted")),
                season=season,
                source_metadata=metadata,
            )
        )
    if not jobs:
        raise UpstreamFormatError(f"{source.id} contained no valid Software Engineering rows")
    return ParsedSource(
        jobs=tuple(jobs),
        category_stats={
            "Software Engineering": CategoryParseStats(
                candidate_rows=len(payload["jobs"]), parsed_rows=len(jobs), skipped_rows=skipped
            )
        },
    )


@dataclass
class _Cell:
    text: list[str] = field(default_factory=list)
    links: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)

    def visible_text(self) -> str:
        return " ".join("".join(self.text).replace("\n", " | ").split())


class _SimplifyTableParser(HTMLParser):
    """Small forgiving parser for the one active Simplify HTML table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self._in_table = False
        self._current_row: list[_Cell] | None = None
        self._current_cell: _Cell | None = None
        self._cell_depth = 0
        self._anchor_stack: list[dict[str, Any]] = []
        self._summary_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): value or "" for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "table":
            self._in_table = True
        elif not self._in_table:
            return
        elif lowered == "tr":
            self._current_row = []
        elif lowered in {"td", "th"} and self._current_row is not None:
            self._current_cell = _Cell()
            self._cell_depth = 1
        elif lowered == "br" and self._current_cell is not None:
            self._current_cell.text.append("\n")
        elif lowered == "summary" and self._current_cell is not None:
            # <details> summaries contain a presentation count such as
            # "5 locations"; the expanded entries are the actual locations.
            self._summary_depth += 1
        elif lowered == "a" and self._current_cell is not None:
            self._anchor_stack.append({"href": attributes.get("href", ""), "text": [], "alts": []})
        elif lowered == "img" and self._anchor_stack:
            self._anchor_stack[-1]["alts"].append(attributes.get("alt", ""))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() == "a":
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None and not self._summary_depth:
            self._current_cell.text.append(data)
        if self._anchor_stack:
            self._anchor_stack[-1]["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "a" and self._anchor_stack:
            anchor = self._anchor_stack.pop()
            if self._current_cell is not None and anchor["href"]:
                self._current_cell.links.append(
                    (anchor["href"], "".join(anchor["text"]), tuple(anchor["alts"]))
                )
        elif lowered in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            self._current_row.append(self._current_cell)
            self._current_cell = None
            self._cell_depth = 0
        elif lowered == "tr" and self._current_row is not None:
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = None
        elif lowered == "table":
            self._in_table = False
        elif lowered == "summary" and self._summary_depth:
            self._summary_depth -= 1


def _software_section(markdown: str) -> str:
    match = re.search(
        r"^##\s+.*Software Engineering Internship Roles\s*$([\s\S]*?)(?=^##\s+|\Z)",
        markdown,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        raise UpstreamFormatError("Simplify README has no active Software Engineering Internship Roles section")
    return match.group(1)


def _clean_company(value: str) -> tuple[str, bool]:
    continuation = value.strip() in {"↳", "â†³"}
    # Strip only presentation flags listed in Simplify's active legend.
    cleaned = re.sub(r"[🔥🛂🇺🇸🎓🔒]", "", value)
    return " ".join(html.unescape(cleaned).split()), continuation


def _application_links(cell: _Cell) -> tuple[str | None, str | None]:
    """Choose the employer Apply anchor, retaining Simplify provenance."""

    links = [(html.unescape(href.strip()), text, alts) for href, text, alts in cell.links if href.strip()]
    source_url = next(
        (href for href, _text, _alts in links if (urlsplit(href).hostname or "").casefold().endswith("simplify.jobs")),
        None,
    )
    direct = next(
        (
            href
            for href, text, alts in links
            if "apply" in {alt.strip().casefold() for alt in alts}
            and not (urlsplit(href).hostname or "").casefold().endswith("simplify.jobs")
        ),
        None,
    )
    if direct is None:
        direct = next(
            (
                href
                for href, text, _alts in links
                if text.strip().casefold() == "apply"
                and not (urlsplit(href).hostname or "").casefold().endswith("simplify.jobs")
            ),
            None,
        )
    if direct is None:
        direct = next(
            (href for href, _text, _alts in links if not (urlsplit(href).hostname or "").casefold().endswith("simplify.jobs")),
            None,
        )
    return direct or source_url, source_url


def _simplify_source_job_id(source_url: str | None) -> str | None:
    if not source_url:
        return None
    parts = [part for part in urlsplit(source_url).path.split("/") if part]
    return parts[-1] if len(parts) >= 2 and parts[-2] == "p" else None


def parse_simplify_source(document: str, source: SourceConfig) -> ParsedSource:
    """Parse active Simplify SWE rows; archived and other sections are ignored."""

    parser = _SimplifyTableParser()
    try:
        parser.feed(_software_section(document))
        parser.close()
    except UpstreamFormatError:
        raise
    except Exception as error:
        raise UpstreamFormatError(f"Could not parse Simplify software table: {error}") from error
    if not parser.rows:
        raise UpstreamFormatError("Simplify Software Engineering table is empty")
    headers = [cell.visible_text().casefold() for cell in parser.rows[0]]
    required = ("company", "role", "location", "application", "age")
    if len(headers) != 5 or any(name not in headers for name in required):
        raise UpstreamFormatError("Simplify Software Engineering table has an unexpected header")
    indexes = {name: headers.index(name) for name in required}
    jobs: list[Job] = []
    skipped = 0
    last_company: str | None = None
    for row_number, row in enumerate(parser.rows[1:], start=2):
        if len(row) != len(headers):
            skipped += 1
            LOGGER.warning("Skipping malformed Simplify row %s: expected %s cells", row_number, len(headers))
            continue
        company, continuation = _clean_company(row[indexes["company"]].visible_text())
        if continuation:
            company = last_company or ""
        elif company:
            last_company = company
        role = row[indexes["role"]].visible_text()
        location = row[indexes["location"]].visible_text()
        application_url, source_url = _application_links(row[indexes["application"]])
        age = row[indexes["age"]].visible_text() or None
        if not all((company, role, location, application_url)):
            skipped += 1
            LOGGER.warning("Skipping malformed Simplify row %s: missing required job fields", row_number)
            continue
        original_company = row[indexes["company"]].visible_text()
        category = "FAANG+" if "🔥" in original_company else "Unknown"
        jobs.append(
            Job(
                company=company,
                position=role,
                location=location,
                salary=None,
                application_url=application_url,
                age=age,
                category=category,
                job_type=source.job_type,
                source_file=source.source_file,
                source_id=source.id,
                source_label=source.label,
                source_job_id=_simplify_source_job_id(source_url),
                source_url=source_url,
                season="Summer 2027",
                source_metadata={"simplify_url": source_url} if source_url else {},
            )
        )
    if not jobs:
        raise UpstreamFormatError("Simplify Software Engineering table contained no valid rows")
    return ParsedSource(
        jobs=tuple(jobs),
        category_stats={
            "Software Engineering": CategoryParseStats(
                candidate_rows=len(parser.rows) - 1, parsed_rows=len(jobs), skipped_rows=skipped
            )
        },
    )

"""Deterministic human-readable Markdown dashboard generation."""

from __future__ import annotations

from typing import Any, Iterable

from .config import TARGET_LOCATION_DESCRIPTION, TARGET_LOCATION_LABEL


GENERATED_WARNING = "<!-- This file is generated automatically. Do not edit manually. -->"


def _markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _record_tie_breaker(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("company") or "").casefold(),
        str(record.get("position") or "").casefold(),
        str(record.get("application_url") or ""),
    )


def _sort_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest first, with ascending deterministic company/title tie breakers."""

    ordered = sorted(records, key=_record_tie_breaker)
    return sorted(ordered, key=lambda record: str(record.get("first_seen") or ""), reverse=True)


def _render_rows(records: Iterable[dict[str, Any]]) -> list[str]:
    rows: list[str] = []
    for record in records:
        url = str(record["application_url"])
        application = f"[Apply](<{url}>)"
        values = (
            record["company"],
            record["position"],
            record["job_type"],
            record["category"],
            record["location"],
            record.get("salary") or "N/A",
            record["first_seen"],
            record["last_seen"],
            "Yes" if record.get("active") else "No",
            application,
        )
        rows.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
    return rows


def render_jobs_markdown(state: dict[str, Any]) -> str:
    """Render all history with active jobs first and deterministic ordering."""

    header = "| Company | Position | Type | Category | Location | Salary | First Seen | Last Seen | Active | Application |"
    divider = "|---|---|---|---|---|---|---|---|---|---|"
    lines = [
        GENERATED_WARNING,
        "",
        f"# {TARGET_LOCATION_LABEL} SWE Job Tracker",
        "",
        f"Tracks SpeedyApply USA internship and new-graduate postings whose displayed Location matches {TARGET_LOCATION_DESCRIPTION}.",
        "",
    ]

    if not state.get("initialized"):
        lines.extend(
            [
                "The tracker has not established its first baseline yet. The first successful scheduled or manual run records current matches without creating an alert.",
                "",
            ]
        )
        return "\n".join(lines)

    records = list(state.get("jobs", {}).values())
    active = _sort_records(record for record in records if record.get("active") is True)
    inactive = _sort_records(record for record in records if record.get("active") is not True)

    lines.extend([f"## Active Jobs ({len(active)})", "", header, divider])
    if active:
        lines.extend(_render_rows(active))
    else:
        lines.append("| _No active matching jobs_ |  |  |  |  |  |  |  |  |  |")

    lines.extend(["", f"## Historical / Closed or Removed Jobs ({len(inactive)})", "", header, divider])
    if inactive:
        lines.extend(_render_rows(inactive))
    else:
        lines.append("| _No historical matching jobs_ |  |  |  |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines)

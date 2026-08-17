"""Readable, answer-free Markdown rendering for one scan result."""

from __future__ import annotations

from urllib.parse import quote

from .models import ApplicationQuestion, ApplicationScanResult, ScanStatus


_ORDER = (
    "job_specific",
    "work_authorization",
    "education",
    "experience",
    "location_logistics",
    "other",
)
_HEADINGS = {
    "job_specific": "Custom screening questions",
    "work_authorization": "Work authorization",
    "education": "Education",
    "experience": "Experience",
    "location_logistics": "Location and logistics",
    "other": "Other visible questions",
}
_COLLAPSED = ("profile", "resume_documents", "compliance_demographic")


def _field_details(question: ApplicationQuestion) -> str:
    required = "Required" if question.required is True else "Optional" if question.required is False else "Requirement not stated"
    details = [required, question.field_type.replace("_", " ")]
    if question.options:
        details.append("Options: " + " / ".join(_escape_text(option) for option in question.options))
    return " — ".join(details)


def _escape_text(value: str) -> str:
    """Keep public form labels from changing the Issue's Markdown structure."""

    return (
        value.replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", " ")
    )


def _questions_block(questions: tuple[ApplicationQuestion, ...]) -> list[str]:
    lines: list[str] = []
    for question in questions:
        lines.append(f"{question.ordinal}. **{_escape_text(question.label)}**  ")
        lines.append(f"   {_field_details(question)}")
    return lines


def render_application_scan_block(result: ApplicationScanResult) -> str:
    """Render only the visible scan section; callers own idempotency markers."""

    provider = result.provider.value.title()
    status = result.status.value.title()
    lines = [
        "## Application Questions",
        "",
        f"**Scan status:** {status}  ",
        f"**Provider:** {provider}  ",
        f"**Questions detected:** {len(result.questions)}  ",
        f"**Custom / screening questions:** {len(result.custom_questions)}",
        "",
        f"[Open application](<{quote(result.application_url, safe=':/?&=#%+-._~')}>)",
    ]
    if result.completeness_reason:
        lines.extend(["", f"> {result.completeness_reason}"])

    by_category: dict[str, list[ApplicationQuestion]] = {}
    for question in result.questions:
        by_category.setdefault(question.category, []).append(question)
    custom = tuple(question for question in result.questions if question.is_custom)
    if custom:
        lines.extend(["", "### Custom screening questions", ""])
        lines.extend(_questions_block(custom))
        for category in tuple(by_category):
            by_category[category] = [question for question in by_category[category] if not question.is_custom]
    for category in _ORDER:
        questions = tuple(by_category.pop(category, ()))
        if not questions:
            continue
        lines.extend(["", f"### {_HEADINGS[category]}", ""])
        lines.extend(_questions_block(questions))

    profile = tuple(by_category.pop("profile", ())) + tuple(by_category.pop("resume_documents", ()))
    if profile:
        lines.extend(["", "<details>", "<summary>Standard application fields</summary>", ""])
        lines.extend(_questions_block(profile))
        lines.extend(["", "</details>"])
    demographics = tuple(by_category.pop("compliance_demographic", ()))
    if demographics:
        lines.extend(["", "<details>", "<summary>Optional compliance / demographic fields</summary>", ""])
        lines.extend(_questions_block(demographics))
        lines.extend(["", "</details>"])
    # Future categories should still be visible rather than silently dropped.
    for category, questions in sorted(by_category.items()):
        if not questions:
            continue
        lines.extend(["", f"### {category.replace('_', ' ').title()}", ""])
        lines.extend(_questions_block(tuple(questions)))

    lines.extend(
        [
            "",
            "> Application scanning is read-only. No application was submitted, no answers were entered, and no files were uploaded.",
        ]
    )
    if result.status is ScanStatus.PARTIAL:
        lines.append(
            "> This scan is partial: conditional, multi-step, or authentication-gated fields may not be visible."
        )
    return "\n".join(lines).rstrip() + "\n"

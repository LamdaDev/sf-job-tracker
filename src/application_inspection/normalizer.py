"""Provider-neutral field type, label, and category normalization."""

from __future__ import annotations

import html
import re
from typing import Iterable

from .models import ApplicationQuestion


_SPACE = re.compile(r"\s+")
_TRAILING_REQUIRED = re.compile(r"(?:\s|\*)+(?:required)?\s*\*+\s*$", re.IGNORECASE)

_TYPE_ALIASES = {
    "address": "address",
    "boolean": "boolean",
    "checkbox": "checkbox",
    "checkboxes": "multi_select",
    "date": "date",
    "datetime": "date",
    "email": "email",
    "file": "file",
    "input_file": "file",
    "input_text": "text",
    "long_text": "long_text",
    "multi_select": "multi_select",
    # Greenhouse's public Job Board API uses these provider-specific names
    # for ordinary select controls.  They still represent visible choices,
    # not a new question type.
    "multi_value_single_select": "single_select",
    "single_value_single_select": "single_select",
    "multi_value_multi_select": "multi_select",
    "single_value_multi_select": "multi_select",
    "multiselect": "multi_select",
    "number": "number",
    "phone": "phone",
    "radio": "radio",
    "select": "single_select",
    "single_select": "single_select",
    "tel": "phone",
    "text": "text",
    "textarea": "long_text",
    "url": "url",
}


def normalize_label(value: str) -> str:
    """Turn provider/DOM presentation text into a stable human label."""

    plain = html.unescape(re.sub(r"<[^>]*>", " ", value or ""))
    plain = _SPACE.sub(" ", plain).strip()
    plain = _TRAILING_REQUIRED.sub("", plain).strip()
    return plain.rstrip(":").strip()


def normalize_field_type(value: str | None, *, multiple: bool = False) -> str:
    """Map HTML and ATS-specific field types onto the public model vocabulary."""

    raw = (value or "unknown").casefold().strip().replace("-", "_").replace(" ", "_")
    normalized = _TYPE_ALIASES.get(raw, raw if raw in _TYPE_ALIASES.values() else "unknown")
    if multiple and normalized in {"single_select", "checkbox"}:
        return "multi_select"
    return normalized


def categorize_question(label: str, *, source_section: str | None = None) -> str:
    """Classify a question using deterministic, explainable text rules."""

    text = normalize_label(label).casefold()
    section = (source_section or "").casefold()
    combined = f"{section} {text}"
    if any(term in combined for term in ("demographic", "eeo", "gender", "race", "ethnicity", "veteran", "disability", "self-identif")):
        return "compliance_demographic"
    if any(
        term in text
        for term in (
            "sponsor",
            "work authorization",
            "authorized to work",
            "right to work",
            "employment eligibility",
            "citizen",
            "citizenship",
            "visa",
            "immigration",
        )
    ):
        return "work_authorization"
    # Greenhouse's compliance group can contain neutral labels such as a
    # disclosure/consent field.  Preserve them but keep them collapsed in an
    # Issue rather than presenting them like screening questions.
    if "compliance" in section:
        return "compliance_demographic"
    if any(term in text for term in ("resume", "curriculum vitae", "cv", "cover letter", "portfolio", "writing sample")):
        return "resume_documents"
    if any(
        term in text
        for term in ("university", "college", "school", "degree", "major", "minor", "graduat", "gpa", "education")
    ):
        return "education"
    if any(
        term in text
        for term in ("years of experience", "work experience", "previous internship", "programming language", "technical skill", "years' experience")
    ):
        return "experience"
    if any(
        term in text
        for term in ("relocat", "preferred office", "office location", "start date", "availability", "commute", "work location")
    ):
        return "location_logistics"
    if text == "name" or any(
        term in text
        for term in (
            "first name",
            "last name",
            "full name",
            "email",
            "phone",
            "linkedin",
            "github",
            "website",
            "address",
            "postal code",
            "zip code",
            "city",
            "state",
            "country",
        )
    ):
        return "profile"
    if any(
        term in text
        for term in (
            "why ",
            "tell us",
            "describe ",
            "project",
            "interested in",
            "motivat",
            "anything else",
            "additional information",
            "proud of",
        )
    ):
        return "job_specific"
    return "other"


def is_custom_question(label: str, category: str) -> bool:
    """Identify prompts worth surfacing before boilerplate application fields."""

    if category == "job_specific":
        return True
    # ``other`` deliberately remains conservative: only natural-language
    # prompts get highlighted, not arbitrary unlabeled DOM fields.
    text = normalize_label(label).casefold()
    return category == "other" and ("?" in text or len(text.split()) >= 5)


def normalize_options(values: Iterable[object] | None) -> tuple[str, ...]:
    """Normalize and de-duplicate public answer choices while preserving order."""

    output: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        if isinstance(value, dict):
            candidate = value.get("label") or value.get("name") or value.get("value") or ""
        else:
            candidate = value if isinstance(value, str) else ""
        option = normalize_label(candidate)
        if not option:
            continue
        key = option.casefold()
        if key not in seen:
            seen.add(key)
            output.append(option)
    return tuple(output)


def normalize_question(
    *,
    label: str,
    field_type: str | None,
    required: bool | None,
    options: Iterable[object] | None = None,
    source_section: str | None = None,
    ordinal: int = 0,
    field_name: str | None = None,
    multiple: bool = False,
) -> ApplicationQuestion | None:
    """Create one normalized question, dropping truly unlabeled controls."""

    clean_label = normalize_label(label)
    if not clean_label:
        return None
    clean_section = normalize_label(source_section or "") or None
    category = categorize_question(clean_label, source_section=clean_section)
    return ApplicationQuestion(
        label=clean_label,
        field_type=normalize_field_type(field_type, multiple=multiple),
        required=required,
        options=normalize_options(options),
        category=category,
        source_section=clean_section,
        ordinal=ordinal,
        is_custom=is_custom_question(clean_label, category),
        field_name=field_name.strip() if isinstance(field_name, str) and field_name.strip() else None,
    )

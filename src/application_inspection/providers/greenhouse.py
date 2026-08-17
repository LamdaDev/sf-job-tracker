"""Structured, read-only extraction through Greenhouse's public Job Board API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

from ..models import ApplicationProvider, ApplicationQuestion, ApplicationScanResult, ScanStatus
from ..normalizer import normalize_question
from ..security import safe_error_message


class GreenhouseFetchError(RuntimeError):
    """A public Greenhouse endpoint was unavailable or returned invalid data."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class GreenhouseJobReference:
    """The board token and public job id required by Greenhouse's API."""

    board_token: str
    job_id: str

    @property
    def api_url(self) -> str:
        board = quote(self.board_token, safe="")
        job = quote(self.job_id, safe="")
        return f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}?questions=true"


def greenhouse_job_reference(application_url: str) -> GreenhouseJobReference | None:
    """Derive a public board token/job id from supported Greenhouse URLs."""

    try:
        parsed = urlsplit(application_url)
    except (TypeError, ValueError):
        return None
    hostname = (parsed.hostname or "").casefold()
    if hostname != "greenhouse.io" and not hostname.endswith(".greenhouse.io"):
        return None
    query = parse_qs(parsed.query)
    # The documented embedded form uses ``for`` and ``token`` rather than a
    # board/jobs path.  It is still a public Greenhouse application URL.
    if parsed.path.rstrip("/").endswith("embed/job_app"):
        board = (query.get("for") or [""])[0]
        job_id = (query.get("token") or [""])[0]
        if board and job_id:
            return GreenhouseJobReference(board, job_id)
    parts = [part for part in parsed.path.split("/") if part]
    if "jobs" not in parts:
        return None
    index = parts.index("jobs")
    if index < 1 or index + 1 >= len(parts):
        return None
    board, job_id = parts[index - 1], parts[index + 1]
    if not board or not job_id:
        return None
    return GreenhouseJobReference(board, job_id)


def fetch_greenhouse_job(
    reference: GreenhouseJobReference, *, timeout_seconds: int = 25
) -> tuple[Mapping[str, Any], int]:
    """Fetch the public structured Greenhouse form schema with no credentials."""

    request = Request(
        reference.api_url,
        headers={
            "Accept": "application/json",
            "User-Agent": "sf-job-tracker-application-inspector/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - endpoint is fixed Greenhouse API
            status = response.getcode()
            body = response.read().decode("utf-8")
    except HTTPError as error:
        raise GreenhouseFetchError(f"Greenhouse API returned HTTP {error.code}", status=error.code) from error
    except (URLError, TimeoutError, OSError, UnicodeError) as error:
        raise GreenhouseFetchError(f"Could not reach Greenhouse API: {type(error).__name__}") from error
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise GreenhouseFetchError("Greenhouse API returned malformed JSON", status=status) from error
    if not isinstance(payload, Mapping):
        raise GreenhouseFetchError("Greenhouse API response was not an object", status=status)
    return payload, status


def _bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.casefold()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _field_options(field: Mapping[str, Any]) -> Iterable[object]:
    values = field.get("values", field.get("options", ()))
    return values if isinstance(values, list) else ()


def _question_from_mapping(
    question: Mapping[str, Any], *, section: str, ordinal: int
) -> ApplicationQuestion | None:
    label = question.get("label") or question.get("question")
    if not isinstance(label, str):
        return None
    fields = question.get("fields")
    field_maps = [field for field in fields if isinstance(field, Mapping)] if isinstance(fields, list) else []
    # The API can expose geocoding/internal inputs (for example Latitude and
    # Longitude) alongside visible form definitions.  Generic HTML parsing
    # already ignores hidden controls; keep the structured path consistent.
    visible_field_maps = [
        field
        for field in field_maps
        if str(field.get("type") or "").casefold() not in {"hidden", "input_hidden"}
    ]
    if field_maps and not visible_field_maps:
        return None
    first_field = visible_field_maps[0] if visible_field_maps else question
    field_type = first_field.get("type") or question.get("type")
    if not isinstance(field_type, str):
        field_type = "unknown"
    options: list[object] = []
    for field in visible_field_maps or [question]:
        options.extend(_field_options(field))
    field_name = first_field.get("name") or question.get("name")
    return normalize_question(
        label=label,
        field_type=field_type,
        required=_bool(question.get("required")),
        options=options,
        source_section=section,
        ordinal=ordinal,
        field_name=field_name if isinstance(field_name, str) else None,
        multiple=len(visible_field_maps) > 1 and str(field_type).casefold() in {"checkbox", "select"},
    )


def _walk_group(value: object, *, section: str) -> Iterable[Mapping[str, Any]]:
    """Find question-shaped records in documented and legacy API group shapes."""

    if isinstance(value, Mapping):
        label = value.get("label") or value.get("question")
        if isinstance(label, str) and ("fields" in value or "required" in value or "type" in value):
            yield value
            return
        for nested in value.values():
            yield from _walk_group(nested, section=section)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_group(nested, section=section)


_GROUPS = (
    "questions",
    "location_questions",
    "compliance",
    "compliance_questions",
    "demographic_questions",
    "demographics",
)


def parse_greenhouse_payload(payload: Mapping[str, Any]) -> tuple[ApplicationQuestion, ...]:
    """Normalize all public Greenhouse question groups in stable page order."""

    if not isinstance(payload, Mapping):
        raise ValueError("Greenhouse payload must be an object")
    questions: list[ApplicationQuestion] = []
    ordinal = 1
    for section in _GROUPS:
        if section not in payload:
            continue
        group = payload[section]
        # Greenhouse legitimately emits optional form groups such as
        # ``demographic_questions: null`` when that section is not configured.
        # A non-null scalar remains malformed and must not masquerade as an
        # empty complete schema.
        if group is None:
            continue
        if not isinstance(group, (list, Mapping)):
            raise ValueError(f"Greenhouse {section} group has an invalid shape")
        for raw_question in _walk_group(group, section=section):
            question = _question_from_mapping(raw_question, section=section, ordinal=ordinal)
            if question is not None:
                questions.append(question)
                ordinal += 1
    # A schema can repeat a question through a compliance wrapper. Preserve the
    # first semantic definition, which is closest to the visible form order.
    output: list[ApplicationQuestion] = []
    seen: set[tuple[str, str, str | None]] = set()
    for question in questions:
        identity = (question.label.casefold(), question.field_type, question.source_section)
        if identity not in seen:
            seen.add(identity)
            output.append(question)
    return tuple(output)


GreenhouseFetcher = Callable[[GreenhouseJobReference], tuple[Mapping[str, Any], int]]


def _call_fetcher(
    fetcher: Callable[..., tuple[Mapping[str, Any], int]], reference: GreenhouseJobReference, timeout_seconds: int
) -> tuple[Mapping[str, Any], int]:
    try:
        return fetcher(reference, timeout_seconds=timeout_seconds)
    except TypeError as error:
        # Test seams and callers can use the small one-argument callable.
        # Only retry that shape when the callable rejected the timeout keyword.
        if "timeout_seconds" not in str(error):
            raise
        return fetcher(reference)


def inspect_greenhouse_application(
    *,
    canonical_job_id: str,
    application_url: str,
    fetcher: Callable[..., tuple[Mapping[str, Any], int]] = fetch_greenhouse_job,
    timeout_seconds: int = 25,
    scanned_at: str | None = None,
) -> ApplicationScanResult:
    """Inspect a Greenhouse job through its documented public API.

    ``complete`` is only claimed when the structured API returned at least one
    recognizable form definition.  A zero-question response remains
    conservative rather than being treated as a successful empty application.
    """

    reference = greenhouse_job_reference(application_url)
    if reference is None:
        return ApplicationScanResult(
            canonical_job_id=canonical_job_id,
            provider=ApplicationProvider.GREENHOUSE,
            application_url=application_url,
            status=ScanStatus.UNAVAILABLE,
            completeness_reason="Could not derive a public Greenhouse board and job id from the application URL.",
            scanned_at=scanned_at,
            metadata={"inspection_method": "greenhouse_api"},
        )
    try:
        payload, http_status = _call_fetcher(fetcher, reference, timeout_seconds)
        questions = parse_greenhouse_payload(payload)
    except GreenhouseFetchError as error:
        unavailable = error.status in {401, 403, 404, 410}
        return ApplicationScanResult(
            canonical_job_id=canonical_job_id,
            provider=ApplicationProvider.GREENHOUSE,
            application_url=application_url,
            status=ScanStatus.UNAVAILABLE if unavailable else ScanStatus.FAILED,
            completeness_reason=(
                "Application appears to be closed or unavailable."
                if error.status in {404, 410}
                else "Public Greenhouse application data is not accessible."
                if unavailable
                else "Greenhouse form inspection failed."
            ),
            scanned_at=scanned_at,
            http_status=error.status,
            error_type=type(error).__name__,
            error_message=safe_error_message(error, stage="fetching the public Greenhouse schema"),
            metadata={"inspection_method": "greenhouse_api"},
        )
    except Exception as error:  # third-party test seams should not escape the inspector
        return ApplicationScanResult(
            canonical_job_id=canonical_job_id,
            provider=ApplicationProvider.GREENHOUSE,
            application_url=application_url,
            status=ScanStatus.FAILED,
            completeness_reason="Greenhouse form inspection failed.",
            scanned_at=scanned_at,
            error_type=type(error).__name__,
            error_message=safe_error_message(error, stage="parsing the public Greenhouse schema"),
            metadata={"inspection_method": "greenhouse_api"},
        )
    if not questions:
        return ApplicationScanResult(
            canonical_job_id=canonical_job_id,
            provider=ApplicationProvider.GREENHOUSE,
            application_url=application_url,
            status=ScanStatus.PARTIAL,
            completeness_reason="Greenhouse returned no recognizable public form fields; completeness cannot be confirmed.",
            scanned_at=scanned_at,
            http_status=http_status,
            metadata={"inspection_method": "greenhouse_api"},
        )
    return ApplicationScanResult(
        canonical_job_id=canonical_job_id,
        provider=ApplicationProvider.GREENHOUSE,
        application_url=application_url,
        status=ScanStatus.COMPLETE,
        questions=questions,
        completeness_reason="Greenhouse public structured application schema was retrieved.",
        scanned_at=scanned_at,
        http_status=http_status,
        metadata={"inspection_method": "greenhouse_api"},
    )

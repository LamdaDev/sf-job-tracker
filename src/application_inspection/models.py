"""Durable, answer-free data models for application-form inspection."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .security import is_sensitive_key


class ScanStatus(str, Enum):
    """How confidently a public application form was inspected."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class ApplicationProvider(str, Enum):
    """Known public application platforms plus the conservative fallback."""

    GREENHOUSE = "greenhouse"
    ASHBY = "ashby"
    LEVER = "lever"
    WORKDAY = "workday"
    GENERIC = "generic"


def _sorted_json_value(value: Any) -> Any:
    """Return only JSON-safe values with mapping keys in stable order."""

    if isinstance(value, Mapping):
        return {
            str(key): _sorted_json_value(value[key])
            for key in sorted(value, key=str)
            if not is_sensitive_key(key)
        }
    if isinstance(value, (tuple, list)):
        return [_sorted_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string or null")
    return value


@dataclass(frozen=True)
class ApplicationQuestion:
    """One public field or question definition; never an applicant answer."""

    label: str
    field_type: str
    required: bool | None = None
    options: tuple[str, ...] = ()
    category: str = "other"
    source_section: str | None = None
    ordinal: int = 0
    is_custom: bool = False
    field_name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("ApplicationQuestion label must be a non-empty string")
        if not isinstance(self.field_type, str) or not self.field_type:
            raise ValueError("ApplicationQuestion field_type must be a non-empty string")
        if self.required is not None and not isinstance(self.required, bool):
            raise ValueError("ApplicationQuestion required must be a boolean or null")
        if not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("ApplicationQuestion ordinal must be a non-negative integer")
        if not all(isinstance(option, str) and option.strip() for option in self.options):
            raise ValueError("ApplicationQuestion options must contain non-empty strings")

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-friendly question definition."""

        return {
            "category": self.category,
            "field_name": self.field_name,
            "field_type": self.field_type,
            "is_custom": self.is_custom,
            "label": self.label,
            "options": list(self.options),
            "ordinal": self.ordinal,
            "required": self.required,
            "source_section": self.source_section,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ApplicationQuestion":
        """Read a persisted question while ignoring future-compatible extras."""

        options = value.get("options", ())
        if not isinstance(options, (list, tuple)) or not all(isinstance(item, str) for item in options):
            raise ValueError("ApplicationQuestion options must be a list of strings")
        required = value.get("required")
        if required is not None and not isinstance(required, bool):
            raise ValueError("ApplicationQuestion required must be a boolean or null")
        label = value.get("label")
        field_type = value.get("field_type", "unknown")
        category = value.get("category", "other")
        if not isinstance(label, str) or not isinstance(field_type, str) or not isinstance(category, str):
            raise ValueError("ApplicationQuestion has invalid string fields")
        ordinal = value.get("ordinal", 0)
        if not isinstance(ordinal, int):
            raise ValueError("ApplicationQuestion ordinal must be an integer")
        return cls(
            label=label,
            field_type=field_type,
            required=required,
            options=tuple(options),
            category=category,
            source_section=_optional_string(value.get("source_section"), "source_section"),
            ordinal=ordinal,
            is_custom=bool(value.get("is_custom", False)),
            field_name=_optional_string(value.get("field_name"), "field_name"),
        )


@dataclass(frozen=True)
class ApplicationScanResult:
    """The result of one bounded, read-only application inspection."""

    canonical_job_id: str
    provider: ApplicationProvider
    application_url: str
    status: ScanStatus
    questions: tuple[ApplicationQuestion, ...] = ()
    completeness_reason: str | None = None
    scanned_at: str | None = None
    http_status: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_job_id, str) or not self.canonical_job_id:
            raise ValueError("canonical_job_id must be a non-empty string")
        if not isinstance(self.application_url, str) or not self.application_url:
            raise ValueError("application_url must be a non-empty string")
        if not isinstance(self.provider, ApplicationProvider):
            object.__setattr__(self, "provider", ApplicationProvider(str(self.provider)))
        if not isinstance(self.status, ScanStatus):
            object.__setattr__(self, "status", ScanStatus(str(self.status)))
        if not all(isinstance(question, ApplicationQuestion) for question in self.questions):
            raise ValueError("questions must contain ApplicationQuestion objects")
        if self.http_status is not None and (not isinstance(self.http_status, int) or self.http_status < 100):
            raise ValueError("http_status must be a valid HTTP status or null")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")

    @property
    def custom_questions(self) -> tuple[ApplicationQuestion, ...]:
        """Return high-value screening prompts in their form order."""

        return tuple(question for question in self.questions if question.is_custom)

    def to_dict(self) -> dict[str, Any]:
        """Return deterministic, JSON-safe result data without any answers."""

        questions = sorted(
            self.questions,
            key=lambda question: (question.ordinal, question.label.casefold(), question.field_name or ""),
        )
        return {
            "application_url": self.application_url,
            "canonical_job_id": self.canonical_job_id,
            "completeness_reason": self.completeness_reason,
            "error_message": self.error_message,
            "error_type": self.error_type,
            "http_status": self.http_status,
            "metadata": _sorted_json_value(self.metadata),
            "provider": self.provider.value,
            "questions": [question.to_dict() for question in questions],
            "scanned_at": self.scanned_at,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ApplicationScanResult":
        """Read persisted scan data and ignore non-scan bookkeeping fields."""

        questions = value.get("questions", ())
        if not isinstance(questions, (list, tuple)) or not all(isinstance(item, Mapping) for item in questions):
            raise ValueError("ApplicationScanResult questions must be a list of mappings")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("ApplicationScanResult metadata must be a mapping")
        canonical_job_id = value.get("canonical_job_id")
        application_url = value.get("application_url")
        provider = value.get("provider")
        status = value.get("status")
        if not all(isinstance(item, str) and item for item in (canonical_job_id, application_url, provider, status)):
            raise ValueError("ApplicationScanResult is missing required string fields")
        http_status = value.get("http_status")
        if http_status is not None and not isinstance(http_status, int):
            raise ValueError("ApplicationScanResult http_status must be an integer or null")
        return cls(
            canonical_job_id=canonical_job_id,
            provider=ApplicationProvider(provider),
            application_url=application_url,
            status=ScanStatus(status),
            questions=tuple(ApplicationQuestion.from_dict(question) for question in questions),
            completeness_reason=_optional_string(value.get("completeness_reason"), "completeness_reason"),
            scanned_at=_optional_string(value.get("scanned_at"), "scanned_at"),
            http_status=http_status,
            error_type=_optional_string(value.get("error_type"), "error_type"),
            error_message=_optional_string(value.get("error_message"), "error_message"),
            metadata=_sorted_json_value(metadata),
        )

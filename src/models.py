"""Normalized source observations and canonical jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def _legacy_source_details(source_file: str, job_type: str) -> tuple[str, str]:
    """Infer provenance for records written by schema v1."""

    if source_file == "README.md" and job_type == "Internship":
        return "speedyapply_internships", "SpeedyApply"
    if source_file == "NEW_GRAD_USA.md" and job_type == "New Grad":
        return "speedyapply_new_grad", "SpeedyApply"
    return "legacy_speedyapply", "SpeedyApply"


@dataclass(frozen=True)
class Job:
    """One normalized observation from a single upstream source.

    ``Job`` retains the original public name for compatibility with the
    SpeedyApply parser. Tracking code treats it as a source observation and
    aggregates many observations into one :class:`CanonicalJob`.
    """

    company: str
    position: str
    location: str
    salary: str | None
    application_url: str
    age: str | None
    category: str
    job_type: str
    source_file: str
    source_id: str = ""
    source_label: str = ""
    source_job_id: str | None = None
    source_url: str | None = None
    posted: str | None = None
    season: str | None = None
    source_metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_id or not self.source_label:
            source_id, source_label = _legacy_source_details(self.source_file, self.job_type)
            if not self.source_id:
                object.__setattr__(self, "source_id", source_id)
            if not self.source_label:
                object.__setattr__(self, "source_label", source_label)

    def to_dict(self) -> dict[str, Any]:
        """Return normalized source-derived fields in a JSON-friendly shape."""

        return {
            "age": self.age,
            "application_url": self.application_url,
            "category": self.category,
            "company": self.company,
            "job_type": self.job_type,
            "location": self.location,
            "position": self.position,
            "posted": self.posted,
            "salary": self.salary,
            "season": self.season,
            "source_file": self.source_file,
            "source_id": self.source_id,
            "source_job_id": self.source_job_id,
            "source_label": self.source_label,
            "source_metadata": dict(sorted(self.source_metadata.items())),
            "source_url": self.source_url,
        }

    def source_dict(self) -> dict[str, Any]:
        """Return static provenance fields stored under a source membership."""

        return self.to_dict()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Job":
        """Create an observation from legacy or current metadata."""

        required = (
            "company",
            "position",
            "location",
            "application_url",
            "category",
            "job_type",
            "source_file",
        )
        missing = [name for name in required if not isinstance(value.get(name), str)]
        if missing:
            raise ValueError(f"Job record is missing valid fields: {', '.join(missing)}")

        optional_strings = ("salary", "age", "source_job_id", "source_url", "posted", "season")
        parsed: dict[str, str | None] = {}
        for name in optional_strings:
            item = value.get(name)
            if item is not None and not isinstance(item, str):
                raise ValueError(f"Job {name} must be a string or null")
            parsed[name] = item
        source_metadata = value.get("source_metadata", {})
        if not isinstance(source_metadata, Mapping) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in source_metadata.items()
        ):
            raise ValueError("Job source_metadata must be a string mapping")
        source_id = value.get("source_id")
        source_label = value.get("source_label")
        inferred_id, inferred_label = _legacy_source_details(value["source_file"], value["job_type"])
        if source_id is not None and not isinstance(source_id, str):
            raise ValueError("Job source_id must be a string")
        if source_label is not None and not isinstance(source_label, str):
            raise ValueError("Job source_label must be a string")
        return cls(
            company=value["company"],
            position=value["position"],
            location=value["location"],
            salary=parsed["salary"],
            application_url=value["application_url"],
            age=parsed["age"],
            category=value["category"],
            job_type=value["job_type"],
            source_file=value["source_file"],
            source_id=source_id or inferred_id,
            source_label=source_label or inferred_label,
            source_job_id=parsed["source_job_id"],
            source_url=parsed["source_url"],
            posted=parsed["posted"],
            season=parsed["season"],
            source_metadata={str(key): str(item) for key, item in source_metadata.items()},
        )


@dataclass(frozen=True)
class CanonicalJob:
    """One real job requisition, possibly observed by several providers."""

    canonical_id: str
    company: str
    position: str
    location: str
    salary: str | None
    application_url: str
    age: str | None
    category: str
    job_type: str
    posted: str | None
    season: str | None
    observations: tuple[Job, ...]
    url_aliases: tuple[str, ...]

    @property
    def sources(self) -> tuple[Job, ...]:
        return self.observations

    def to_dict(self) -> dict[str, Any]:
        """Return durable canonical fields, excluding lifecycle timestamps."""

        return {
            "age": self.age,
            "application_url": self.application_url,
            "canonical_id": self.canonical_id,
            "category": self.category,
            "company": self.company,
            "job_type": self.job_type,
            "location": self.location,
            "position": self.position,
            "posted": self.posted,
            "salary": self.salary,
            "season": self.season,
            "url_aliases": list(self.url_aliases),
        }

    @classmethod
    def from_mapping(cls, canonical_id: str, value: Mapping[str, Any]) -> "CanonicalJob":
        """Read a persisted canonical record for rendering or notifications."""

        required = ("company", "position", "location", "application_url", "category", "job_type")
        missing = [name for name in required if not isinstance(value.get(name), str)]
        if missing:
            raise ValueError(f"Canonical record is missing valid fields: {', '.join(missing)}")
        source_values = value.get("sources", {})
        observations: list[Job] = []
        if isinstance(source_values, Mapping):
            for source_id, source_value in sorted(source_values.items()):
                if not isinstance(source_id, str) or not isinstance(source_value, Mapping):
                    continue
                parsed = dict(source_value)
                parsed.setdefault("source_id", source_id)
                try:
                    observations.append(Job.from_mapping(parsed))
                except ValueError:
                    continue
        optional = ("salary", "age", "posted", "season")
        values: dict[str, str | None] = {}
        for name in optional:
            item = value.get(name)
            values[name] = item if isinstance(item, str) else None
        aliases = value.get("url_aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
            aliases = [value["application_url"]]
        return cls(
            canonical_id=canonical_id,
            company=value["company"],
            position=value["position"],
            location=value["location"],
            salary=values["salary"],
            application_url=value["application_url"],
            age=values["age"],
            category=value["category"],
            job_type=value["job_type"],
            posted=values["posted"],
            season=values["season"],
            observations=tuple(observations),
            url_aliases=tuple(sorted(set(aliases))),
        )

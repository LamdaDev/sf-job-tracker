"""Data models shared by parsing, tracking, rendering, and notifications."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Job:
    """A normalized job row from a public SpeedyApply Markdown source."""

    company: str
    position: str
    location: str
    salary: str | None
    application_url: str
    age: str | None
    category: str
    job_type: str
    source_file: str

    def to_dict(self) -> dict[str, str | None]:
        """Return only source-derived fields, in a JSON-friendly shape."""

        return {
            "age": self.age,
            "application_url": self.application_url,
            "category": self.category,
            "company": self.company,
            "job_type": self.job_type,
            "location": self.location,
            "position": self.position,
            "salary": self.salary,
            "source_file": self.source_file,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Job":
        """Create a Job from either current-state or historical metadata."""

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

        salary = value.get("salary")
        age = value.get("age")
        if salary is not None and not isinstance(salary, str):
            raise ValueError("Job salary must be a string or null")
        if age is not None and not isinstance(age, str):
            raise ValueError("Job age must be a string or null")

        return cls(
            company=value["company"],
            position=value["position"],
            location=value["location"],
            salary=salary,
            application_url=value["application_url"],
            age=age,
            category=value["category"],
            job_type=value["job_type"],
            source_file=value["source_file"],
        )


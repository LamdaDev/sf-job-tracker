"""Location filtering and source-aware canonical history reconciliation."""

from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .canonical import aggregate_observations, fallback_fingerprint_for_fields, inspect_job_url
from .config import (
    CURRENT_JOBS_SCHEMA_VERSION,
    SAN_FRANCISCO_LOCATION_ALIASES,
    SAN_FRANCISCO_PLACE_QUALIFIERS,
    SOURCE_PRIORITY,
)
from .models import CanonicalJob, Job
from .storage import validate_seen_state


@dataclass(frozen=True)
class StateTransition:
    """The complete result of reconciling successfully checked sources."""

    state: dict[str, Any]
    current_state: dict[str, Any]
    new_jobs: tuple[CanonicalJob, ...]
    baseline: bool
    baselined_source_ids: tuple[str, ...]
    inactive_count: int
    reactivated_count: int
    duplicate_count: int


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_location(location: str) -> str:
    """Return comparison-only text; never alter the displayed source location."""

    decomposed = unicodedata.normalize("NFKD", location)
    plain = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain.casefold()).split())


def _location_segments(location: str) -> list[str]:
    """Split explicit multi-location separators before normalizing aliases."""

    split = re.split(r"(?:<br\s*/?>|\r?\n|\||;)", location, flags=re.IGNORECASE)
    return [normalize_location(segment) for segment in split if normalize_location(segment)]


def location_matches(location: str) -> bool:
    """Match San Francisco only, including ``SF`` aliases and South SF.

    State-qualified phrases can appear within a longer string (for example
    ``Remote - San Francisco, CA +1``). Bare aliases are accepted only as a
    complete multi-location segment, which avoids matching ``SF Bay Area`` or
    arbitrary words containing the letters ``sf``.
    """

    normalized = normalize_location(location)
    if not normalized:
        return False
    padded = f" {normalized} "
    for phrase in ("san francisco ca", "san francisco california", "sf ca", "sf california", "s f ca", "s f california"):
        if f" {phrase} " in padded:
            return True
    for qualifier in SAN_FRANCISCO_PLACE_QUALIFIERS:
        for state in ("ca", "california"):
            if f" san francisco {qualifier} {state} " in padded:
                return True
    return any(segment in SAN_FRANCISCO_LOCATION_ALIASES for segment in _location_segments(location))


def notification_batch_id(ids: Iterable[str]) -> str:
    """Create a stable identifier used in state and a hidden GitHub Issue marker."""

    payload = "\n".join(sorted(ids)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_sort_key(job: CanonicalJob) -> tuple[str, str, str]:
    return (job.company.casefold(), job.position.casefold(), job.canonical_id)


def _source_static_changed(record: dict[str, Any], observation: Job) -> bool:
    expected = observation.source_dict()
    # ApplyGuy's feed-level updatedAt changes even when a particular listing
    # does not. It is useful provenance, but must not manufacture hourly state
    # churn for every unchanged job.
    expected_metadata = dict(expected.get("source_metadata", {}))
    current_metadata = dict(record.get("source_metadata", {}))
    for volatile_key in ("feedUpdatedAt", "age"):
        expected_metadata.pop(volatile_key, None)
        current_metadata.pop(volatile_key, None)
    expected["source_metadata"] = expected_metadata
    comparison = dict(record)
    comparison["source_metadata"] = current_metadata
    # Relative age changes with time, not with a meaningful listing event.
    # Preserve the original display value until some real source field changes.
    expected["age"] = comparison.get("age")
    return any(comparison.get(key) != value for key, value in expected.items())


def _merged_canonical_static(record: dict[str, Any], job: CanonicalJob) -> dict[str, Any]:
    """Merge current data without degrading richer metadata from a failed source."""

    expected = job.to_dict()
    # A relative source age would otherwise change every day and manufacture a
    # commit. Structured posted dates are handled independently below.
    if isinstance(record.get("age"), str):
        expected["age"] = record["age"]
    for nullable_field in ("salary", "posted", "season"):
        if expected.get(nullable_field) is None and isinstance(record.get(nullable_field), str):
            expected[nullable_field] = record[nullable_field]
    if isinstance(record.get("category"), str) and (
        expected.get("category") == "Unknown" or record["category"] in {"FAANG+", "Quant"}
    ):
        # Keep the higher-confidence SpeedyApply classification when a source
        # with only role-category/unknown data is all that was fetched.
        expected["category"] = record["category"]
    # Keep a richer historical presentation if only a less detailed source was
    # available this hour. A later longer source value can still improve it.
    for field in ("company", "position", "location"):
        if isinstance(record.get(field), str) and len(record[field]) > len(str(expected.get(field) or "")):
            expected[field] = record[field]
    # Older Simplify parsing retained <details> summary text (for example
    # ``5 locations``) ahead of the actual places. Prefer the cleaned current
    # representation rather than preserving that presentation artifact merely
    # because it is longer.
    if (
        isinstance(record.get("location"), str)
        and re.match(r"^\d+\s+locations\b", record["location"], flags=re.IGNORECASE)
        and isinstance(job.location, str)
        and not re.match(r"^\d+\s+locations\b", job.location, flags=re.IGNORECASE)
    ):
        expected["location"] = job.location
    previous_url = record.get("application_url")
    if isinstance(previous_url, str):
        previous_identity = inspect_job_url(previous_url)
        incoming_identity = inspect_job_url(expected["application_url"])
        if previous_identity.direct and not incoming_identity.direct:
            expected["application_url"] = previous_url
    return expected


def _canonical_static_changed(record: dict[str, Any], job: CanonicalJob) -> bool:
    expected = _merged_canonical_static(record, job)
    # Aliases only ever grow. Dropping a source one hour should not erase a
    # useful previously observed direct/wrapper URL.
    expected_aliases = set(expected.pop("url_aliases", []))
    current_aliases = set(record.get("url_aliases", []))
    if not expected_aliases.issubset(current_aliases):
        return True
    return any(record.get(key) != value for key, value in expected.items())


def _new_record(job: CanonicalJob, timestamp: str) -> dict[str, Any]:
    record = job.to_dict()
    record.update(
        {
            "active": True,
            "first_seen": timestamp,
            "inactive_at": None,
            "last_seen": timestamp,
            "sources": {},
        }
    )
    for observation in job.observations:
        source = observation.source_dict()
        source.update(
            {
                "active": True,
                "first_seen": timestamp,
                "inactive_at": None,
                "last_seen": timestamp,
            }
        )
        record["sources"][observation.source_id] = source
    return record


def _replace_pending_job_id(state: dict[str, Any], old_id: str, new_id: str) -> None:
    """Keep an existing pending batch deliverable after safe ID promotion."""

    for batch in state.get("pending_notifications", {}).values():
        if not isinstance(batch, dict) or not isinstance(batch.get("job_ids"), list):
            continue
        batch["job_ids"] = sorted(
            {new_id if job_id == old_id else job_id for job_id in batch["job_ids"]}
        )


def _promote_matching_fallback_record(
    state: dict[str, Any], job: CanonicalJob
) -> dict[str, Any] | None:
    """Promote a known wrapper-only job when direct ATS evidence appears later.

    A fallback key is deliberately exact (company, title, type, location,
    season). It is therefore safe to replace that provisional identity with a
    direct requisition identity, preserving history and avoiding a false new
    alert when an aggregator later exposes the employer link.
    """

    if job.canonical_id.startswith("fallback:"):
        return None
    fallback_id = "fallback:" + fallback_fingerprint_for_fields(
        job.company, job.position, job.job_type, job.location, job.season
    )
    record = state["jobs"].get(fallback_id)
    if not isinstance(record, dict):
        return None
    state["jobs"].pop(fallback_id)
    record["canonical_id"] = job.canonical_id
    state["jobs"][job.canonical_id] = record
    _replace_pending_job_id(state, fallback_id, job.canonical_id)
    return record


def _update_observed_record(record: dict[str, Any], job: CanonicalJob, timestamp: str) -> tuple[bool, bool]:
    """Merge canonical metadata and current source memberships.

    Returns ``(meaningful_change, reactivated)``. Reappearing jobs deliberately
    do not become a new notification candidate.
    """

    changed = False
    was_active = record.get("active") is True
    if _canonical_static_changed(record, job):
        static = _merged_canonical_static(record, job)
        aliases = sorted(set(record.get("url_aliases", [])) | set(static.pop("url_aliases", [])))
        record.update(static)
        record["url_aliases"] = aliases
        changed = True
    sources = record.setdefault("sources", {})
    if not isinstance(sources, dict):
        raise ValueError("Canonical job sources must be a mapping")
    for observation in job.observations:
        existing = sources.get(observation.source_id)
        if existing is None:
            source_record = observation.source_dict()
            source_record.update(
                {
                    "active": True,
                    "first_seen": timestamp,
                    "inactive_at": None,
                    "last_seen": timestamp,
                }
            )
            sources[observation.source_id] = source_record
            changed = True
            continue
        if not isinstance(existing, dict):
            raise ValueError(f"Source record {observation.source_id} is not an object")
        source_changed = _source_static_changed(existing, observation)
        source_reactivated = existing.get("active") is not True
        if source_changed or source_reactivated:
            existing.update(observation.source_dict())
            existing["active"] = True
            existing["inactive_at"] = None
            existing["last_seen"] = timestamp
            changed = True
    record["active"] = True
    record["inactive_at"] = None
    reactivated = not was_active
    if changed or reactivated:
        record["last_seen"] = timestamp
    return changed or reactivated, reactivated


def _deactivate_missing_sources(
    state: dict[str, Any],
    observed: dict[str, CanonicalJob],
    successful_source_ids: set[str],
    timestamp: str,
) -> tuple[int, int]:
    """Mark only successful-but-absent source memberships inactive."""

    inactive_count = 0
    reactivated_count = 0
    for canonical_id, record in state["jobs"].items():
        if not isinstance(record, dict):
            raise ValueError(f"Seen canonical record for {canonical_id} is not an object")
        sources = record.get("sources")
        if not isinstance(sources, dict):
            raise ValueError(f"Seen canonical record for {canonical_id} has invalid sources")
        current_observation = observed.get(canonical_id)
        observed_source_ids = (
            {observation.source_id for observation in current_observation.observations}
            if current_observation is not None
            else set()
        )
        source_changed = False
        for source_id, source_record in sources.items():
            if source_id not in successful_source_ids or source_id in observed_source_ids:
                continue
            if not isinstance(source_record, dict):
                raise ValueError(f"Source record {source_id} is not an object")
            if source_record.get("active") is True:
                source_record["active"] = False
                source_record["inactive_at"] = timestamp
                source_changed = True
        is_active = any(isinstance(source, dict) and source.get("active") is True for source in sources.values())
        was_active = record.get("active") is True
        if is_active != was_active:
            record["active"] = is_active
            record["inactive_at"] = None if is_active else timestamp
            record["last_seen"] = timestamp if is_active else record.get("last_seen")
            if is_active:
                reactivated_count += 1
            else:
                inactive_count += 1
        elif source_changed and is_active:
            # A membership changed but the global job stayed active. Keep the
            # job's last_seen event-driven rather than advancing on every poll.
            pass
    return inactive_count, reactivated_count


def _current_state(state: dict[str, Any]) -> dict[str, Any]:
    jobs: dict[str, Any] = {}
    for canonical_id, record in sorted(state["jobs"].items()):
        if isinstance(record, dict) and record.get("active") is True:
            current_record = {
                key: copy.deepcopy(value)
                for key, value in record.items()
                if key not in {"first_seen", "last_seen", "inactive_at", "active"}
            }
            sources = current_record.get("sources")
            if isinstance(sources, dict):
                current_record["sources"] = {
                    source_id: {
                        key: copy.deepcopy(value)
                        for key, value in source.items()
                        if key not in {"first_seen", "last_seen", "inactive_at", "active"}
                    }
                    for source_id, source in sorted(sources.items())
                    if isinstance(source, dict) and source.get("active") is True
                }
            jobs[canonical_id] = current_record
    return {"jobs": jobs, "schema_version": CURRENT_JOBS_SCHEMA_VERSION}


def _coerce_canonical_jobs(jobs: Iterable[CanonicalJob | Job]) -> tuple[list[CanonicalJob], int]:
    values = list(jobs)
    if not values:
        return [], 0
    if all(isinstance(job, CanonicalJob) for job in values):
        canonical = sorted((job for job in values if isinstance(job, CanonicalJob)), key=_canonical_sort_key)
        return canonical, 0
    if not all(isinstance(job, Job) for job in values):
        raise ValueError("Current jobs must be normalized observations or canonical jobs")
    return aggregate_observations(job for job in values if isinstance(job, Job))


def deduplicate_jobs(jobs: Iterable[Job]) -> tuple[list[CanonicalJob], int]:
    """Compatibility helper: canonicalize observations rather than raw URLs."""

    return aggregate_observations(jobs)


def apply_current_jobs(
    existing_state: dict[str, Any],
    current_jobs: Iterable[CanonicalJob | Job],
    *,
    successful_source_ids: Iterable[str] | None = None,
    timestamp: str | None = None,
    initialize: bool = False,
) -> StateTransition:
    """Reconcile a partial or complete successful source snapshot safely."""

    state = copy.deepcopy(validate_seen_state(existing_state))
    observed_jobs, duplicate_count = _coerce_canonical_jobs(current_jobs)
    observed_by_id = {job.canonical_id: job for job in observed_jobs}
    successful = set(successful_source_ids or {
        observation.source_id for job in observed_jobs for observation in job.observations
    })
    now = timestamp or utc_timestamp()
    baseline = initialize or not state["initialized"]
    initialized_sources = state["initialized_sources"]
    baselined_sources = tuple(sorted(source_id for source_id in successful if not initialized_sources.get(source_id, False)))
    new_jobs: list[CanonicalJob] = []
    reactivated_count = 0

    for canonical_id in sorted(observed_by_id):
        job = observed_by_id[canonical_id]
        record = state["jobs"].get(canonical_id)
        if record is None:
            record = _promote_matching_fallback_record(state, job)
        if record is None:
            state["jobs"][canonical_id] = _new_record(job, now)
            observed_initialized = any(
                initialized_sources.get(observation.source_id, False) for observation in job.observations
            )
            if not baseline and observed_initialized and not initialize:
                new_jobs.append(job)
            continue
        if not isinstance(record, dict):
            raise ValueError(f"Seen canonical record for {canonical_id} is not an object")
        _changed, reactivated = _update_observed_record(record, job, now)
        if reactivated:
            reactivated_count += 1

    inactive_count, source_reactivated = _deactivate_missing_sources(state, observed_by_id, successful, now)
    reactivated_count += source_reactivated

    if not state["initialized"]:
        state["initialized"] = True
        state["initialized_at"] = now
    for source_id in successful:
        initialized_sources[source_id] = True

    if new_jobs:
        batch_id = notification_batch_id(job.canonical_id for job in new_jobs)
        if batch_id not in state["pending_notifications"]:
            state["pending_notifications"][batch_id] = {
                "created_at": now,
                "issue_number": None,
                "job_ids": sorted(job.canonical_id for job in new_jobs),
                "job_urls": sorted(job.application_url for job in new_jobs),
                "status": "pending",
            }

    return StateTransition(
        state=state,
        current_state=_current_state(state),
        new_jobs=tuple(sorted(new_jobs, key=_canonical_sort_key)),
        baseline=baseline,
        baselined_source_ids=baselined_sources,
        inactive_count=inactive_count,
        reactivated_count=reactivated_count,
        duplicate_count=duplicate_count,
    )

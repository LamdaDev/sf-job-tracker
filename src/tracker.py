"""Pure-ish permanent-history and current-snapshot transition logic."""

from __future__ import annotations

import copy
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import (
    BAY_AREA_CITY_ALIASES,
    BAY_AREA_CITY_NAMES,
    BAY_AREA_REGION_ALIASES,
    BAY_AREA_UNAMBIGUOUS_REGION_ALIASES,
    CALIFORNIA_LOCATION_TOKENS,
    CURRENT_JOBS_SCHEMA_VERSION,
    LOCATION_SCOPE_VERSION,
    LOCATION_PLACE_QUALIFIERS,
)
from .models import Job
from .storage import validate_seen_state

LOGGER = logging.getLogger(__name__)

_NON_CALIFORNIA_US_STATE_CODES = frozenset(
    """
    ak al ar az co ct dc de fl ga hi ia id il in ks ky la ma md me mi mn mo ms mt
    nc nd ne nh nj nm nv ny oh ok or pa ri sc sd tn tx ut va vt wa wi wv wy
    """.split()
)


@dataclass(frozen=True)
class StateTransition:
    """The complete result of reconciling one successful upstream snapshot."""

    state: dict[str, Any]
    current_state: dict[str, Any]
    new_jobs: tuple[Job, ...]
    baseline: bool
    inactive_count: int
    reactivated_count: int
    duplicate_count: int
    scope_rebased: bool


def utc_timestamp() -> str:
    """Return a compact, timezone-aware UTC timestamp suitable for JSON."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_location(location: str) -> str:
    """Return comparison-only location text without changing the stored value."""

    decomposed = unicodedata.normalize("NFKD", location)
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_accents.casefold()).split())


def _contains_phrase(normalized_location: str, phrase: str) -> bool:
    """Match a whole normalized phrase, never an arbitrary substring."""

    return f" {phrase} " in f" {normalized_location} "


def _matches_california_place(normalized_location: str, place: str) -> bool:
    """Require a configured place and CA spelling, allowing known descriptors."""

    for state in CALIFORNIA_LOCATION_TOKENS:
        if _contains_phrase(normalized_location, f"{place} {state}"):
            return True
        if any(
            _contains_phrase(normalized_location, f"{place} {qualifier} {state}")
            for qualifier in LOCATION_PLACE_QUALIFIERS
        ):
            return True
    return False


def _matches_unambiguous_region(normalized_location: str) -> bool:
    """Accept named SF regional phrases unless explicitly paired with another state."""

    for region in BAY_AREA_UNAMBIGUOUS_REGION_ALIASES:
        if not _contains_phrase(normalized_location, region):
            continue
        if any(
            _contains_phrase(normalized_location, f"{region} {state}")
            for state in _NON_CALIFORNIA_US_STATE_CODES
        ):
            continue
        return True
    return False


def location_matches(location: str) -> bool:
    """Match the configured, deterministic San Francisco Bay Area scope.

    This deliberately recognizes exact city/alias phrases paired with a
    California spelling. It does not attempt live route estimates, infer the
    hidden locations behind ``+N``, or use fuzzy matching.
    """

    normalized_location = normalize_location(location)
    if not normalized_location:
        return False

    if _matches_unambiguous_region(normalized_location):
        return True

    places_requiring_california = (
        *BAY_AREA_CITY_NAMES,
        *BAY_AREA_CITY_ALIASES,
        *BAY_AREA_REGION_ALIASES,
    )
    return any(
        _matches_california_place(normalized_location, place)
        for place in places_requiring_california
    )


def deduplicate_jobs(jobs: Iterable[Job]) -> tuple[list[Job], int]:
    """Collapse only exact repeated application URLs, preserving first appearance."""

    unique: dict[str, Job] = {}
    duplicates = 0
    for job in jobs:
        if job.application_url in unique:
            duplicates += 1
            LOGGER.warning("Ignoring duplicate application URL in current snapshot: %s", job.application_url)
            continue
        unique[job.application_url] = job
    return list(unique.values()), duplicates


def notification_batch_id(urls: Iterable[str]) -> str:
    """Create a stable identifier used in state and a hidden GitHub Issue marker."""

    payload = "\n".join(sorted(urls)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_metadata_changed(record: dict[str, Any], job: Job) -> bool:
    return any(record.get(key) != value for key, value in job.to_dict().items())


def _record_for_new_job(job: Job, timestamp: str) -> dict[str, Any]:
    record: dict[str, Any] = dict(job.to_dict())
    record.update(
        {
            "active": True,
            "first_seen": timestamp,
            "inactive_at": None,
            "last_seen": timestamp,
        }
    )
    return record


def _update_existing_record(record: dict[str, Any], job: Job, timestamp: str) -> bool:
    """Refresh a record only when data or its active state meaningfully changes."""

    metadata_changed = _source_metadata_changed(record, job)
    was_active = record.get("active") is True
    if not metadata_changed and was_active:
        return False

    record.update(job.to_dict())
    record["active"] = True
    record["inactive_at"] = None
    record["last_seen"] = timestamp
    return True


def _current_state(current_jobs: Iterable[Job]) -> dict[str, Any]:
    return {
        "jobs": {job.application_url: job.to_dict() for job in sorted(current_jobs, key=lambda job: job.application_url)},
        "schema_version": CURRENT_JOBS_SCHEMA_VERSION,
    }


def apply_current_jobs(
    existing_state: dict[str, Any],
    current_jobs: Iterable[Job],
    *,
    timestamp: str | None = None,
    initialize: bool = False,
) -> StateTransition:
    """Reconcile permanent history with a validated current matching snapshot.

    This function deliberately does not write files or contact GitHub. It makes
    first-run baselining, URL-only deduplication, inactive transitions, and
    retryable notification batches straightforward to test.
    """

    state = copy.deepcopy(validate_seen_state(existing_state))
    observed_jobs, duplicate_count = deduplicate_jobs(current_jobs)
    observed_by_url = {job.application_url: job for job in observed_jobs}
    now = timestamp or utc_timestamp()
    scope_rebased = state.get("location_scope_version") != LOCATION_SCOPE_VERSION
    baseline = initialize or not state["initialized"] or scope_rebased
    new_jobs: list[Job] = []
    reactivated_count = 0

    history = state["jobs"]
    for url in sorted(observed_by_url):
        job = observed_by_url[url]
        record = history.get(url)
        if record is None:
            history[url] = _record_for_new_job(job, now)
            if not baseline:
                new_jobs.append(job)
            continue
        if not isinstance(record, dict):
            raise ValueError(f"Seen job record for {url} is not an object")
        was_active = record.get("active") is True
        changed = _update_existing_record(record, job, now)
        if changed and not was_active:
            reactivated_count += 1

    inactive_count = 0
    for url, record in history.items():
        if url in observed_by_url:
            continue
        if not isinstance(record, dict):
            raise ValueError(f"Seen job record for {url} is not an object")
        if record.get("active") is True:
            record["active"] = False
            record["inactive_at"] = now
            # last_seen remains the most recent persisted observation. Updating
            # it every hourly poll would create meaningless commits.
            inactive_count += 1

    if not state["initialized"]:
        state["initialized"] = True
        state["initialized_at"] = now
    if scope_rebased:
        state["location_scope_version"] = LOCATION_SCOPE_VERSION

    if new_jobs:
        batch_id = notification_batch_id(job.application_url for job in new_jobs)
        if batch_id not in state["pending_notifications"]:
            state["pending_notifications"][batch_id] = {
                "created_at": now,
                "issue_number": None,
                "job_urls": sorted(job.application_url for job in new_jobs),
                "status": "pending",
            }

    return StateTransition(
        state=state,
        current_state=_current_state(observed_jobs),
        new_jobs=tuple(sorted(new_jobs, key=lambda job: (job.company.casefold(), job.position.casefold(), job.application_url))),
        baseline=baseline,
        inactive_count=inactive_count,
        reactivated_count=reactivated_count,
        duplicate_count=duplicate_count,
        scope_rebased=scope_rebased,
    )

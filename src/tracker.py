"""Pure-ish permanent-history and current-snapshot transition logic."""

from __future__ import annotations

import copy
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import CURRENT_JOBS_SCHEMA_VERSION
from .models import Job
from .storage import validate_seen_state

LOGGER = logging.getLogger(__name__)


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


def utc_timestamp() -> str:
    """Return a compact, timezone-aware UTC timestamp suitable for JSON."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def location_matches(location: str, target_location: str) -> bool:
    """Use the required case-insensitive literal substring semantics."""

    return target_location.casefold() in location.casefold()


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
    baseline = initialize or not state["initialized"]
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
    )


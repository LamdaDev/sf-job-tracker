"""CLI orchestration for multi-source collection, state, and notifications."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .canonical import aggregate_observations
from .config import (
    SOURCES,
    TARGET_LOCATION_LABEL,
    SourceConfig,
    application_scan_enabled,
    application_scan_max_attempts,
    application_scan_only_new_jobs,
)
from .fetcher import FetchedSnapshot, UpstreamFetchError, fetch_upstream_sources
from .models import CanonicalJob
from .notifier import (
    DeliveryResult,
    GitHubIssueNotifier,
    GitHubNotificationError,
    deliver_pending_notifications,
    jobs_for_notification_batch,
    send_test_application_scan_issue as send_test_application_scan_issue_notification,
    send_test_notification as send_test_issue_notification,
)
from .parser import UpstreamFormatError, parse_configured_source_with_diagnostics
from .renderer import render_jobs_markdown
from .storage import (
    StorageError,
    load_application_questions_state,
    load_current_state,
    load_seen_state,
    serialise_json,
    validate_application_questions_state,
    write_json_if_changed,
    write_texts_transactionally,
)
from .tracker import StateTransition, apply_current_jobs, location_matches, utc_timestamp


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_APPLICATION_SCAN_URL = "https://jobs.ashbyhq.com/replit/7e0dafe8-3eec-442e-aa76-a4d84d779fb1"
_URL_IN_ERROR_MESSAGE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT_IN_ERROR_MESSAGE = re.compile(
    r"\b(cookie|csrf(?:[_ -]?token)?|(?:access|auth|refresh|session|id)?[_ -]?token|authorization)\s*[=:]\s*[^\s,;]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RunSummary:
    """Useful details from one collection run, including dry runs."""

    snapshot_sha: str
    revisions: dict[str, str]
    parsed_counts: dict[str, int]
    matching_counts: dict[str, int]
    matching_count: int
    canonical_count: int
    known_before_count: int
    transition: StateTransition
    files_changed: tuple[Path, ...]
    matching_duplicate_count: int
    source_errors: dict[str, str]


def _state_paths(root: Path) -> tuple[Path, Path, Path]:
    return root / "data" / "seen_jobs.json", root / "data" / "current_jobs.json", root / "jobs.md"


def _application_questions_path(root: Path) -> Path:
    return root / "data" / "application_questions.json"


def _parse_snapshot(
    snapshot: FetchedSnapshot,
) -> tuple[list, dict[str, int], dict[str, int], set[str], dict[str, str]]:
    """Parse every independently healthy feed, preserving partial failures."""

    parsed_jobs = []
    parsed_counts: dict[str, int] = {}
    matching_counts: dict[str, int] = {}
    successful: set[str] = set()
    errors = dict(snapshot.errors)
    for source in SOURCES:
        document = snapshot.document_for(source)
        if document is None:
            errors.setdefault(source.id, "source document was not fetched")
            continue
        try:
            parsed = parse_configured_source_with_diagnostics(document, source)
        except UpstreamFormatError as error:
            errors[source.id] = str(error)
            LOGGER.error("Could not parse %s; source presence is preserved: %s", source.id, error)
            continue
        source_jobs = list(parsed.jobs)
        parsed_counts[source.id] = len(source_jobs)
        matching = [job for job in source_jobs if location_matches(job.location)]
        matching_counts[source.id] = len(matching)
        parsed_jobs.extend(matching)
        successful.add(source.id)
    if not successful:
        details = "; ".join(f"{source_id}: {message}" for source_id, message in sorted(errors.items()))
        raise UpstreamFormatError(f"No configured source parsed successfully. {details}")
    return parsed_jobs, parsed_counts, matching_counts, successful, errors


def _log_collection_summary(summary: RunSummary, *, dry_run: bool) -> None:
    revisions = ", ".join(f"{key}={sha[:12]}" for key, sha in sorted(summary.revisions.items()))
    LOGGER.info("Fetched source revisions: %s", revisions or "fixture/no revision")
    for source in SOURCES:
        if source.id in summary.parsed_counts:
            LOGGER.info(
                "%s (%s): %s parsed, %s matching %s",
                source.id,
                source.label,
                summary.parsed_counts[source.id],
                summary.matching_counts[source.id],
                TARGET_LOCATION_LABEL,
            )
        else:
            LOGGER.warning("%s (%s): unavailable this run", source.id, source.label)
    LOGGER.info("Raw matching observations: %s", summary.matching_count)
    LOGGER.info("Canonical matching jobs: %s", summary.canonical_count)
    LOGGER.info("Known canonical jobs before this run: %s", summary.known_before_count)
    if summary.transition.baseline:
        mode = "would establish" if dry_run else "established"
        LOGGER.info("Baseline mode: %s history without alerting.", mode)
    if summary.transition.baselined_source_ids:
        LOGGER.info(
            "Silently onboarded source(s): %s",
            ", ".join(summary.transition.baselined_source_ids),
        )
    LOGGER.info("New canonical matching jobs: %s", len(summary.transition.new_jobs))
    LOGGER.info("Canonical jobs marked inactive: %s", summary.transition.inactive_count)
    if summary.transition.reactivated_count:
        LOGGER.info("Canonical jobs reactivated without a new alert: %s", summary.transition.reactivated_count)
    if summary.matching_duplicate_count:
        LOGGER.info("Collapsed %s duplicate matching observation(s)", summary.matching_duplicate_count)
    for source_id, message in sorted(summary.source_errors.items()):
        LOGGER.warning("Source failure preserved as unknown (%s): %s", source_id, message)
    for job in summary.transition.new_jobs:
        LOGGER.info("NEW: %s — %s [%s]", job.company, job.position, job.canonical_id)
    if dry_run:
        LOGGER.info("Dry run: no state, dashboard, or notifications were modified.")


def run_tracker(
    *,
    root: Path = PROJECT_ROOT,
    dry_run: bool = False,
    initialize: bool = False,
    snapshot_fetcher: Callable[[], FetchedSnapshot] = fetch_upstream_sources,
    timestamp: str | None = None,
) -> RunSummary:
    """Fetch, aggregate, and reconcile sources without delivering Issues."""

    snapshot = snapshot_fetcher()
    matching_observations, parsed_counts, matching_counts, successful, source_errors = _parse_snapshot(snapshot)
    canonical_jobs, duplicate_count = aggregate_observations(matching_observations)

    seen_path, current_path, dashboard_path = _state_paths(root)
    existing_state = load_seen_state(seen_path)
    # Validate existing current state rather than silently masking a manually
    # corrupted generated file. Migration happens in memory until a real run.
    load_current_state(current_path)
    known_before_count = len(existing_state["jobs"])
    transition = apply_current_jobs(
        existing_state,
        canonical_jobs,
        successful_source_ids=successful,
        timestamp=timestamp,
        initialize=initialize,
    )

    changed: list[Path] = []
    if not dry_run:
        dashboard = render_jobs_markdown(transition.state)
        changed.extend(
            write_texts_transactionally(
                {
                    current_path: serialise_json(transition.current_state),
                    dashboard_path: dashboard,
                    # History is the final durable commit point.
                    seen_path: serialise_json(transition.state),
                }
            )
        )
    summary = RunSummary(
        snapshot_sha=snapshot.commit_sha,
        revisions=dict(snapshot.revisions),
        parsed_counts=parsed_counts,
        matching_counts=matching_counts,
        matching_count=len(matching_observations),
        canonical_count=len(canonical_jobs),
        known_before_count=known_before_count,
        transition=transition,
        files_changed=tuple(changed),
        matching_duplicate_count=duplicate_count,
        source_errors=source_errors,
    )
    _log_collection_summary(summary, dry_run=dry_run)
    return summary


@dataclass(frozen=True)
class ApplicationEnrichmentResult:
    """Best-effort results kept separate from job-alert delivery success."""

    attempted_job_ids: tuple[str, ...]
    persisted_job_ids: tuple[str, ...]
    updated_issue_job_ids: tuple[str, ...]
    failed_job_ids: tuple[str, ...]


@dataclass(frozen=True)
class _ManualApplicationScanTarget:
    """Small, state-free target used only by the manual test workflow."""

    canonical_id: str
    application_url: str


def _application_inspection_api() -> Any:
    """Load the optional subsystem only when notification enrichment needs it."""

    from . import application_inspection

    return application_inspection


def _status_value(value: Any) -> str:
    raw_value = getattr(value, "value", value)
    return raw_value if isinstance(raw_value, str) else str(raw_value)


def _safe_scan_message(value: Any) -> Any:
    """Keep technical scan reasons useful without persisting sensitive detail."""

    if value is None or not isinstance(value, str):
        return value
    redacted = _URL_IN_ERROR_MESSAGE.sub("<url>", value)
    redacted = _SENSITIVE_ASSIGNMENT_IN_ERROR_MESSAGE.sub(r"\1=<redacted>", redacted)
    # A parser should never return page HTML as an error. This cap is a second
    # guard against accidental diagnostic dumps in the public JSON state.
    return redacted[:500]


def _scan_record_for_result(
    result: Any,
    *,
    canonical_job_id: str,
    prior_record: Mapping[str, Any] | None,
    issue_number: int,
) -> dict[str, Any]:
    """Add retry/delivery metadata without changing the scan-result model."""

    if not hasattr(result, "to_dict"):
        raise ValueError("Application inspector returned a result without to_dict()")
    record = dict(result.to_dict())
    record["canonical_job_id"] = canonical_job_id
    # The public scan model serializes enums itself, but normalize defensively
    # so store validation and retry policy always see a stable string.
    if "status" in record:
        record["status"] = _status_value(record["status"])
    if isinstance(record.get("questions"), tuple):
        record["questions"] = list(record["questions"])
    for field in ("completeness_reason", "error_message"):
        if field in record:
            record[field] = _safe_scan_message(record[field])
    scanned_at = record.get("scanned_at")
    timestamp = scanned_at if isinstance(scanned_at, str) and scanned_at else utc_timestamp()
    attempts = 1
    if prior_record is not None and isinstance(prior_record.get("attempt_count"), int):
        attempts = prior_record["attempt_count"] + 1
    record["attempt_count"] = attempts
    record["first_scanned"] = (
        prior_record.get("first_scanned")
        if prior_record is not None and isinstance(prior_record.get("first_scanned"), str)
        else timestamp
    )
    record["last_scanned"] = timestamp
    record["issue_number"] = issue_number
    # Persist this intent before touching GitHub. If a PATCH fails, the next
    # delivery run can render the same result into the existing Issue.
    record["issue_update_pending"] = True
    return record


def _persist_application_scan_state(path: Path, state: dict[str, Any]) -> bool:
    """Validate and durably write scan state before an Issue-body mutation."""

    validate_application_questions_state(state)
    return write_json_if_changed(path, state)


def _scan_result_from_record(api: Any, record: Mapping[str, Any]) -> Any:
    """Discard orchestration metadata before rebuilding the public result."""

    integration_keys = {
        "attempt_count",
        "first_scanned",
        "last_scanned",
        "issue_number",
        "issue_update_pending",
        "next_retry_after",
    }
    payload = {key: value for key, value in record.items() if key not in integration_keys}
    return api.ApplicationScanResult.from_dict(payload)


def _delivery_scan_targets(
    state: Mapping[str, Any], delivery: DeliveryResult, *, require_scan_eligibility: bool = True
) -> dict[str, tuple[CanonicalJob, int]]:
    """Return only jobs whose alert Issue was resolved in this invocation."""

    history = state.get("jobs")
    pending = state.get("pending_notifications")
    if not isinstance(history, Mapping) or not isinstance(pending, Mapping):
        return {}
    targets: dict[str, tuple[CanonicalJob, int]] = {}
    for batch_id, issue_number in delivery.batch_issue_numbers:
        batch = pending.get(batch_id)
        if not isinstance(batch, Mapping):
            continue
        if require_scan_eligibility and batch.get("application_scan_eligible") is not True:
            # Batches persisted before application enrichment was introduced
            # are alert-deliverable, but deliberately not scan candidates.
            continue
        try:
            for job in jobs_for_notification_batch(history, batch):
                targets.setdefault(job.canonical_id, (job, issue_number))
        except (KeyError, ValueError) as error:
            # The alert itself has already been created/resolved. A malformed
            # old batch must never turn optional enrichment into a job-alert
            # failure.
            LOGGER.error("Could not resolve application scan target for batch %s: %s", batch_id, error)
    return targets


def _retry_scan_targets(
    state: Mapping[str, Any], scans: Mapping[str, Any], *, max_attempts: int
) -> dict[str, tuple[CanonicalJob, int]]:
    """Bound retries to failures previously caused by a new-job alert scan."""

    history = state.get("jobs")
    if not isinstance(history, Mapping):
        return {}
    targets: dict[str, tuple[CanonicalJob, int]] = {}
    for canonical_job_id, record in scans.items():
        if not isinstance(canonical_job_id, str) or not isinstance(record, Mapping):
            continue
        if record.get("status") != "failed":
            continue
        attempts = record.get("attempt_count")
        issue_number = record.get("issue_number")
        if not isinstance(attempts, int) or attempts >= max_attempts:
            continue
        if not isinstance(issue_number, int) or issue_number < 1:
            continue
        job_record = history.get(canonical_job_id)
        if not isinstance(job_record, Mapping):
            continue
        try:
            targets[canonical_job_id] = (CanonicalJob.from_mapping(canonical_job_id, job_record), issue_number)
        except ValueError as error:
            LOGGER.error("Could not restore retry scan target %s: %s", canonical_job_id, error)
    return targets


def _previously_alerted_scan_targets(state: Mapping[str, Any]) -> dict[str, tuple[CanonicalJob, int]]:
    """Resolve an explicit opt-in backfill from already-created alert Issues.

    Normal production never calls this. It deliberately considers only jobs
    that already have a real job-alert Issue, not the broader historical job
    baseline.
    """

    pending = state.get("pending_notifications")
    if not isinstance(pending, Mapping):
        return {}
    prior_batches = tuple(
        sorted(
            (
                (batch_id, batch["issue_number"])
                for batch_id, batch in pending.items()
                if isinstance(batch_id, str)
                and isinstance(batch, Mapping)
                and isinstance(batch.get("issue_number"), int)
                and batch["issue_number"] > 0
            )
        )
    )
    return _delivery_scan_targets(
        state,
        DeliveryResult((), (), (), prior_batches),
        require_scan_eligibility=False,
    )


def _pending_issue_update_targets(scans: Mapping[str, Any]) -> dict[str, int]:
    """Find saved scan results whose prior same-Issue update did not finish."""

    targets: dict[str, int] = {}
    for canonical_job_id, record in scans.items():
        if not isinstance(canonical_job_id, str) or not isinstance(record, Mapping):
            continue
        issue_number = record.get("issue_number")
        if record.get("issue_update_pending") is True and isinstance(issue_number, int) and issue_number > 0:
            targets[canonical_job_id] = issue_number
    return targets


def _failed_scan_result(api: Any, job: CanonicalJob, error: Exception) -> Any:
    """Convert unexpected inspector failures into a persisted failed result."""

    provider = api.detect_application_provider(job.application_url)
    return api.failed_scan_result(job, job.application_url, provider, error)


def enrich_delivered_job_issues(
    *,
    root: Path,
    state: Mapping[str, Any],
    delivery: DeliveryResult,
    notifier: Any,
    inspector_factory: Callable[[], Any] | None = None,
    scan_only_new_jobs: bool = True,
    max_attempts: int = 2,
) -> ApplicationEnrichmentResult:
    """Inspect only newly alerted/retry jobs, after their alert Issues exist.

    Every per-job operation is isolated. In particular, an application site
    timeout, invalid saved result, local scan-state write failure, or Issue
    body PATCH failure never changes delivery status for this or another job.
    """

    try:
        api = _application_inspection_api()
        scan_path = _application_questions_path(root)
        scan_state = load_application_questions_state(scan_path)
    except Exception as error:
        LOGGER.error("Application enrichment was skipped without affecting job alerts: %s", error)
        return ApplicationEnrichmentResult((), (), (), ())

    scans = scan_state["scans"]
    delivery_targets = _delivery_scan_targets(state, delivery)
    retry_targets = _retry_scan_targets(state, scans, max_attempts=max_attempts)
    # New/recovered notification batches take precedence. A failed scan is a
    # retry only because it was already tied to such an alert Issue; this is
    # never a sweep over historical job history.
    scan_targets = dict(retry_targets)
    scan_targets.update(delivery_targets)
    if not scan_only_new_jobs:
        # This is an explicit environment opt-in for previously alerted jobs,
        # not an automatic history sweep. The default remains prospective.
        scan_targets.update(_previously_alerted_scan_targets(state))
    update_targets = _pending_issue_update_targets(scans)
    update_targets.update({canonical_job_id: issue_number for canonical_job_id, (_, issue_number) in scan_targets.items()})

    inspector: Any | None = None
    inspector_error: Exception | None = None
    attempted: list[str] = []
    persisted: list[str] = []
    updated: list[str] = []
    failed: list[str] = []

    for canonical_job_id in sorted(update_targets):
        issue_number = update_targets[canonical_job_id]
        job_and_issue = scan_targets.get(canonical_job_id)
        record = scans.get(canonical_job_id)

        # A terminal result from an earlier interrupted delivery must not be
        # inspected again. Only fresh alert targets and bounded failed retries
        # reach the inspector.
        should_scan = job_and_issue is not None and (
            not isinstance(record, Mapping)
            or (
                record.get("status") == "failed"
                and isinstance(record.get("attempt_count"), int)
                and record["attempt_count"] < max_attempts
            )
        )

        if should_scan:
            job = job_and_issue[0]
            attempted.append(canonical_job_id)
            previous_record = scans.get(canonical_job_id)
            try:
                if inspector is None and inspector_error is None:
                    try:
                        inspector = (inspector_factory or api.ApplicationInspector)()
                    except Exception as error:
                        inspector_error = error
                if inspector_error is not None:
                    result = _failed_scan_result(api, job, inspector_error)
                else:
                    try:
                        result = inspector.inspect(job)
                    except Exception as error:  # defensive: inspectors should return failed results
                        result = _failed_scan_result(api, job, error)
                new_record = _scan_record_for_result(
                    result,
                    canonical_job_id=canonical_job_id,
                    prior_record=record if isinstance(record, Mapping) else None,
                    issue_number=issue_number,
                )
                scans[canonical_job_id] = new_record
                # This write is intentionally before the remote Issue update.
                _persist_application_scan_state(scan_path, scan_state)
                persisted.append(canonical_job_id)
                record = new_record
                if new_record.get("status") == "failed":
                    failed.append(canonical_job_id)
            except Exception as error:
                # Do not leave a malformed/unsaved record in the shared state:
                # otherwise one broken result could make validation fail for
                # every later job in this aggregate notification batch.
                if previous_record is None:
                    scans.pop(canonical_job_id, None)
                else:
                    scans[canonical_job_id] = previous_record
                LOGGER.error(
                    "Application scan persistence failed for %s; original Issue #%s remains intact (%s): %s",
                    canonical_job_id,
                    issue_number,
                    type(error).__name__,
                    _safe_scan_message(str(error)),
                )
                failed.append(canonical_job_id)
                continue

        if not isinstance(record, Mapping) or record.get("issue_update_pending") is not True:
            continue
        try:
            result = _scan_result_from_record(api, record)
            rendered_block = api.render_application_scan_block(result)
            notifier.update_issue_with_application_scan(issue_number, canonical_job_id, rendered_block)
            # A completed remote update is recorded separately. If this local
            # write fails, marker replacement makes the next update harmless.
            record["issue_update_pending"] = False
            _persist_application_scan_state(scan_path, scan_state)
            updated.append(canonical_job_id)
        except Exception as error:
            LOGGER.error(
                "Application scan Issue update failed for %s on Issue #%s; saved scan will be retried (%s): %s",
                canonical_job_id,
                issue_number,
                type(error).__name__,
                _safe_scan_message(str(error)),
            )
            failed.append(canonical_job_id)

    if attempted:
        LOGGER.info("Application inspection attempted for %s canonical job(s).", len(attempted))
    if updated:
        LOGGER.info("Updated %s alert Issue application-scan section(s).", len(updated))
    return ApplicationEnrichmentResult(
        tuple(attempted), tuple(persisted), tuple(updated), tuple(failed)
    )


def deliver_pending(
    *,
    root: Path = PROJECT_ROOT,
    environment: dict[str, str] | None = None,
    inspector_factory: Callable[[], Any] | None = None,
) -> DeliveryResult | None:
    """Deliver alert Issues first, then optionally enrich those same Issues."""

    environment = environment or dict(os.environ)
    token = environment.get("GITHUB_TOKEN")
    repository = environment.get("GITHUB_REPOSITORY", "LamdaDev/sf-job-tracker")
    if not token:
        LOGGER.warning("GITHUB_TOKEN is not set; pending GitHub Issue notifications were not delivered.")
        return None
    seen_path, _, _ = _state_paths(root)
    state = load_seen_state(seen_path)
    notifier = GitHubIssueNotifier(token, repository, api_url=environment.get("GITHUB_API_URL", "https://api.github.com"))
    result = deliver_pending_notifications(state, notifier)
    if application_scan_enabled(environment):
        # Enrichment is deliberately after delivery. Its failures are caught
        # per job by ``enrich_delivered_job_issues`` and cannot re-pend or
        # suppress the original job-notification batch.
        enrich_delivered_job_issues(
            root=root,
            state=state,
            delivery=result,
            notifier=notifier,
            inspector_factory=inspector_factory,
            scan_only_new_jobs=application_scan_only_new_jobs(environment),
            max_attempts=application_scan_max_attempts(environment),
        )
    else:
        LOGGER.info("Application question enrichment is disabled by APPLICATION_SCAN_ENABLED.")
    changed = write_json_if_changed(seen_path, state)
    if result.delivered_batches:
        LOGGER.info("Created %s GitHub Issue notification batch(es).", len(result.delivered_batches))
    if result.existing_issue_batches:
        LOGGER.info("Marked %s already-created GitHub Issue notification batch(es) as sent.", len(result.existing_issue_batches))
    if result.failed_batches:
        LOGGER.error("%s notification batch(es) remain pending and will be retried.", len(result.failed_batches))
    if changed:
        LOGGER.info("Updated persisted notification delivery state.")
    return result


def send_test_notification(*, environment: dict[str, str] | None = None) -> int:
    """Create a fresh explicit manual test Issue without touching tracker files."""

    environment = environment or dict(os.environ)
    token = environment.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN is required to send a test GitHub Issue notification")
    repository = environment.get("GITHUB_REPOSITORY", "LamdaDev/sf-job-tracker")
    notifier = GitHubIssueNotifier(token, repository, api_url=environment.get("GITHUB_API_URL", "https://api.github.com"))
    issue_number = send_test_issue_notification(notifier)
    LOGGER.info(
        "Created fresh test GitHub Issue #%s. No upstream jobs or tracker files were changed.",
        issue_number,
    )
    return issue_number


def _manual_application_scan_target(application_url: str | None) -> _ManualApplicationScanTarget:
    """Validate a user-supplied test URL without touching tracker state."""

    normalized_url = (application_url or DEFAULT_TEST_APPLICATION_SCAN_URL).strip()
    # Import the lightweight URL guard here instead of importing/starting any
    # provider browser machinery while parsing normal tracker commands.
    from .application_inspection.security import is_safe_public_http_url

    if not is_safe_public_http_url(normalized_url):
        raise ValueError("--test-application-url must be a public http(s) URL")
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:16]
    return _ManualApplicationScanTarget(
        canonical_id=f"manual-test:application:{digest}",
        application_url=normalized_url,
    )


def send_test_application_scan(
    *,
    application_url: str | None = None,
    environment: dict[str, str] | None = None,
    inspector_factory: Callable[[], Any] | None = None,
) -> int:
    """Create and enrich one fresh manual test Issue without any state writes.

    This intentionally does not load tracker history, pending notifications, or
    application-question state. The issue is created before the inspector is
    even constructed, matching production's alert-before-scan safety boundary.
    """

    target = _manual_application_scan_target(application_url)
    environment = environment or dict(os.environ)
    token = environment.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN is required to send a test application-scan Issue")
    repository = environment.get("GITHUB_REPOSITORY", "LamdaDev/sf-job-tracker")
    notifier = GitHubIssueNotifier(
        token,
        repository,
        api_url=environment.get("GITHUB_API_URL", "https://api.github.com"),
    )

    # No marker lookup: every explicit manual test is meant to notify the
    # user, just like the existing fresh notification test mode.
    issue_number = send_test_application_scan_issue_notification(notifier, target.application_url)
    LOGGER.info(
        "Created fresh application-scan test Issue #%s. Tracker files remain untouched.",
        issue_number,
    )

    api = _application_inspection_api()
    try:
        inspector = (inspector_factory or api.ApplicationInspector)()
        result = inspector.inspect(target)
    except Exception as error:  # one visible failed block is safer than suppressing the Issue
        provider = api.detect_application_provider(target.application_url)
        result = api.failed_scan_result(
            target,
            target.application_url,
            provider,
            error,
            stage="running the manual test application inspection",
        )

    rendered_block = api.render_application_scan_block(result)
    # If this PATCH fails, the already-created test Issue remains visible and
    # the workflow fails clearly. There is deliberately no JSON retry state.
    notifier.update_issue_with_application_scan(issue_number, target.canonical_id, rendered_block)
    question_count = len(getattr(result, "questions", ()))
    LOGGER.info(
        "Manual application test completed for %s: provider=%s status=%s questions=%s.",
        target.canonical_id,
        _status_value(getattr(result, "provider", "unknown")),
        _status_value(getattr(result, "status", "failed")),
        question_count,
    )
    return issue_number


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Track {TARGET_LOCATION_LABEL} SWE jobs from public source feeds.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and evaluate live data without writing files or delivering notifications.")
    parser.add_argument("--initialize", action="store_true", help="Record a baseline without alerting for otherwise unseen jobs.")
    parser.add_argument("--deliver-pending", action="store_true", help="Only deliver persisted pending GitHub Issue alerts.")
    parser.add_argument("--send-test-notification", action="store_true", help="Create a fresh, clearly marked test Issue only.")
    parser.add_argument(
        "--send-test-application-scan",
        action="store_true",
        help="Create one fresh Issue, read-only scan a public application URL, and update that same Issue without changing tracker data.",
    )
    parser.add_argument(
        "--test-application-url",
        metavar="URL",
        help="Public direct application URL for --send-test-application-scan (defaults to the configured Replit Ashby example).",
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="Repository root containing data/ and jobs.md.")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")
    exclusive_modes = sum((args.deliver_pending, args.send_test_notification, args.send_test_application_scan))
    if exclusive_modes > 1:
        parser.error("notification-only modes cannot be combined")
    if args.test_application_url and not args.send_test_application_scan:
        parser.error("--test-application-url requires --send-test-application-scan")
    if (args.deliver_pending or args.send_test_notification or args.send_test_application_scan) and (args.dry_run or args.initialize):
        parser.error("notification-only modes cannot be combined with --dry-run or --initialize")
    try:
        if args.deliver_pending:
            result = deliver_pending(root=args.root)
            if result is not None and result.failed_batches:
                return 2
        elif args.send_test_notification:
            send_test_notification()
        elif args.send_test_application_scan:
            send_test_application_scan(application_url=args.test_application_url)
        else:
            run_tracker(root=args.root, dry_run=args.dry_run, initialize=args.initialize)
    except (StorageError, UpstreamFetchError, UpstreamFormatError, GitHubNotificationError, ValueError) as error:
        LOGGER.error("Job tracker failed without updating state: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

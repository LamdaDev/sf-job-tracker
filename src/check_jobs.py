"""CLI orchestration for multi-source collection, state, and notifications."""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .canonical import aggregate_observations
from .config import SOURCES, TARGET_LOCATION_LABEL, SourceConfig
from .fetcher import FetchedSnapshot, UpstreamFetchError, fetch_upstream_sources
from .notifier import (
    DeliveryResult,
    GitHubIssueNotifier,
    GitHubNotificationError,
    deliver_pending_notifications,
    send_test_notification as send_test_issue_notification,
)
from .parser import UpstreamFormatError, parse_configured_source_with_diagnostics
from .renderer import render_jobs_markdown
from .storage import (
    StorageError,
    load_current_state,
    load_seen_state,
    serialise_json,
    write_json_if_changed,
    write_texts_transactionally,
)
from .tracker import StateTransition, apply_current_jobs, location_matches


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def deliver_pending(*, root: Path = PROJECT_ROOT, environment: dict[str, str] | None = None) -> DeliveryResult | None:
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
    """Create one explicit manual test Issue without touching tracker files."""

    environment = environment or dict(os.environ)
    token = environment.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("GITHUB_TOKEN is required to send a test GitHub Issue notification")
    repository = environment.get("GITHUB_REPOSITORY", "LamdaDev/sf-job-tracker")
    notifier = GitHubIssueNotifier(token, repository, api_url=environment.get("GITHUB_API_URL", "https://api.github.com"))
    result = send_test_issue_notification(notifier)
    LOGGER.info(
        "%s test GitHub Issue #%s. No upstream jobs or tracker files were changed.",
        "Created" if result.created else "Reused existing", result.issue_number,
    )
    return result.issue_number


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Track {TARGET_LOCATION_LABEL} SWE jobs from public source feeds.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and evaluate live data without writing files or delivering notifications.")
    parser.add_argument("--initialize", action="store_true", help="Record a baseline without alerting for otherwise unseen jobs.")
    parser.add_argument("--deliver-pending", action="store_true", help="Only deliver persisted pending GitHub Issue alerts.")
    parser.add_argument("--send-test-notification", action="store_true", help="Create one safe, clearly marked test Issue only.")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="Repository root containing data/ and jobs.md.")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")
    exclusive_modes = sum((args.deliver_pending, args.send_test_notification))
    if exclusive_modes > 1:
        parser.error("--deliver-pending and --send-test-notification cannot be combined")
    if (args.deliver_pending or args.send_test_notification) and (args.dry_run or args.initialize):
        parser.error("notification-only modes cannot be combined with --dry-run or --initialize")
    try:
        if args.deliver_pending:
            result = deliver_pending(root=args.root)
            if result is not None and result.failed_batches:
                return 2
        elif args.send_test_notification:
            send_test_notification()
        else:
            run_tracker(root=args.root, dry_run=args.dry_run, initialize=args.initialize)
    except (StorageError, UpstreamFetchError, UpstreamFormatError, GitHubNotificationError, ValueError) as error:
        LOGGER.error("Job tracker failed without updating state: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

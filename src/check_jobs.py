"""CLI orchestration for collecting, persisting, and notifying about jobs."""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import CATEGORIES, SOURCES, TARGET_LOCATION, TARGET_LOCATION_LABEL
from .fetcher import FetchedSnapshot, UpstreamFetchError, fetch_upstream_sources
from .notifier import DeliveryResult, GitHubIssueNotifier, deliver_pending_notifications
from .parser import UpstreamFormatError, parse_source_with_diagnostics
from .renderer import render_jobs_markdown
from .storage import (
    StorageError,
    load_current_state,
    load_seen_state,
    serialise_json,
    write_json_if_changed,
    write_texts_transactionally,
)
from .tracker import StateTransition, deduplicate_jobs, location_matches, apply_current_jobs

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class RunSummary:
    """Useful details from one collection run, including dry runs."""

    snapshot_sha: str
    parsed_counts: dict[str, dict[str, int]]
    matching_count: int
    known_before_count: int
    transition: StateTransition
    files_changed: tuple[Path, ...]
    upstream_duplicate_count: int


def _state_paths(root: Path) -> tuple[Path, Path, Path]:
    return root / "data" / "seen_jobs.json", root / "data" / "current_jobs.json", root / "jobs.md"


def _parse_snapshot(snapshot: FetchedSnapshot) -> tuple[list, dict[str, dict[str, int]]]:
    parsed_jobs = []
    counts: dict[str, dict[str, int]] = {}
    for source in SOURCES:
        try:
            document = snapshot.documents[source]
        except KeyError as error:
            raise UpstreamFetchError(
                f"Fetched snapshot {snapshot.commit_sha} is missing {source.source_file}"
            ) from error
        parsed_source = parse_source_with_diagnostics(document, source)
        source_jobs = list(parsed_source.jobs)
        if not source_jobs:
            raise UpstreamFormatError(
                f"{source.source_file} produced no valid job rows; refusing to update tracker state"
            )
        counts[source.job_type] = {
            category: parsed_source.category_stats[category].parsed_rows for category in CATEGORIES
        }
        parsed_jobs.extend(source_jobs)
    return parsed_jobs, counts


def _log_collection_summary(summary: RunSummary, *, dry_run: bool) -> None:
    LOGGER.info("Fetched SpeedyApply sources successfully at commit %s", summary.snapshot_sha)
    for job_type in ("Internship", "New Grad"):
        LOGGER.info("%s:", job_type)
        for category in CATEGORIES:
            LOGGER.info("  %s: %s parsed", category, summary.parsed_counts[job_type][category])
    LOGGER.info('Matching "%s": %s', TARGET_LOCATION, summary.matching_count)
    LOGGER.info("Known matching jobs before this run: %s", summary.known_before_count)
    if summary.transition.baseline:
        mode = "would establish" if dry_run else "established"
        LOGGER.info("Baseline mode: %s history without alerting.", mode)
    LOGGER.info("New matching jobs: %s", len(summary.transition.new_jobs))
    LOGGER.info("Jobs marked inactive: %s", summary.transition.inactive_count)
    if summary.transition.reactivated_count:
        LOGGER.info("Jobs reactivated without a new alert: %s", summary.transition.reactivated_count)
    if summary.upstream_duplicate_count:
        LOGGER.warning("Collapsed %s duplicate upstream application URL(s)", summary.upstream_duplicate_count)
    for job in summary.transition.new_jobs:
        LOGGER.info("NEW: %s — %s", job.company, job.position)
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
    """Fetch and reconcile the live upstream data without sending notifications."""

    snapshot = snapshot_fetcher()
    parsed_jobs, parsed_counts = _parse_snapshot(snapshot)
    unique_jobs, upstream_duplicate_count = deduplicate_jobs(parsed_jobs)
    matching_jobs = [
        job for job in unique_jobs if location_matches(job.location, TARGET_LOCATION)
    ]

    seen_path, current_path, dashboard_path = _state_paths(root)
    existing_state = load_seen_state(seen_path)
    # Validate the existing current file before writing a replacement. This
    # makes unexpected manual corruption visible instead of silently masking it.
    load_current_state(current_path)
    known_before_count = len(existing_state["jobs"])
    transition = apply_current_jobs(
        existing_state,
        matching_jobs,
        timestamp=timestamp,
        initialize=initialize,
    )

    changed: list[Path] = []
    if not dry_run:
        dashboard = render_jobs_markdown(transition.state)
        changed.extend(
            write_texts_transactionally(
                {
                    # Seen history is listed last so it is the final durable
                    # commit point even on filesystems where rollback fails.
                    current_path: serialise_json(transition.current_state),
                    dashboard_path: dashboard,
                    seen_path: serialise_json(transition.state),
                }
            )
        )

    summary = RunSummary(
        snapshot_sha=snapshot.commit_sha,
        parsed_counts=parsed_counts,
        matching_count=len(matching_jobs),
        known_before_count=known_before_count,
        transition=transition,
        files_changed=tuple(changed),
        upstream_duplicate_count=upstream_duplicate_count,
    )
    _log_collection_summary(summary, dry_run=dry_run)
    return summary


def deliver_pending(
    *, root: Path = PROJECT_ROOT, environment: dict[str, str] | None = None
) -> DeliveryResult | None:
    """Deliver persisted notification batches, leaving failures safely pending."""

    environment = environment or dict(os.environ)
    token = environment.get("GITHUB_TOKEN")
    repository = environment.get("GITHUB_REPOSITORY", "LamdaDev/sf-job-tracker")
    if not token:
        LOGGER.warning("GITHUB_TOKEN is not set; pending GitHub Issue notifications were not delivered.")
        return None

    seen_path, _, _ = _state_paths(root)
    state = load_seen_state(seen_path)
    notifier = GitHubIssueNotifier(
        token,
        repository,
        api_url=environment.get("GITHUB_API_URL", "https://api.github.com"),
    )
    result = deliver_pending_notifications(state, notifier)
    changed = write_json_if_changed(seen_path, state)
    if result.delivered_batches:
        LOGGER.info("Created %s GitHub Issue notification batch(es).", len(result.delivered_batches))
    if result.existing_issue_batches:
        LOGGER.info(
            "Marked %s already-created GitHub Issue notification batch(es) as sent.",
            len(result.existing_issue_batches),
        )
    if result.failed_batches:
        LOGGER.error(
            "%s notification batch(es) remain pending and will be retried.", len(result.failed_batches)
        )
    if changed:
        LOGGER.info("Updated persisted notification delivery state.")
    return result


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=f"Track {TARGET_LOCATION_LABEL} SWE jobs from SpeedyApply's public USA Markdown lists."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and evaluate live data without changing files or delivering notifications.",
    )
    parser.add_argument(
        "--initialize",
        action="store_true",
        help="Record a baseline without creating alerts for otherwise unseen URLs.",
    )
    parser.add_argument(
        "--deliver-pending",
        action="store_true",
        help="Only deliver persisted pending GitHub Issue alerts; do not fetch upstream data.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repository root containing data/ and jobs.md (default: this project).",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Console logging level (default: INFO).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")
    if args.deliver_pending and (args.dry_run or args.initialize):
        parser.error("--deliver-pending cannot be combined with --dry-run or --initialize")

    try:
        if args.deliver_pending:
            result = deliver_pending(root=args.root)
            if result is not None and result.failed_batches:
                # Delivery state for any successful batches was written before
                # reporting failure, so the workflow can commit it and retry
                # only the remaining pending batches next time.
                return 2
        else:
            run_tracker(root=args.root, dry_run=args.dry_run, initialize=args.initialize)
    except (StorageError, UpstreamFetchError, UpstreamFormatError, ValueError) as error:
        LOGGER.error("Job tracker failed without updating state: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

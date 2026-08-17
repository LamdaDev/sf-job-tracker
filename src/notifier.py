"""GitHub Issue notification formatting and retry-safe REST delivery."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .canonical import aggregate_observations
from .config import TARGET_LOCATION_LABEL
from .models import CanonicalJob, Job
from .tracker import notification_batch_id


LOGGER = logging.getLogger(__name__)
TRACKER_BATCH_MARKER_PREFIX = "<!-- sf-job-tracker:batch:v1:"


class GitHubNotificationError(RuntimeError):
    """Raised for GitHub API failures that should leave a batch pending."""


@dataclass(frozen=True)
class DeliveryResult:
    delivered_batches: tuple[str, ...]
    failed_batches: tuple[str, ...]
    existing_issue_batches: tuple[str, ...]
    # Each batch successfully resolved to one aggregate Issue.  Keeping this
    # alongside the existing delivery result lets optional enrichment happen
    # strictly *after* the alert has survived creation/retry handling.
    batch_issue_numbers: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class ExpiredIssueCloseResult:
    """Outcome of closing aged tracker job-alert Issues."""

    closed_issue_numbers: tuple[int, ...]
    failed_issue_numbers: tuple[int, ...]


def issue_marker(batch_id: str) -> str:
    return f"<!-- sf-job-tracker:batch:v1:{batch_id} -->"


def _is_tracker_job_alert_issue(issue: Mapping[str, Any]) -> bool:
    """Recognize only normal job-alert Issues, never tests or user Issues."""

    return (
        "pull_request" not in issue
        and isinstance(issue.get("body"), str)
        and TRACKER_BATCH_MARKER_PREFIX in issue["body"]
    )


def _github_timestamp(value: Any) -> datetime | None:
    """Parse GitHub's UTC Issue timestamp conservatively."""

    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def application_scan_markers(canonical_job_id: str) -> tuple[str, str]:
    """Return per-canonical-job boundaries within an aggregate alert Issue."""

    return (
        f"<!-- application-scan:start canonical-id={canonical_job_id} -->",
        f"<!-- application-scan:end canonical-id={canonical_job_id} -->",
    )


def replace_application_scan_block(
    issue_body: str | None, canonical_job_id: str, rendered_block: str
) -> str:
    """Append or replace exactly one enrichment section for a canonical job.

    Job alerts can contain several canonical jobs.  Explicit, job-specific
    markers avoid a retry appending duplicate question sections while leaving
    the original alert text untouched.
    """

    start, end = application_scan_markers(canonical_job_id)
    visible_block = rendered_block.strip()
    replacement = "\n".join((start, visible_block, end))
    body = issue_body or ""
    # Accept the compact form used in early design examples too, so a future
    # marker spelling migration cannot append a second block on retry.
    marker_pairs = (
        (start, end),
        (
            f"<!-- application-scan:start:{canonical_job_id} -->",
            f"<!-- application-scan:end:{canonical_job_id} -->",
        ),
    )
    for candidate_start, candidate_end in marker_pairs:
        pattern = re.compile(rf"{re.escape(candidate_start)}.*?{re.escape(candidate_end)}", re.DOTALL)
        if pattern.search(body):
            return pattern.sub(replacement, body, count=1)
    separator = "" if not body or body.endswith("\n") else "\n"
    return f"{body}{separator}\n{replacement}\n"


TEST_NOTIFICATION_MARKER = "<!-- sf-job-tracker:test-notification:v1 -->"
TEST_APPLICATION_SCAN_MARKER = "<!-- sf-job-tracker:test-application-scan:v1 -->"


def test_issue_title() -> str:
    return f"\U0001f9ea TEST — {TARGET_LOCATION_LABEL} job tracker notification"


def build_test_issue_body() -> str:
    return "\n".join(
        [
            "# \U0001f9ea Test notification",
            "",
            "This is a manually requested notification test from `sf-job-tracker`.",
            "",
            "It is **not** a job alert and did not fetch jobs or change tracker history, "
            "the current-job snapshot, or the dashboard.",
            "",
            "Every manual test run deliberately creates a fresh Issue so it can trigger a "
            "new email or GitHub Mobile notification.",
            "",
            TEST_NOTIFICATION_MARKER,
            "",
        ]
    )


def test_application_scan_issue_title() -> str:
    """Return the title for a deliberately fresh enrichment-test Issue."""

    return "\U0001f9ea TEST \u2014 Application question enrichment"


def build_test_application_scan_issue_body(application_url: str) -> str:
    """Build a state-free placeholder that will receive one scan block.

    The actual result is inserted only after the Issue exists. That ordering
    mirrors production delivery and proves an inspection failure can never
    suppress the notification itself.
    """

    safe_url = quote(application_url, safe=":/?&=#%+-._~")
    return "\n".join(
        [
            "# \U0001f9ea Test application-question enrichment",
            "",
            "This is a manually requested, fresh test Issue from `sf-job-tracker`.",
            "",
            f"**Test application:** [Open public application](<{safe_url}>)",
            "",
            "The Issue is created first and is then enriched with the visible public",
            "application questions, if the site permits read-only inspection.",
            "",
            "It does **not** fetch tracker feeds or change tracker history, generated",
            "jobs, pending notifications, or application-question state.",
            "",
            "If the site shows login, CAPTCHA, or anti-bot verification, the result",
            "will say so rather than attempting to bypass it.",
            "",
            TEST_APPLICATION_SCAN_MARKER,
            "",
        ]
    )


def issue_title(job_count: int) -> str:
    suffix = "job" if job_count == 1 else "jobs"
    return f"\U0001f6a8 {job_count} new {TARGET_LOCATION_LABEL} SWE {suffix}"


def _job_sort_key(job: CanonicalJob) -> tuple[str, str, str]:
    return (job.company.casefold(), job.position.casefold(), job.canonical_id)


def _canonical_jobs(jobs: Iterable[CanonicalJob | Job]) -> list[CanonicalJob]:
    values = list(jobs)
    if all(isinstance(job, CanonicalJob) for job in values):
        return [job for job in values if isinstance(job, CanonicalJob)]
    if all(isinstance(job, Job) for job in values):
        return aggregate_observations(job for job in values if isinstance(job, Job))[0]
    raise ValueError("Issue jobs must be canonical jobs or normalized observations")


def _source_labels(job: CanonicalJob) -> str:
    labels = {observation.source_label for observation in job.observations if observation.source_label}
    return ", ".join(sorted(labels)) or "Unknown"


def build_issue_body(jobs: Iterable[CanonicalJob | Job], batch_id: str | None = None) -> str:
    """Build a canonical multi-source alert plus a deterministic hidden marker."""

    ordered = sorted(_canonical_jobs(jobs), key=_job_sort_key)
    if not ordered:
        raise ValueError("Cannot build an Issue notification with no jobs")
    resolved_batch_id = batch_id or notification_batch_id(job.canonical_id for job in ordered)
    blocks = [f"# New {TARGET_LOCATION_LABEL} SWE Postings", ""]
    for job in ordered:
        blocks.extend(
            [
                f"## {job.company}",
                "",
                f"**{job.position}**",
                "",
                f"- **Type:** {job.job_type}",
                f"- **Category:** {job.category}",
                f"- **Location:** {job.location}",
                f"- **Sources:** {_source_labels(job)}",
                f"- **Salary:** {job.salary or 'N/A'}",
                f"- **Age:** {job.age or 'N/A'}",
                *([f"- **Posted:** {job.posted}"] if job.posted else []),
                "",
                f"### [Apply to {job.company} →](<{job.application_url}>)",
                "",
                "---",
                "",
            ]
        )
    blocks.extend(["Detected automatically by sf-job-tracker.", "", issue_marker(resolved_batch_id), ""])
    return "\n".join(blocks)


class GitHubIssueNotifier:
    """Small GitHub REST client kept independent of tracker state mutations."""

    def __init__(
        self,
        token: str,
        repository: str,
        *,
        api_url: str = "https://api.github.com",
        timeout_seconds: int = 30,
    ) -> None:
        if not token:
            raise ValueError("A GitHub token is required to create Issues")
        if "/" not in repository:
            raise ValueError("GitHub repository must be in owner/name form")
        self.token = token
        self.repository = repository
        self.api_url = api_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _request_json(
        self, method: str, url_or_path: str, payload: Mapping[str, Any] | None = None
    ) -> tuple[Any, Mapping[str, str]]:
        url = url_or_path if url_or_path.startswith("http") else f"{self.api_url}{url_or_path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "sf-job-tracker",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310
                status = response.getcode()
                raw_body = response.read()
                if status < 200 or status >= 300:
                    raise GitHubNotificationError(f"GitHub API returned HTTP {status} for {method} {url}")
                if not raw_body:
                    return None, dict(response.headers.items())
                return json.loads(raw_body.decode("utf-8")), dict(response.headers.items())
        except HTTPError as error:
            raise GitHubNotificationError(f"GitHub API returned HTTP {error.code} for {method} {url}") from error
        except (URLError, OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubNotificationError(f"GitHub API request failed for {method} {url}: {error}") from error

    @staticmethod
    def _next_link(headers: Mapping[str, str]) -> str | None:
        link_header = next((value for key, value in headers.items() if key.casefold() == "link"), "")
        match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
        return match.group(1) if match else None

    def _iter_issues(self, *, state: str) -> Iterable[Mapping[str, Any]]:
        """Yield repository Issues across every GitHub API page."""

        url: str | None = f"/repos/{self.repository}/issues?state={state}&per_page=100"
        while url:
            payload, headers = self._request_json("GET", url)
            if not isinstance(payload, list):
                raise GitHubNotificationError("GitHub Issues API returned a non-list response")
            for issue in payload:
                if isinstance(issue, Mapping):
                    yield issue
            url = self._next_link(headers)

    def iter_open_issues(self) -> Iterable[Mapping[str, Any]]:
        """Yield open Issues only; pull requests remain visible to callers to filter."""

        return self._iter_issues(state="open")

    def find_issue_with_marker(self, marker: str) -> int | None:
        for issue in self._iter_issues(state="all"):
            if "pull_request" in issue:
                continue
            if marker in str(issue.get("body") or "") and isinstance(issue.get("number"), int):
                return issue["number"]
        return None

    def find_issue_for_batch(self, batch_id: str) -> int | None:
        return self.find_issue_with_marker(issue_marker(batch_id))

    def _create_issue(self, *, title: str, body: str) -> int:
        response, _ = self._request_json("POST", f"/repos/{self.repository}/issues", {"title": title, "body": body})
        if not isinstance(response, dict) or not isinstance(response.get("number"), int):
            raise GitHubNotificationError("GitHub Issue creation returned no issue number")
        return response["number"]

    def get_issue_body(self, issue_number: int) -> str:
        """Read the latest Issue body before replacing one scan section."""

        if issue_number < 1:
            raise ValueError("GitHub Issue number must be positive")
        response, _ = self._request_json("GET", f"/repos/{self.repository}/issues/{issue_number}")
        if not isinstance(response, dict):
            raise GitHubNotificationError("GitHub Issue lookup returned no Issue object")
        body = response.get("body")
        if body is None:
            return ""
        if not isinstance(body, str):
            raise GitHubNotificationError("GitHub Issue lookup returned an invalid body")
        return body

    def update_issue_body(self, issue_number: int, body: str) -> None:
        """Update an Issue body without changing its title, labels, or state."""

        if issue_number < 1:
            raise ValueError("GitHub Issue number must be positive")
        response, _ = self._request_json(
            "PATCH", f"/repos/{self.repository}/issues/{issue_number}", {"body": body}
        )
        if not isinstance(response, dict):
            raise GitHubNotificationError("GitHub Issue update returned no Issue object")

    def close_issue(self, issue_number: int) -> None:
        """Close an Issue without changing its body or deleting its history."""

        if issue_number < 1:
            raise ValueError("GitHub Issue number must be positive")
        response, _ = self._request_json(
            "PATCH", f"/repos/{self.repository}/issues/{issue_number}", {"state": "closed"}
        )
        if not isinstance(response, dict):
            raise GitHubNotificationError("GitHub Issue close returned no Issue object")

    def update_issue_with_application_scan(
        self, issue_number: int, canonical_job_id: str, rendered_block: str
    ) -> bool:
        """Idempotently insert one scan block into the existing alert Issue.

        Returns whether a PATCH was necessary.  Reading first avoids needless
        API writes when a prior run completed the update but failed before its
        local state could record that fact.
        """

        current_body = self.get_issue_body(issue_number)
        updated_body = replace_application_scan_block(current_body, canonical_job_id, rendered_block)
        if updated_body == current_body:
            return False
        self.update_issue_body(issue_number, updated_body)
        return True

    def create_issue(self, jobs: Iterable[CanonicalJob | Job], batch_id: str) -> int:
        ordered = _canonical_jobs(jobs)
        issue_number = self._create_issue(title=issue_title(len(ordered)), body=build_issue_body(ordered, batch_id))
        try:
            self._request_json("POST", f"/repos/{self.repository}/issues/{issue_number}/labels", {"labels": ["new-job"]})
        except GitHubNotificationError as error:
            LOGGER.warning("Created Issue #%s but could not apply optional new-job label: %s", issue_number, error)
        return issue_number

    def create_test_issue(self) -> int:
        return self._create_issue(title=test_issue_title(), body=build_test_issue_body())

    def create_test_application_scan_issue(self, application_url: str) -> int:
        """Create a fresh manual scan-test Issue without marker lookup.

        Unlike production batch alerts, each manual invocation intentionally
        creates another Issue so it can exercise a user's email or mobile
        notification delivery.
        """

        return self._create_issue(
            title=test_application_scan_issue_title(),
            body=build_test_application_scan_issue_body(application_url),
        )


def send_test_notification(notifier: GitHubIssueNotifier) -> int:
    """Create a fresh, state-free manual test Issue on every invocation.

    Production job-alert batches remain idempotent through their hidden batch
    markers. The manual test is intentionally different: a new Issue is the
    event that exercises a user's email and mobile notification delivery.
    """

    return notifier.create_test_issue()


def send_test_application_scan_issue(notifier: GitHubIssueNotifier, application_url: str) -> int:
    """Create one fresh, state-free Issue for a manual application scan test."""

    return notifier.create_test_application_scan_issue(application_url)


def close_expired_tracker_issues(
    notifier: Any,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> ExpiredIssueCloseResult:
    """Close only aged, open, tracker-generated job-alert Issues.

    The cutoff uses GitHub's immutable Issue ``created_at`` timestamp rather
    than recent comments or application-question enrichment updates. Marker
    matching protects manual test Issues and every Issue not created by this
    tracker. Closing is deliberate and reversible; no Issue is deleted.
    """

    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        raise ValueError("now must include a timezone")
    cutoff = reference_time.astimezone(timezone.utc) - timedelta(days=retention_days)
    closed: list[int] = []
    failed: list[int] = []

    for issue in notifier.iter_open_issues():
        if not isinstance(issue, Mapping) or not _is_tracker_job_alert_issue(issue):
            continue
        issue_number = issue.get("number")
        created_at = _github_timestamp(issue.get("created_at"))
        if not isinstance(issue_number, int) or issue_number < 1:
            LOGGER.warning("Skipping tracker Issue with an invalid GitHub Issue number.")
            continue
        if created_at is None:
            LOGGER.warning("Skipping tracker Issue #%s with an invalid created_at timestamp.", issue_number)
            continue
        # The exact 21-day boundary is eligible; the hourly scheduler closes
        # it on the first production run at or after that moment.
        if created_at > cutoff:
            continue
        try:
            notifier.close_issue(issue_number)
            closed.append(issue_number)
        except (GitHubNotificationError, ValueError) as error:
            LOGGER.error("Could not close expired tracker Issue #%s: %s", issue_number, error)
            failed.append(issue_number)

    return ExpiredIssueCloseResult(tuple(closed), tuple(failed))


def _jobs_for_batch(history: Mapping[str, Any], batch: Mapping[str, Any]) -> list[CanonicalJob]:
    job_ids = batch.get("job_ids")
    if isinstance(job_ids, list) and all(isinstance(item, str) for item in job_ids):
        return [CanonicalJob.from_mapping(job_id, history[job_id]) for job_id in job_ids]
    urls = batch.get("job_urls")
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        raise ValueError("batch has no valid job IDs or URLs")
    # Schema-v1 batches intentionally retain URL hashes so an old hidden Issue
    # marker remains valid after canonical-state migration.
    jobs: list[CanonicalJob] = []
    for url in urls:
        match = next(
            (
                (canonical_id, record)
                for canonical_id, record in history.items()
                if isinstance(record, Mapping)
                and (record.get("application_url") == url or url in record.get("url_aliases", []))
            ),
            None,
        )
        if match is None:
            raise KeyError(url)
        jobs.append(CanonicalJob.from_mapping(*match))
    return jobs


def jobs_for_notification_batch(history: Mapping[str, Any], batch: Mapping[str, Any]) -> list[CanonicalJob]:
    """Resolve canonical jobs for a persisted batch without exposing internals."""

    return _jobs_for_batch(history, batch)


def deliver_pending_notifications(state: dict[str, Any], notifier: GitHubIssueNotifier) -> DeliveryResult:
    """Attempt every pending batch and retain failures for a later retry."""

    pending = state.get("pending_notifications")
    history = state.get("jobs")
    if not isinstance(pending, dict) or not isinstance(history, dict):
        raise ValueError("State does not contain valid pending notifications and job history")
    delivered: list[str] = []
    failed: list[str] = []
    existing: list[str] = []
    issue_numbers: dict[str, int] = {}
    ordered_batches = sorted(pending.items(), key=lambda item: (str(item[1].get("created_at", "")), item[0]))
    for batch_id, batch in ordered_batches:
        if not isinstance(batch, dict) or batch.get("status") == "sent":
            continue
        try:
            jobs = _jobs_for_batch(history, batch)
            issue_number = notifier.find_issue_for_batch(batch_id)
            if issue_number is not None:
                existing.append(batch_id)
            else:
                issue_number = notifier.create_issue(jobs, batch_id)
                delivered.append(batch_id)
            batch["issue_number"] = issue_number
            batch["status"] = "sent"
            issue_numbers[batch_id] = issue_number
        except (GitHubNotificationError, KeyError, ValueError) as error:
            LOGGER.error("Notification batch %s remains pending: %s", batch_id, error)
            failed.append(batch_id)
    return DeliveryResult(
        tuple(delivered),
        tuple(failed),
        tuple(existing),
        tuple(sorted(issue_numbers.items())),
    )

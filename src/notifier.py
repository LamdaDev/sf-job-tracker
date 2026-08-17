"""GitHub Issue notification formatting and retry-safe REST delivery."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import TARGET_LOCATION_LABEL
from .models import Job
from .tracker import notification_batch_id

LOGGER = logging.getLogger(__name__)


class GitHubNotificationError(RuntimeError):
    """Raised for GitHub API failures that should leave a batch pending."""


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of attempting every pending notification batch once."""

    delivered_batches: tuple[str, ...]
    failed_batches: tuple[str, ...]
    existing_issue_batches: tuple[str, ...]


@dataclass(frozen=True)
class ManualTestNotificationResult:
    """Outcome of a manually requested, state-free notification test."""

    issue_number: int
    created: bool


def issue_marker(batch_id: str) -> str:
    """Return the hidden marker used to make retries idempotent."""

    return f"<!-- sf-job-tracker:batch:v1:{batch_id} -->"


TEST_NOTIFICATION_MARKER = "<!-- sf-job-tracker:test-notification:v1 -->"


def test_issue_title() -> str:
    """Return an unmistakable title that cannot be confused with a job alert."""

    return f"🧪 TEST — {TARGET_LOCATION_LABEL} job tracker notification"


def build_test_issue_body() -> str:
    """Build the manual test Issue without any fake job or stateful side effect."""

    return "\n".join(
        [
            "# 🧪 Test notification",
            "",
            "This is a manually requested notification test from `sf-job-tracker`.",
            "",
            "It is **not** a job alert and did not fetch jobs or change tracker history, "
            "the current-job snapshot, or the dashboard.",
            "",
            "If you receive this Issue by email or GitHub Mobile, your notification setup is working.",
            "",
            TEST_NOTIFICATION_MARKER,
            "",
        ]
    )


def issue_title(job_count: int) -> str:
    """Return a compact singular/plural alert title."""

    suffix = "job" if job_count == 1 else "jobs"
    return f"🚨 {job_count} new {TARGET_LOCATION_LABEL} SWE {suffix}"


def _job_sort_key(job: Job) -> tuple[str, str, str]:
    return (job.company.casefold(), job.position.casefold(), job.application_url)


def build_issue_body(jobs: Iterable[Job], batch_id: str | None = None) -> str:
    """Build a readable Issue body plus a deterministic hidden batch marker."""

    ordered = sorted(jobs, key=_job_sort_key)
    if not ordered:
        raise ValueError("Cannot build an Issue notification with no jobs")
    resolved_batch_id = batch_id or notification_batch_id(job.application_url for job in ordered)
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
                f"- **Salary:** {job.salary or 'N/A'}",
                f"- **Age:** {job.age or 'N/A'}",
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
        self,
        method: str,
        url_or_path: str,
        payload: Mapping[str, Any] | None = None,
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
            raise GitHubNotificationError(
                f"GitHub API returned HTTP {error.code} for {method} {url}"
            ) from error
        except (URLError, OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubNotificationError(f"GitHub API request failed for {method} {url}: {error}") from error

    @staticmethod
    def _next_link(headers: Mapping[str, str]) -> str | None:
        link_header = next((value for key, value in headers.items() if key.casefold() == "link"), "")
        match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
        return match.group(1) if match else None

    def find_issue_with_marker(self, marker: str) -> int | None:
        """Scan open and closed issues for a deterministic idempotency marker."""

        url: str | None = f"/repos/{self.repository}/issues?state=all&per_page=100"
        while url:
            payload, headers = self._request_json("GET", url)
            if not isinstance(payload, list):
                raise GitHubNotificationError("GitHub Issues API returned a non-list response")
            for issue in payload:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                if marker in str(issue.get("body") or ""):
                    number = issue.get("number")
                    if isinstance(number, int):
                        return number
            url = self._next_link(headers)
        return None

    def find_issue_for_batch(self, batch_id: str) -> int | None:
        """Find a prior real-job alert for a retry-safe notification batch."""

        return self.find_issue_with_marker(issue_marker(batch_id))

    def _create_issue(self, *, title: str, body: str) -> int:
        """Create one Issue and return its number without applying any labels."""

        response, _ = self._request_json(
            "POST", f"/repos/{self.repository}/issues", {"title": title, "body": body}
        )
        if not isinstance(response, dict) or not isinstance(response.get("number"), int):
            raise GitHubNotificationError("GitHub Issue creation returned no issue number")
        return response["number"]

    def create_issue(self, jobs: Iterable[Job], batch_id: str) -> int:
        """Create a single alert Issue. Labeling is deliberately best-effort."""

        ordered = list(jobs)
        issue_number = self._create_issue(
            title=issue_title(len(ordered)), body=build_issue_body(ordered, batch_id)
        )
        try:
            self._request_json(
                "POST",
                f"/repos/{self.repository}/issues/{issue_number}/labels",
                {"labels": ["new-job"]},
            )
        except GitHubNotificationError as error:
            LOGGER.warning("Created Issue #%s but could not apply optional new-job label: %s", issue_number, error)
        return issue_number

    def create_test_issue(self) -> int:
        """Create the explicitly manual test Issue, without a job label."""

        return self._create_issue(title=test_issue_title(), body=build_test_issue_body())


def send_test_notification(notifier: GitHubIssueNotifier) -> ManualTestNotificationResult:
    """Create at most one manual test Issue without reading or changing tracker state."""

    existing_issue = notifier.find_issue_with_marker(TEST_NOTIFICATION_MARKER)
    if existing_issue is not None:
        return ManualTestNotificationResult(issue_number=existing_issue, created=False)
    return ManualTestNotificationResult(issue_number=notifier.create_test_issue(), created=True)


def deliver_pending_notifications(
    state: dict[str, Any], notifier: GitHubIssueNotifier
) -> DeliveryResult:
    """Attempt every pending batch and retain failed ones for a later retry."""

    pending = state.get("pending_notifications")
    history = state.get("jobs")
    if not isinstance(pending, dict) or not isinstance(history, dict):
        raise ValueError("State does not contain valid pending notifications and job history")

    delivered: list[str] = []
    failed: list[str] = []
    existing: list[str] = []
    ordered_batches = sorted(
        pending.items(), key=lambda item: (str(item[1].get("created_at", "")), item[0])
    )
    for batch_id, batch in ordered_batches:
        if not isinstance(batch, dict) or batch.get("status") == "sent":
            continue
        urls = batch.get("job_urls")
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            LOGGER.error("Notification batch %s has invalid URLs; leaving it pending", batch_id)
            failed.append(batch_id)
            continue
        try:
            jobs = [Job.from_mapping(history[url]) for url in urls]
        except (KeyError, ValueError) as error:
            LOGGER.error("Notification batch %s cannot be rendered: %s", batch_id, error)
            failed.append(batch_id)
            continue

        try:
            issue_number = notifier.find_issue_for_batch(batch_id)
            if issue_number is not None:
                existing.append(batch_id)
            else:
                issue_number = notifier.create_issue(jobs, batch_id)
                delivered.append(batch_id)
            batch["issue_number"] = issue_number
            batch["status"] = "sent"
        except GitHubNotificationError as error:
            LOGGER.error("Notification batch %s remains pending: %s", batch_id, error)
            failed.append(batch_id)

    return DeliveryResult(
        delivered_batches=tuple(delivered),
        failed_batches=tuple(failed),
        existing_issue_batches=tuple(existing),
    )

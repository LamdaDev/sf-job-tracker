from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import pytest

import src.check_jobs as check_jobs
from src.config import job_alert_issue_retention_days
from src.notifier import (
    ExpiredIssueCloseResult,
    GitHubIssueNotifier,
    GitHubNotificationError,
    TEST_APPLICATION_SCAN_MARKER,
    TEST_NOTIFICATION_MARKER,
    close_expired_tracker_issues,
    issue_marker,
)


NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


class RetentionNotifier:
    def __init__(
        self,
        issues: Iterable[Mapping[str, Any]],
        *,
        fail_numbers: set[int] | None = None,
    ) -> None:
        self.issues = tuple(issues)
        self.fail_numbers = fail_numbers or set()
        self.closed: list[int] = []

    def iter_open_issues(self) -> Iterable[Mapping[str, Any]]:
        return self.issues

    def close_issue(self, issue_number: int) -> None:
        if issue_number in self.fail_numbers:
            raise GitHubNotificationError("temporary GitHub failure")
        self.closed.append(issue_number)


def _issue(
    number: int,
    created_at: str,
    body: str,
    **extra: Any,
) -> dict[str, Any]:
    return {"number": number, "created_at": created_at, "body": body, **extra}


def test_retention_closes_only_tracker_alerts_at_the_exact_twenty_one_day_boundary() -> None:
    notifier = RetentionNotifier(
        [
            _issue(1, "2026-07-27T12:00:00Z", issue_marker("exact")),
            _issue(2, "2026-07-27T11:59:59Z", issue_marker("older")),
            _issue(3, "2026-07-27T12:00:01Z", issue_marker("newer")),
            _issue(4, "2026-07-01T00:00:00Z", TEST_NOTIFICATION_MARKER),
            _issue(5, "2026-07-01T00:00:00Z", TEST_APPLICATION_SCAN_MARKER),
            _issue(6, "2026-07-01T00:00:00Z", "# A user-created issue"),
            _issue(7, "2026-07-01T00:00:00Z", issue_marker("pull-request"), pull_request={}),
            _issue(8, "not-a-timestamp", issue_marker("malformed")),
        ]
    )

    result = close_expired_tracker_issues(notifier, retention_days=21, now=NOW)

    assert result == ExpiredIssueCloseResult((1, 2), ())
    assert notifier.closed == [1, 2]


def test_retention_continues_after_one_close_failure_for_later_issues() -> None:
    notifier = RetentionNotifier(
        [
            _issue(10, "2026-07-01T00:00:00Z", issue_marker("failed")),
            _issue(11, "2026-07-01T00:00:00Z", issue_marker("succeeds")),
        ],
        fail_numbers={10},
    )

    result = close_expired_tracker_issues(notifier, retention_days=21, now=NOW)

    assert result.closed_issue_numbers == (11,)
    assert result.failed_issue_numbers == (10,)
    assert notifier.closed == [11]


def test_retention_rejects_invalid_age_configuration_and_naive_clock() -> None:
    notifier = RetentionNotifier(())
    with pytest.raises(ValueError, match="retention_days"):
        close_expired_tracker_issues(notifier, retention_days=0, now=NOW)
    with pytest.raises(ValueError, match="timezone"):
        close_expired_tracker_issues(
            notifier,
            retention_days=21,
            now=datetime(2026, 8, 17, 12, 0),
        )


def test_retention_config_defaults_to_three_weeks_and_rejects_bad_values() -> None:
    assert job_alert_issue_retention_days({}) == 21
    assert job_alert_issue_retention_days({"JOB_ALERT_ISSUE_RETENTION_DAYS": "30"}) == 30
    assert job_alert_issue_retention_days({"JOB_ALERT_ISSUE_RETENTION_DAYS": "0"}) == 21
    assert job_alert_issue_retention_days({"JOB_ALERT_ISSUE_RETENTION_DAYS": "not-a-number"}) == 21


def test_notifier_paginates_open_issues_and_closes_through_the_github_api() -> None:
    first_path = "/repos/LamdaDev/sf-job-tracker/issues?state=open&per_page=100"
    second_url = "https://api.github.com/repos/LamdaDev/sf-job-tracker/issues?state=open&page=2"
    calls: list[tuple[str, str, Mapping[str, Any] | None]] = []

    class PaginatedNotifier(GitHubIssueNotifier):
        def _request_json(
            self, method: str, url_or_path: str, payload: Mapping[str, Any] | None = None
        ) -> tuple[Any, Mapping[str, str]]:
            calls.append((method, url_or_path, payload))
            if method == "GET" and url_or_path == first_path:
                return ([{"number": 1}], {"Link": f'<{second_url}>; rel="next"'})
            if method == "GET" and url_or_path == second_url:
                return ([{"number": 2}], {})
            if method == "PATCH" and url_or_path.endswith("/issues/2"):
                return ({"number": 2, "state": "closed"}, {})
            raise AssertionError((method, url_or_path, payload))

    notifier = PaginatedNotifier("not-a-real-token", "LamdaDev/sf-job-tracker")

    assert [issue["number"] for issue in notifier.iter_open_issues()] == [1, 2]
    notifier.close_issue(2)
    assert calls[-1] == ("PATCH", "/repos/LamdaDev/sf-job-tracker/issues/2", {"state": "closed"})


def test_cleanup_cli_path_does_not_collect_jobs_or_write_tracker_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        check_jobs,
        "close_expired_issues",
        lambda: calls.append("cleanup") or ExpiredIssueCloseResult((), ()),
    )
    monkeypatch.setattr(
        check_jobs,
        "run_tracker",
        lambda **_: pytest.fail("cleanup mode must not collect jobs"),
    )
    monkeypatch.setattr(
        check_jobs,
        "deliver_pending",
        lambda **_: pytest.fail("cleanup mode must not deliver alerts"),
    )

    assert check_jobs.main(["--close-expired-issues"]) == 0
    assert calls == ["cleanup"]


def test_cleanup_cli_returns_nonzero_when_a_close_remains_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        check_jobs,
        "close_expired_issues",
        lambda: ExpiredIssueCloseResult((), (99,)),
    )

    assert check_jobs.main(["--close-expired-issues"]) == 2

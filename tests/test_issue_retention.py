from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import pytest

import src.check_jobs as check_jobs
from src.config import job_alert_issue_retention_days
from src.notifier import (
    ExpiredIssueDoneResult,
    GitHubIssueNotifier,
    GitHubNotificationError,
    GitHubProjectNotifier,
    PROJECT_ITEMS_QUERY,
    ProjectStatusUpdateResult,
    TEST_APPLICATION_SCAN_MARKER,
    TEST_NOTIFICATION_MARKER,
    UPDATE_PROJECT_STATUS_MUTATION,
    issue_marker,
    move_expired_tracker_issues_to_done,
)


NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


class IssueFinder:
    def __init__(self, issues: Iterable[Mapping[str, Any]]) -> None:
        self.issues = tuple(issues)

    def iter_open_issues(self) -> Iterable[Mapping[str, Any]]:
        return self.issues


class ProjectMover:
    def __init__(
        self,
        outcomes: Mapping[int, ProjectStatusUpdateResult],
        *,
        fail_numbers: set[int] | None = None,
    ) -> None:
        self.outcomes = dict(outcomes)
        self.fail_numbers = fail_numbers or set()
        self.requested: list[int] = []

    def move_issue_to_done(self, issue_number: int) -> ProjectStatusUpdateResult:
        self.requested.append(issue_number)
        if issue_number in self.fail_numbers:
            raise GitHubNotificationError("temporary GitHub failure")
        return self.outcomes[issue_number]


def _issue(
    number: int,
    created_at: str,
    body: str,
    **extra: Any,
) -> dict[str, Any]:
    return {"number": number, "created_at": created_at, "body": body, **extra}


def _project_item(
    *,
    item_id: str,
    project_id: str,
    project_title: str,
    status: str | None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "fieldValueByName": None if status is None else {"name": status},
        "project": {
            "id": project_id,
            "title": project_title,
            "fields": {
                "nodes": [
                    {
                        "id": f"status-{project_id}",
                        "name": "Status",
                        "options": [
                            {"id": f"todo-{project_id}", "name": "Todo"},
                            {"id": f"done-{project_id}", "name": "Done"},
                        ],
                    }
                ],
                "pageInfo": {"hasNextPage": False},
            },
        },
    }


def test_retention_moves_only_tracker_alerts_at_the_exact_twenty_one_day_boundary() -> None:
    issue_finder = IssueFinder(
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
    project_mover = ProjectMover(
        {
            1: ProjectStatusUpdateResult(("Job search",), ()),
            2: ProjectStatusUpdateResult((), ("Job search",)),
        }
    )

    result = move_expired_tracker_issues_to_done(
        issue_finder,
        project_mover,
        retention_days=21,
        now=NOW,
    )

    assert result == ExpiredIssueDoneResult((1,), (2,), (), ())
    assert project_mover.requested == [1, 2]


def test_retention_reports_unlinked_items_and_continues_after_one_failure() -> None:
    issue_finder = IssueFinder(
        [
            _issue(10, "2026-07-01T00:00:00Z", issue_marker("failed")),
            _issue(11, "2026-07-01T00:00:00Z", issue_marker("unlinked")),
            _issue(12, "2026-07-01T00:00:00Z", issue_marker("succeeds")),
        ]
    )
    project_mover = ProjectMover(
        {
            11: ProjectStatusUpdateResult((), ()),
            12: ProjectStatusUpdateResult(("Job search",), ()),
        },
        fail_numbers={10},
    )

    result = move_expired_tracker_issues_to_done(
        issue_finder,
        project_mover,
        retention_days=21,
        now=NOW,
    )

    assert result == ExpiredIssueDoneResult((12,), (), (11,), (10,))
    assert project_mover.requested == [10, 11, 12]


def test_retention_rejects_invalid_age_configuration_and_naive_clock() -> None:
    issue_finder = IssueFinder(())
    project_mover = ProjectMover({})
    with pytest.raises(ValueError, match="retention_days"):
        move_expired_tracker_issues_to_done(
            issue_finder,
            project_mover,
            retention_days=0,
            now=NOW,
        )
    with pytest.raises(ValueError, match="timezone"):
        move_expired_tracker_issues_to_done(
            issue_finder,
            project_mover,
            retention_days=21,
            now=datetime(2026, 8, 17, 12, 0),
        )


def test_retention_config_defaults_to_three_weeks_and_rejects_bad_values() -> None:
    assert job_alert_issue_retention_days({}) == 21
    assert job_alert_issue_retention_days({"JOB_ALERT_ISSUE_RETENTION_DAYS": "30"}) == 30
    assert job_alert_issue_retention_days({"JOB_ALERT_ISSUE_RETENTION_DAYS": "0"}) == 21
    assert job_alert_issue_retention_days({"JOB_ALERT_ISSUE_RETENTION_DAYS": "not-a-number"}) == 21


def test_issue_notifier_paginates_open_issues_without_modifying_them() -> None:
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
            raise AssertionError((method, url_or_path, payload))

    notifier = PaginatedNotifier("not-a-real-token", "LamdaDev/sf-job-tracker")

    assert [issue["number"] for issue in notifier.iter_open_issues()] == [1, 2]
    assert all(method == "GET" for method, _, _ in calls)


def test_project_notifier_updates_linked_items_only_when_not_already_done() -> None:
    calls: list[tuple[str, Mapping[str, Any]]] = []

    class FakeProjectNotifier(GitHubProjectNotifier):
        def _request_graphql(
            self, query: str, variables: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            calls.append((query, variables))
            if query == PROJECT_ITEMS_QUERY:
                return {
                    "repository": {
                        "issue": {
                            "projectItems": {
                                "nodes": [
                                    _project_item(
                                        item_id="item-1",
                                        project_id="project-1",
                                        project_title="Job search",
                                        status="Todo",
                                    ),
                                    _project_item(
                                        item_id="item-2",
                                        project_id="project-2",
                                        project_title="Applications",
                                        status="Done",
                                    ),
                                ],
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    }
                }
            if query == UPDATE_PROJECT_STATUS_MUTATION:
                return {
                    "updateProjectV2ItemFieldValue": {
                        "projectV2Item": {"id": variables["itemId"]}
                    }
                }
            raise AssertionError(query)

    notifier = FakeProjectNotifier("not-a-real-token", "LamdaDev/sf-job-tracker")

    result = notifier.move_issue_to_done(42)

    assert result == ProjectStatusUpdateResult(("Job search",), ("Applications",))
    assert calls[0][1] == {
        "owner": "LamdaDev",
        "repository": "sf-job-tracker",
        "issueNumber": 42,
    }
    assert calls[1] == (
        UPDATE_PROJECT_STATUS_MUTATION,
        {
            "projectId": "project-1",
            "itemId": "item-1",
            "fieldId": "status-project-1",
            "optionId": "done-project-1",
        },
    )
    assert len(calls) == 2


def test_project_notifier_rejects_a_linked_project_without_done_status() -> None:
    item = _project_item(
        item_id="item-1",
        project_id="project-1",
        project_title="Job search",
        status="Todo",
    )
    item["project"]["fields"]["nodes"][0]["options"] = [
        {"id": "todo", "name": "Todo"}
    ]

    class FakeProjectNotifier(GitHubProjectNotifier):
        def _request_graphql(
            self, query: str, variables: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            return {
                "repository": {
                    "issue": {
                        "projectItems": {
                            "nodes": [item],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            }

    notifier = FakeProjectNotifier("not-a-real-token", "LamdaDev/sf-job-tracker")

    with pytest.raises(GitHubNotificationError, match="no 'Done' Status option"):
        notifier.move_issue_to_done(42)


def test_project_cleanup_requires_both_repository_and_projects_tokens() -> None:
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        check_jobs.move_expired_issues_to_done(environment={})
    with pytest.raises(ValueError, match="PROJECTS_TOKEN"):
        check_jobs.move_expired_issues_to_done(environment={"GITHUB_TOKEN": "issue-token"})


def test_cleanup_cli_path_does_not_collect_jobs_or_write_tracker_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        check_jobs,
        "move_expired_issues_to_done",
        lambda: calls.append("cleanup") or ExpiredIssueDoneResult((), (), (), ()),
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

    assert check_jobs.main(["--move-expired-issues-to-done"]) == 0
    assert calls == ["cleanup"]


def test_cleanup_cli_returns_nonzero_when_a_project_update_remains_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        check_jobs,
        "move_expired_issues_to_done",
        lambda: ExpiredIssueDoneResult((), (), (), (99,)),
    )

    assert check_jobs.main(["--move-expired-issues-to-done"]) == 2

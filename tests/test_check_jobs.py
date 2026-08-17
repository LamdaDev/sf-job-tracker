from __future__ import annotations

from pathlib import Path

import pytest

import src.check_jobs as check_jobs
from src.check_jobs import main, run_tracker
from src.config import SOURCES
from src.fetcher import FetchedSnapshot
from src.notifier import DeliveryResult, GitHubNotificationError, ManualTestNotificationResult
from src.parser import UpstreamFormatError


FIXTURES = Path(__file__).parent / "fixtures"


def snapshot_from_fixtures() -> FetchedSnapshot:
    return FetchedSnapshot(
        commit_sha="a" * 40,
        documents={
            SOURCES[0]: (FIXTURES / "internships.md").read_text(encoding="utf-8"),
            SOURCES[1]: (FIXTURES / "new_grads.md").read_text(encoding="utf-8"),
        },
    )


def test_dry_run_does_not_write_state_or_dashboard(tmp_path: Path) -> None:
    summary = run_tracker(
        root=tmp_path,
        dry_run=True,
        snapshot_fetcher=snapshot_from_fixtures,
        timestamp="2026-08-16T12:00:00Z",
    )

    assert summary.transition.baseline is True
    assert summary.matching_count == 7
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "jobs.md").exists()


def test_matching_location_is_filtered_before_duplicate_urls_are_collapsed(tmp_path: Path) -> None:
    internship_source = (FIXTURES / "internships.md").read_text(encoding="utf-8")
    matching_row = next(line for line in internship_source.splitlines() if "figma/123" in line)
    nonmatching_duplicate = matching_row.replace(
        "San Francisco, CA +1", "Seattle, WA"
    ).replace("<strong>Figma</strong>", "<strong>Outside duplicate</strong>")
    snapshot = FetchedSnapshot(
        commit_sha="b" * 40,
        documents={
            SOURCES[0]: internship_source.replace(
                matching_row, f"{nonmatching_duplicate}\n{matching_row}", 1
            ),
            SOURCES[1]: (FIXTURES / "new_grads.md").read_text(encoding="utf-8"),
        },
    )

    summary = run_tracker(
        root=tmp_path,
        dry_run=True,
        snapshot_fetcher=lambda: snapshot,
        timestamp="2026-08-16T12:00:00Z",
    )

    assert summary.matching_count == 7
    assert any(
        record["application_url"] == "https://boards.example.test/figma/123?gh_jid=123&source=fixture"
        for record in summary.transition.current_state["jobs"].values()
    )


def test_partial_parse_failure_preserves_failed_source_presence(tmp_path: Path) -> None:
    run_tracker(
        root=tmp_path,
        snapshot_fetcher=snapshot_from_fixtures,
        timestamp="2026-08-16T12:00:00Z",
    )
    seen_path = tmp_path / "data" / "seen_jobs.json"
    current_path = tmp_path / "data" / "current_jobs.json"
    dashboard_path = tmp_path / "jobs.md"
    before = {path: path.read_text(encoding="utf-8") for path in (seen_path, current_path, dashboard_path)}

    def broken_snapshot() -> FetchedSnapshot:
        snapshot = snapshot_from_fixtures()
        return FetchedSnapshot(
            commit_sha="b" * 40,
            documents={SOURCES[0]: "not a compatible source", SOURCES[1]: snapshot.documents[SOURCES[1]]},
        )

    summary = run_tracker(root=tmp_path, snapshot_fetcher=broken_snapshot)

    assert {path: path.read_text(encoding="utf-8") for path in before} == before
    assert "speedyapply_internships" in summary.source_errors


def test_all_parser_failures_raise_without_creating_tracker_files(tmp_path: Path) -> None:
    broken = FetchedSnapshot(commit_sha="c" * 40, documents={SOURCES[0]: "not a compatible source"})
    with pytest.raises(UpstreamFormatError, match="No configured source parsed successfully"):
        run_tracker(root=tmp_path, snapshot_fetcher=lambda: broken)
    assert not (tmp_path / "data").exists()


def test_delivery_command_returns_nonzero_after_persisting_notification_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed_delivery = DeliveryResult(
        delivered_batches=("sent",), failed_batches=("pending",), existing_issue_batches=()
    )
    monkeypatch.setattr(check_jobs, "deliver_pending", lambda *, root: failed_delivery)

    assert main(["--deliver-pending", "--root", str(tmp_path)]) == 2


def test_test_notification_mode_does_not_collect_or_write_tracker_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[object] = []

    class FakeIssueNotifier:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created.append((args, kwargs))

    def fake_send(notifier: object) -> ManualTestNotificationResult:
        assert isinstance(notifier, FakeIssueNotifier)
        return ManualTestNotificationResult(issue_number=88, created=True)

    monkeypatch.setattr(check_jobs, "GitHubIssueNotifier", FakeIssueNotifier)
    monkeypatch.setattr(check_jobs, "send_test_issue_notification", fake_send)
    monkeypatch.setattr(
        check_jobs,
        "run_tracker",
        lambda **_: pytest.fail("test-notification mode must not collect jobs"),
    )
    monkeypatch.setattr(
        check_jobs,
        "deliver_pending",
        lambda **_: pytest.fail("test-notification mode must not deliver job alerts"),
    )

    issue_number = check_jobs.send_test_notification(
        environment={
            "GITHUB_TOKEN": "not-a-real-token",
            "GITHUB_REPOSITORY": "LamdaDev/sf-job-tracker",
        }
    )

    assert issue_number == 88
    assert created
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "jobs.md").exists()


def test_cli_test_notification_mode_only_runs_the_test_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        check_jobs,
        "send_test_notification",
        lambda: calls.append("test") or 88,
    )
    monkeypatch.setattr(
        check_jobs,
        "run_tracker",
        lambda **_: pytest.fail("test-notification mode must not collect jobs"),
    )
    monkeypatch.setattr(
        check_jobs,
        "deliver_pending",
        lambda **_: pytest.fail("test-notification mode must not deliver job alerts"),
    )

    assert main(["--send-test-notification"]) == 0
    assert calls == ["test"]


def test_cli_test_notification_mode_reports_api_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        check_jobs,
        "send_test_notification",
        lambda: (_ for _ in ()).throw(GitHubNotificationError("API unavailable")),
    )

    assert main(["--send-test-notification"]) == 1

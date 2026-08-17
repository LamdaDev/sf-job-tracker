from __future__ import annotations

from src.notifier import (
    GitHubNotificationError,
    TEST_NOTIFICATION_MARKER,
    build_issue_body,
    build_test_issue_body,
    deliver_pending_notifications,
    issue_marker,
    issue_title,
    send_test_notification as send_test_issue_notification,
    test_issue_title as notification_test_issue_title,
)
from src.storage import empty_seen_state
from src.tracker import apply_current_jobs
from .test_tracker import make_job


class FakeNotifier:
    def __init__(self, *, existing_issue: int | None = None, fail_for: set[str] | None = None) -> None:
        self.existing_issue = existing_issue
        self.fail_for = fail_for or set()
        self.created: list[tuple[list, str]] = []

    def find_issue_for_batch(self, batch_id: str) -> int | None:
        if batch_id in self.fail_for:
            raise GitHubNotificationError("temporary API failure")
        return self.existing_issue

    def create_issue(self, jobs: list, batch_id: str) -> int:
        self.created.append((jobs, batch_id))
        return 42


class FakeTestNotifier:
    def __init__(self) -> None:
        self.create_calls = 0

    def create_test_issue(self) -> int:
        self.create_calls += 1
        return 76 + self.create_calls


def state_with_pending_batch() -> tuple[dict, str]:
    first = make_job("https://jobs.example.test/one")
    second = make_job("https://jobs.example.test/two")
    baseline = apply_current_jobs(
        empty_seen_state(), [first], successful_source_ids={first.source_id}, timestamp="2026-08-16T12:00:00Z"
    )
    transition = apply_current_jobs(
        baseline.state, [first, second], successful_source_ids={first.source_id}, timestamp="2026-08-16T13:00:00Z"
    )
    return transition.state, next(iter(transition.state["pending_notifications"]))


def test_notification_payload_is_canonical_actionable_and_has_hidden_marker() -> None:
    job = make_job("https://jobs.example.test/one", company="Notion")
    body = build_issue_body([job], "abc123")

    assert issue_title(1) == "🚨 1 new San Francisco Bay Area SWE job"
    assert issue_title(2) == "🚨 2 new San Francisco Bay Area SWE jobs"
    assert "**Type:** New Grad" in body
    assert "**Category:** Other" in body
    assert "**Sources:** SpeedyApply" in body
    assert "[Apply to Notion →](<https://jobs.example.test/one>)" in body
    assert issue_marker("abc123") in body


def test_test_notification_payload_is_unmistakable_and_state_free() -> None:
    body = build_test_issue_body()
    assert notification_test_issue_title() == "🧪 TEST — San Francisco Bay Area job tracker notification"
    assert "not** a job alert" in body
    assert "did not fetch jobs or change tracker history" in body
    assert "Every manual test run deliberately creates a fresh Issue" in body
    assert TEST_NOTIFICATION_MARKER in body


def test_test_notification_always_creates_a_fresh_issue() -> None:
    notifier = FakeTestNotifier()
    assert send_test_issue_notification(notifier) == 77  # type: ignore[arg-type]
    assert send_test_issue_notification(notifier) == 78  # type: ignore[arg-type]
    assert notifier.create_calls == 2


def test_delivery_marks_created_issue_as_sent_and_renders_canonical_job_once() -> None:
    state, batch_id = state_with_pending_batch()
    notifier = FakeNotifier()
    result = deliver_pending_notifications(state, notifier)  # type: ignore[arg-type]
    assert result.delivered_batches == (batch_id,)
    assert state["pending_notifications"][batch_id]["status"] == "sent"
    assert state["pending_notifications"][batch_id]["issue_number"] == 42
    assert len(notifier.created[0][0]) == 1


def test_delivery_recognises_existing_issue_and_failure_stays_pending() -> None:
    state, batch_id = state_with_pending_batch()
    existing_notifier = FakeNotifier(existing_issue=99)
    existing = deliver_pending_notifications(state, existing_notifier)  # type: ignore[arg-type]
    assert existing.existing_issue_batches == (batch_id,)
    assert state["pending_notifications"][batch_id]["issue_number"] == 99
    assert existing_notifier.created == []

    state, batch_id = state_with_pending_batch()
    failed = deliver_pending_notifications(state, FakeNotifier(fail_for={batch_id}))  # type: ignore[arg-type]
    assert failed.failed_batches == (batch_id,)
    assert state["pending_notifications"][batch_id]["status"] == "pending"

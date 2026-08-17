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
    def __init__(self, *, existing_issue: int | None = None) -> None:
        self.existing_issue = existing_issue
        self.create_calls = 0

    def find_issue_with_marker(self, marker: str) -> int | None:
        assert marker == TEST_NOTIFICATION_MARKER
        return self.existing_issue

    def create_test_issue(self) -> int:
        self.create_calls += 1
        return 77


def state_with_pending_batch() -> tuple[dict, str]:
    first = make_job("https://jobs.example.test/one")
    second = make_job("https://jobs.example.test/two")
    baseline = apply_current_jobs(empty_seen_state(), [first], timestamp="2026-08-16T12:00:00Z")
    transition = apply_current_jobs(baseline.state, [first, second], timestamp="2026-08-16T13:00:00Z")
    return transition.state, next(iter(transition.state["pending_notifications"]))


def test_notification_payload_is_actionable_and_has_hidden_marker() -> None:
    job = make_job("https://jobs.example.test/one", company="Notion")
    body = build_issue_body([job], "abc123")

    assert issue_title(1) == "🚨 1 new San Francisco Bay Area SWE job"
    assert issue_title(2) == "🚨 2 new San Francisco Bay Area SWE jobs"
    assert "**Type:** New Grad" in body
    assert "**Category:** Other" in body
    assert "[Apply to Notion →](<https://jobs.example.test/one>)" in body
    assert issue_marker("abc123") in body


def test_test_notification_payload_is_unmistakable_and_state_free() -> None:
    body = build_test_issue_body()

    assert notification_test_issue_title() == "🧪 TEST — San Francisco Bay Area job tracker notification"
    assert "not** a job alert" in body
    assert "did not fetch jobs or change tracker history" in body
    assert TEST_NOTIFICATION_MARKER in body


def test_test_notification_creates_once_then_is_idempotent() -> None:
    fresh = FakeTestNotifier()
    created = send_test_issue_notification(fresh)  # type: ignore[arg-type]
    assert created.created is True
    assert created.issue_number == 77
    assert fresh.create_calls == 1

    existing = FakeTestNotifier(existing_issue=91)
    reused = send_test_issue_notification(existing)  # type: ignore[arg-type]
    assert reused.created is False
    assert reused.issue_number == 91
    assert existing.create_calls == 0


def test_delivery_marks_created_issue_as_sent() -> None:
    state, batch_id = state_with_pending_batch()
    notifier = FakeNotifier()

    result = deliver_pending_notifications(state, notifier)  # type: ignore[arg-type]

    assert result.delivered_batches == (batch_id,)
    assert state["pending_notifications"][batch_id]["status"] == "sent"
    assert state["pending_notifications"][batch_id]["issue_number"] == 42
    assert len(notifier.created) == 1


def test_delivery_recognises_existing_issue_and_does_not_create_duplicate() -> None:
    state, batch_id = state_with_pending_batch()
    notifier = FakeNotifier(existing_issue=99)

    result = deliver_pending_notifications(state, notifier)  # type: ignore[arg-type]

    assert result.existing_issue_batches == (batch_id,)
    assert result.delivered_batches == ()
    assert notifier.created == []
    assert state["pending_notifications"][batch_id]["issue_number"] == 99


def test_delivery_failure_leaves_batch_pending() -> None:
    state, batch_id = state_with_pending_batch()
    notifier = FakeNotifier(fail_for={batch_id})

    result = deliver_pending_notifications(state, notifier)  # type: ignore[arg-type]

    assert result.failed_batches == (batch_id,)
    assert state["pending_notifications"][batch_id]["status"] == "pending"

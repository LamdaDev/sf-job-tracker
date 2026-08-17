from __future__ import annotations

import copy

from src.config import LOCATION_SCOPE_VERSION
from src.models import Job
from src.storage import empty_seen_state
from src.tracker import apply_current_jobs


def make_job(
    url: str,
    *,
    company: str = "Example",
    position: str = "Software Engineer",
    location: str = "San Francisco, CA",
) -> Job:
    return Job(
        company=company,
        position=position,
        location=location,
        salary=None,
        application_url=url,
        age="1d",
        category="Other",
        job_type="New Grad",
        source_file="NEW_GRAD_USA.md",
    )


def test_first_run_establishes_baseline_without_new_alerts() -> None:
    job = make_job("https://jobs.example.test/one")

    transition = apply_current_jobs(empty_seen_state(), [job], timestamp="2026-08-16T12:00:00Z")

    assert transition.baseline is True
    assert transition.new_jobs == ()
    assert transition.state["initialized"] is True
    assert transition.state["jobs"][job.application_url]["active"] is True
    assert transition.state["pending_notifications"] == {}


def test_new_url_after_baseline_creates_one_pending_notification_batch() -> None:
    first = make_job("https://jobs.example.test/one")
    second = make_job("https://jobs.example.test/two")
    baseline = apply_current_jobs(empty_seen_state(), [first], timestamp="2026-08-16T12:00:00Z")

    transition = apply_current_jobs(baseline.state, [first, second], timestamp="2026-08-16T13:00:00Z")

    assert transition.baseline is False
    assert transition.new_jobs == (second,)
    batch = next(iter(transition.state["pending_notifications"].values()))
    assert batch["job_urls"] == [second.application_url]
    assert batch["status"] == "pending"


def test_existing_url_does_not_alert_twice_or_churn_last_seen() -> None:
    first = make_job("https://jobs.example.test/one")
    baseline = apply_current_jobs(empty_seen_state(), [first], timestamp="2026-08-16T12:00:00Z")

    transition = apply_current_jobs(baseline.state, [first], timestamp="2026-08-16T13:00:00Z")

    assert transition.new_jobs == ()
    assert transition.state["jobs"][first.application_url]["last_seen"] == "2026-08-16T12:00:00Z"
    assert transition.state == baseline.state


def test_disappearance_and_reappearance_change_active_without_second_alert() -> None:
    first = make_job("https://jobs.example.test/one")
    second = make_job("https://jobs.example.test/two")
    baseline = apply_current_jobs(empty_seen_state(), [first, second], timestamp="2026-08-16T12:00:00Z")

    missing = apply_current_jobs(baseline.state, [second], timestamp="2026-08-16T13:00:00Z")
    first_record = missing.state["jobs"][first.application_url]
    assert missing.inactive_count == 1
    assert first_record["active"] is False
    assert first_record["inactive_at"] == "2026-08-16T13:00:00Z"

    reappeared = apply_current_jobs(missing.state, [first, second], timestamp="2026-08-16T14:00:00Z")
    assert reappeared.reactivated_count == 1
    assert reappeared.new_jobs == ()
    assert reappeared.state["jobs"][first.application_url]["active"] is True


def test_different_urls_with_same_company_and_title_are_distinct() -> None:
    first = make_job("https://jobs.example.test/one")
    second = make_job("https://jobs.example.test/two")
    baseline = apply_current_jobs(empty_seen_state(), [first], timestamp="2026-08-16T12:00:00Z")

    transition = apply_current_jobs(baseline.state, [first, second], timestamp="2026-08-16T13:00:00Z")

    assert len(transition.state["jobs"]) == 2
    assert transition.new_jobs == (second,)


def test_exact_duplicate_url_in_one_snapshot_is_collapsed() -> None:
    first = make_job("https://jobs.example.test/one")
    duplicate_with_different_title = make_job(
        "https://jobs.example.test/one", position="Software Engineer II"
    )

    transition = apply_current_jobs(
        empty_seen_state(), [first, duplicate_with_different_title], timestamp="2026-08-16T12:00:00Z"
    )

    assert transition.duplicate_count == 1
    assert len(transition.state["jobs"]) == 1
    assert transition.state["jobs"][first.application_url]["position"] == "Software Engineer"


def test_initialize_suppresses_alerts_for_newly_observed_urls() -> None:
    first = make_job("https://jobs.example.test/one")
    second = make_job("https://jobs.example.test/two")
    baseline = apply_current_jobs(empty_seen_state(), [first], timestamp="2026-08-16T12:00:00Z")

    transition = apply_current_jobs(
        baseline.state, [first, second], timestamp="2026-08-16T13:00:00Z", initialize=True
    )

    assert transition.baseline is True
    assert transition.new_jobs == ()
    assert transition.state["pending_notifications"] == {}


def test_expanded_location_scope_rebaselines_legacy_history_without_an_alert() -> None:
    san_francisco = make_job("https://jobs.example.test/san-francisco")
    original = apply_current_jobs(
        empty_seen_state(), [san_francisco], timestamp="2026-08-16T12:00:00Z"
    )
    legacy_state = copy.deepcopy(original.state)
    legacy_state.pop("location_scope_version")
    san_jose = make_job("https://jobs.example.test/san-jose", location="San Jose, CA")

    expanded = apply_current_jobs(
        legacy_state,
        [san_francisco, san_jose],
        timestamp="2026-08-16T13:00:00Z",
    )

    assert expanded.baseline is True
    assert expanded.scope_rebased is True
    assert expanded.new_jobs == ()
    assert expanded.state["location_scope_version"] == LOCATION_SCOPE_VERSION
    assert expanded.state["pending_notifications"] == {}

    sunnyvale = make_job("https://jobs.example.test/sunnyvale", location="Sunnyvale, CA")
    after_rebaseline = apply_current_jobs(
        expanded.state,
        [san_francisco, san_jose, sunnyvale],
        timestamp="2026-08-16T14:00:00Z",
    )

    assert after_rebaseline.baseline is False
    assert after_rebaseline.scope_rebased is False
    assert after_rebaseline.new_jobs == (sunnyvale,)

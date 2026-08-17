from __future__ import annotations

import copy

from src.canonical import canonicalize_job_url
from src.config import LOCATION_SCOPE_VERSION
from src.models import Job
from src.storage import empty_seen_state
from src.tracker import apply_current_jobs


def make_job(
    url: str,
    *,
    source_id: str = "speedyapply_new_grad",
    source_label: str = "SpeedyApply",
    source_url: str | None = None,
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
        source_file="NEW_GRAD_USA.md" if source_id.endswith("new_grad") else "README.md",
        source_id=source_id,
        source_label=source_label,
        source_url=source_url or url,
    )


def test_first_run_establishes_baseline_without_new_alerts() -> None:
    job = make_job("https://jobs.example.test/one")
    transition = apply_current_jobs(
        empty_seen_state(), [job], successful_source_ids={job.source_id}, timestamp="2026-08-16T12:00:00Z"
    )

    assert transition.baseline is True
    assert transition.new_jobs == ()
    assert transition.baselined_source_ids == (job.source_id,)
    key = canonicalize_job_url(job.application_url)
    assert transition.state["jobs"][key]["active"] is True
    assert transition.state["pending_notifications"] == {}


def test_new_existing_source_job_creates_one_canonical_notification_batch() -> None:
    first = make_job("https://jobs.example.test/one")
    second = make_job("https://jobs.example.test/two")
    baseline = apply_current_jobs(
        empty_seen_state(), [first], successful_source_ids={first.source_id}, timestamp="2026-08-16T12:00:00Z"
    )
    transition = apply_current_jobs(
        baseline.state, [first, second], successful_source_ids={first.source_id}, timestamp="2026-08-16T13:00:00Z"
    )

    assert transition.baseline is False
    assert [job.application_url for job in transition.new_jobs] == [second.application_url]
    batch = next(iter(transition.state["pending_notifications"].values()))
    assert batch["job_ids"] == [canonicalize_job_url(second.application_url)]
    assert batch["status"] == "pending"


def test_unchanged_observation_does_not_churn_state() -> None:
    job = make_job("https://jobs.example.test/one")
    baseline = apply_current_jobs(
        empty_seen_state(), [job], successful_source_ids={job.source_id}, timestamp="2026-08-16T12:00:00Z"
    )
    transition = apply_current_jobs(
        baseline.state, [job], successful_source_ids={job.source_id}, timestamp="2026-08-16T13:00:00Z"
    )

    assert transition.new_jobs == ()
    assert transition.state == baseline.state
    assert transition.current_state == baseline.current_state


def test_relative_age_change_does_not_create_meaningless_state_churn() -> None:
    job = make_job("https://jobs.example.test/one")
    baseline = apply_current_jobs(
        empty_seen_state(), [job], successful_source_ids={job.source_id}, timestamp="2026-08-16T12:00:00Z"
    )
    newer_age = Job(**{**job.__dict__, "age": "2d"})
    transition = apply_current_jobs(
        baseline.state, [newer_age], successful_source_ids={job.source_id}, timestamp="2026-08-16T13:00:00Z"
    )
    assert transition.state == baseline.state


def test_new_source_is_silently_baselined_but_existing_source_still_alerts() -> None:
    known = make_job("https://jobs.example.test/known")
    baseline = apply_current_jobs(
        empty_seen_state(), [known], successful_source_ids={known.source_id}, timestamp="2026-08-16T12:00:00Z"
    )
    applyguy_known = make_job(
        known.application_url,
        source_id="applyguy_new_grad",
        source_label="ApplyGuy",
        source_url="https://applyguy.ai/jobs/known",
    )
    source_onboarding = apply_current_jobs(
        baseline.state,
        [known, applyguy_known],
        successful_source_ids={known.source_id, applyguy_known.source_id},
        timestamp="2026-08-16T13:00:00Z",
    )
    assert source_onboarding.new_jobs == ()
    assert source_onboarding.baselined_source_ids == ("applyguy_new_grad",)
    record = source_onboarding.state["jobs"][canonicalize_job_url(known.application_url)]
    assert set(record["sources"]) == {"speedyapply_new_grad", "applyguy_new_grad"}

    new_from_both_speedy = make_job("https://jobs.example.test/new")
    new_from_both_applyguy = make_job(
        new_from_both_speedy.application_url,
        source_id="applyguy_new_grad",
        source_label="ApplyGuy",
    )
    later = apply_current_jobs(
        source_onboarding.state,
        [known, applyguy_known, new_from_both_speedy, new_from_both_applyguy],
        successful_source_ids={known.source_id, applyguy_known.source_id},
        timestamp="2026-08-16T14:00:00Z",
    )
    assert len(later.new_jobs) == 1
    assert later.new_jobs[0].canonical_id == canonicalize_job_url(new_from_both_speedy.application_url)


def test_source_aware_inactivity_and_failed_source_unknown_semantics() -> None:
    speedy = make_job("https://jobs.example.test/one")
    applyguy = make_job(
        speedy.application_url,
        source_id="applyguy_new_grad",
        source_label="ApplyGuy",
    )
    baseline = apply_current_jobs(
        empty_seen_state(),
        [speedy, applyguy],
        successful_source_ids={speedy.source_id, applyguy.source_id},
        timestamp="2026-08-16T12:00:00Z",
    )

    # SpeedyApply succeeds and no longer lists it, while ApplyGuy does: the
    # canonical job remains active and only the source membership changes.
    one_source_removed = apply_current_jobs(
        baseline.state,
        [applyguy],
        successful_source_ids={speedy.source_id, applyguy.source_id},
        timestamp="2026-08-16T13:00:00Z",
    )
    key = canonicalize_job_url(speedy.application_url)
    assert one_source_removed.state["jobs"][key]["active"] is True
    assert one_source_removed.state["jobs"][key]["sources"][speedy.source_id]["active"] is False

    # A failed SpeedyApply fetch is unknown, so it cannot change source/global
    # activity while ApplyGuy remains available.
    failed_speedy = apply_current_jobs(
        baseline.state,
        [applyguy],
        successful_source_ids={applyguy.source_id},
        timestamp="2026-08-16T13:00:00Z",
    )
    assert failed_speedy.state["jobs"][key]["sources"][speedy.source_id]["active"] is True

    # A healthy lower-detail source must not erase salary/category obtained
    # from a source that is temporarily unavailable.
    speedy_with_salary = Job(**{**speedy.__dict__, "salary": "$60/hr", "category": "FAANG+"})
    rich_baseline = apply_current_jobs(
        empty_seen_state(), [speedy_with_salary], successful_source_ids={speedy.source_id}, timestamp="2026-08-16T12:00:00Z"
    )
    preserved = apply_current_jobs(
        rich_baseline.state, [applyguy], successful_source_ids={applyguy.source_id}, timestamp="2026-08-16T13:00:00Z"
    )
    rich_key = canonicalize_job_url(speedy.application_url)
    assert preserved.state["jobs"][rich_key]["salary"] == "$60/hr"
    assert preserved.state["jobs"][rich_key]["category"] == "FAANG+"

    all_removed = apply_current_jobs(
        one_source_removed.state,
        [],
        successful_source_ids={speedy.source_id, applyguy.source_id},
        timestamp="2026-08-16T14:00:00Z",
    )
    assert all_removed.inactive_count == 1
    assert all_removed.state["jobs"][key]["active"] is False


def test_disappearance_then_reappearance_never_alerts_again() -> None:
    job = make_job("https://jobs.example.test/one")
    baseline = apply_current_jobs(
        empty_seen_state(), [job], successful_source_ids={job.source_id}, timestamp="2026-08-16T12:00:00Z"
    )
    missing = apply_current_jobs(
        baseline.state, [], successful_source_ids={job.source_id}, timestamp="2026-08-16T13:00:00Z"
    )
    reappeared = apply_current_jobs(
        missing.state, [job], successful_source_ids={job.source_id}, timestamp="2026-08-16T14:00:00Z"
    )
    assert reappeared.reactivated_count == 1
    assert reappeared.new_jobs == ()


def test_wrapper_only_job_is_promoted_when_direct_ats_url_appears_without_alerting() -> None:
    wrapper = make_job(
        "https://applyguy.ai/jobs/notion",
        source_id="applyguy_new_grad",
        source_label="ApplyGuy",
        company="Notion",
        position="Software Engineer",
        source_url="https://applyguy.ai/jobs/notion",
    )
    baseline = apply_current_jobs(
        empty_seen_state(), [wrapper], successful_source_ids={wrapper.source_id}, timestamp="2026-08-16T12:00:00Z"
    )
    direct = make_job(
        "https://jobs.ashbyhq.com/notion/ABC/application?embed=true&utm_source=applyguy",
        source_id="applyguy_new_grad",
        source_label="ApplyGuy",
        company="Notion",
        position="Software Engineer",
        source_url=wrapper.application_url,
    )
    promoted = apply_current_jobs(
        baseline.state, [direct], successful_source_ids={direct.source_id}, timestamp="2026-08-16T13:00:00Z"
    )
    assert promoted.new_jobs == ()
    assert len(promoted.state["jobs"]) == 1
    record = promoted.state["jobs"]["ashby:notion:abc"]
    assert record["first_seen"] == "2026-08-16T12:00:00Z"


def test_successful_new_source_with_zero_matching_jobs_is_still_initialized() -> None:
    baseline = apply_current_jobs(
        empty_seen_state(),
        [],
        successful_source_ids={"speedyapply_internships"},
        timestamp="2026-08-16T12:00:00Z",
    )
    assert baseline.state["initialized_sources"]["speedyapply_internships"] is True
    later = apply_current_jobs(
        baseline.state,
        [],
        successful_source_ids={"simplify_summer_2027"},
        timestamp="2026-08-16T13:00:00Z",
    )
    assert later.baselined_source_ids == ("simplify_summer_2027",)
    assert later.state["initialized_sources"]["simplify_summer_2027"] is True


def test_expanded_location_scope_silently_rebaselines_existing_v2_history() -> None:
    san_francisco = make_job("https://jobs.example.test/san-francisco")
    original = apply_current_jobs(
        empty_seen_state(),
        [san_francisco],
        successful_source_ids={san_francisco.source_id},
        timestamp="2026-08-16T12:00:00Z",
    )
    # The immediately preceding multi-source state had no scope marker and
    # contained only SF jobs. Treat it as legacy coverage, rather than sending
    # alerts for every newly included nearby-city listing.
    legacy_v2_state = copy.deepcopy(original.state)
    legacy_v2_state.pop("location_scope_version")
    san_jose = make_job("https://jobs.example.test/san-jose", location="San Jose, CA")

    expanded = apply_current_jobs(
        legacy_v2_state,
        [san_francisco, san_jose],
        successful_source_ids={san_francisco.source_id},
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
        successful_source_ids={san_francisco.source_id},
        timestamp="2026-08-16T14:00:00Z",
    )

    assert after_rebaseline.baseline is False
    assert after_rebaseline.scope_rebased is False
    assert after_rebaseline.new_jobs[0].canonical_id == canonicalize_job_url(sunnyvale.application_url)

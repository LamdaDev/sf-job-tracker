from __future__ import annotations

from src.canonical import aggregate_observations, canonicalize_job_url, inspect_job_url
from src.models import Job


def make_observation(
    application_url: str,
    *,
    source_id: str = "speedyapply_internships",
    source_label: str = "SpeedyApply",
    source_url: str | None = None,
    company: str = "Acme",
    position: str = "Software Engineer Intern",
    location: str = "San Francisco, CA",
    season: str = "Summer 2027",
) -> Job:
    return Job(
        company=company,
        position=position,
        location=location,
        salary="$50/hr",
        application_url=application_url,
        age="1d",
        category="Software Engineering",
        job_type="Internship",
        source_file="README.md",
        source_id=source_id,
        source_label=source_label,
        source_url=source_url,
        season=season,
    )


def test_ashby_application_and_tracking_variations_share_one_requisition_identity() -> None:
    base = "https://jobs.ashbyhq.com/Acme/7f2d7c92-1111-2222-3333-444444444444"
    embedded = (
        f"{base}/application?embed=true&utm_source=simplify&"
        "utm_campaign=summer-2027#application"
    )

    assert canonicalize_job_url(base) == "ashby:acme:7f2d7c92-1111-2222-3333-444444444444"
    assert canonicalize_job_url(embedded) == canonicalize_job_url(base)
    assert inspect_job_url(embedded).canonical_url == f"{base}/application"


def test_generic_direct_url_removes_safe_marketing_parameters_but_preserves_identity_query() -> None:
    base = "https://careers.example.test/jobs/apply?jobId=123&department=eng"
    tracking = "https://careers.example.test/jobs/apply?utm_source=feed&jobId=123&department=eng&gclid=abc"
    assert canonicalize_job_url(base) == canonicalize_job_url(tracking)


def test_greenhouse_and_lever_use_provider_requisition_ids() -> None:
    greenhouse = "https://boards.greenhouse.io/acme/jobs/1234567?gh_src=abc"
    lever = "https://jobs.lever.co/acme/4d2a1bcd-1234-5678-90ab-cdef12345678?lever-source=feed"

    assert canonicalize_job_url(greenhouse) == "greenhouse:acme:1234567"
    assert canonicalize_job_url(lever) == "lever:acme:4d2a1bcd-1234-5678-90ab-cdef12345678"


def test_distinct_known_requisition_ids_are_never_merged_by_matching_text() -> None:
    first = make_observation("https://jobs.ashbyhq.com/acme/first-requisition")
    second = make_observation("https://jobs.ashbyhq.com/acme/second-requisition")

    jobs, duplicates = aggregate_observations((first, second))

    assert duplicates == 0
    assert {job.canonical_id for job in jobs} == {
        "ashby:acme:first-requisition",
        "ashby:acme:second-requisition",
    }


def test_workday_terminal_slugs_with_underscores_remain_distinct_requisitions() -> None:
    first_url = (
        "https://hp.wd5.myworkdayjobs.com/en-US/externalcareersite/job/"
        "San-Francisco-California-United-States-of-America/"
        "AI-Software-Engineer---HP-IQ_3163597-2"
    )
    second_url = (
        "https://hp.wd5.myworkdayjobs.com/en-US/exteu-ac-careersite/job/"
        "San-Francisco-California-United-States-of-America/"
        "AI-Software-Engineer---HP-IQ_3163597-1"
    )

    first_id = canonicalize_job_url(first_url)
    second_id = canonicalize_job_url(second_url)
    jobs, duplicates = aggregate_observations((make_observation(first_url), make_observation(second_url)))

    assert first_id == "workday:hp.wd5.myworkdayjobs.com:ai-software-engineer---hp-iq_3163597-2"
    assert second_id == "workday:hp.wd5.myworkdayjobs.com:ai-software-engineer---hp-iq_3163597-1"
    assert first_id != second_id
    assert duplicates == 0
    assert {job.canonical_id for job in jobs} == {first_id, second_id}


def test_single_wrapper_observation_attaches_to_its_one_exact_direct_match() -> None:
    direct = make_observation("https://jobs.ashbyhq.com/acme/requisition-42")
    wrapper = make_observation(
        "https://applyguy.ai/jobs/acme-software-engineer-intern",
        source_id="applyguy_internships",
        source_label="ApplyGuy",
        source_url="https://applyguy.ai/jobs/acme-software-engineer-intern",
    )

    jobs, duplicates = aggregate_observations((direct, wrapper))

    assert duplicates == 1
    assert len(jobs) == 1
    assert jobs[0].canonical_id == "ashby:acme:requisition-42"
    assert jobs[0].application_url == direct.application_url
    assert {observation.source_id for observation in jobs[0].observations} == {
        "speedyapply_internships",
        "applyguy_internships",
    }


def test_wrapper_does_not_attach_when_two_distinct_direct_requisitions_match_its_fingerprint() -> None:
    first = make_observation("https://jobs.ashbyhq.com/acme/requisition-42")
    second = make_observation("https://jobs.ashbyhq.com/acme/requisition-43")
    wrapper = make_observation(
        "https://simplify.jobs/p/acme-software-engineer-intern",
        source_id="simplify_summer_2027",
        source_label="Simplify",
        source_url="https://simplify.jobs/p/acme-software-engineer-intern",
    )

    jobs, duplicates = aggregate_observations((first, second, wrapper))

    assert duplicates == 0
    assert len(jobs) == 3
    assert any(job.canonical_id.startswith("fallback:") for job in jobs)


def test_same_requisition_from_three_sources_becomes_one_with_all_provenance() -> None:
    direct_url = "https://jobs.ashbyhq.com/acme/requisition-42"
    speedy = make_observation(direct_url)
    applyguy = make_observation(
        f"{direct_url}/application?embed=true&utm_source=applyguy",
        source_id="applyguy_internships",
        source_label="ApplyGuy",
        source_url="https://applyguy.ai/jobs/acme-software-engineer-intern",
    )
    simplify = make_observation(
        direct_url,
        source_id="simplify_summer_2027",
        source_label="Simplify",
        source_url="https://simplify.jobs/p/acme-software-engineer-intern",
    )

    jobs, duplicates = aggregate_observations((speedy, applyguy, simplify))

    assert duplicates == 2
    assert len(jobs) == 1
    canonical = jobs[0]
    assert canonical.canonical_id == "ashby:acme:requisition-42"
    assert {observation.source_id for observation in canonical.observations} == {
        "speedyapply_internships",
        "applyguy_internships",
        "simplify_summer_2027",
    }
    assert set(canonical.url_aliases) == {
        direct_url,
        applyguy.application_url,
        applyguy.source_url,
        simplify.source_url,
    }

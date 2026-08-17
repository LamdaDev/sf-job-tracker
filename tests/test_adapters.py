from __future__ import annotations

import json

from src.adapters import parse_applyguy_source, parse_simplify_source
from src.config import SOURCE_BY_ID
from src.tracker import location_matches


def test_applyguy_internships_prefer_listing_url_and_keep_wrapper_as_provenance() -> None:
    document = json.dumps(
        {
            "updatedAt": "2026-08-17T00:00:00Z",
            "jobs": [
                {
                    "id": "applyguy-intern-1",
                    "company": "Acme",
                    "title": "Software Engineering Intern",
                    "location": "San Francisco, CA",
                    "category": "Software Engineering",
                    "season": "Summer 2027",
                    "listingUrl": "https://jobs.ashbyhq.com/acme/123",
                    "url": "https://applyguy.example/jobs/acme-intern",
                    "age": "1d",
                },
                {
                    "id": "applyguy-pm-1",
                    "company": "Acme",
                    "title": "Product Management Intern",
                    "location": "San Francisco, CA",
                    "category": "Product Management",
                    "listingUrl": "https://jobs.example.test/acme/pm",
                    "url": "https://applyguy.example/jobs/acme-pm",
                },
            ],
        }
    )

    parsed = parse_applyguy_source(document, SOURCE_BY_ID["applyguy_internships"])

    assert len(parsed.jobs) == 1
    job = parsed.jobs[0]
    assert job.company == "Acme"
    assert job.application_url == "https://jobs.ashbyhq.com/acme/123"
    assert job.source_url == "https://applyguy.example/jobs/acme-intern"
    assert job.source_metadata["listingUrl"] == job.application_url
    assert job.source_metadata["url"] == job.source_url
    assert job.category == "Software Engineering"
    assert job.season == "Summer 2027"


def test_applyguy_new_grad_keeps_eligibility_and_filters_non_swe_roles() -> None:
    document = json.dumps(
        {
            "jobs": [
                {
                    "id": "applyguy-grad-1",
                    "company": "Acme",
                    "title": "Software Engineer, New Grad",
                    "location": "SF, CA",
                    "eligibility": "New Grad",
                    "matchKind": "exact",
                    "listingUrl": "https://jobs.lever.co/acme/123",
                    "url": "https://applyguy.example/jobs/acme-grad",
                },
                {
                    "id": "applyguy-pm-grad-1",
                    "company": "Acme",
                    "title": "Product Manager, New Grad",
                    "location": "San Francisco, CA",
                    "eligibility": "Entry Level",
                    "matchKind": "exact",
                    "listingUrl": "https://jobs.example.test/acme/pm-grad",
                    "url": "https://applyguy.example/jobs/acme-pm-grad",
                },
                {
                    "id": "applyguy-senior-1",
                    "company": "Acme",
                    "title": "Software Engineer",
                    "location": "San Francisco, CA",
                    "eligibility": "Experienced",
                    "listingUrl": "https://jobs.example.test/acme/senior",
                    "url": "https://applyguy.example/jobs/acme-senior",
                },
            ]
        }
    )

    parsed = parse_applyguy_source(document, SOURCE_BY_ID["applyguy_new_grad"])

    assert len(parsed.jobs) == 1
    job = parsed.jobs[0]
    assert job.application_url == "https://jobs.lever.co/acme/123"
    assert job.source_url == "https://applyguy.example/jobs/acme-grad"
    assert job.source_metadata["eligibility"] == "New Grad"
    assert job.source_metadata["matchKind"] == "exact"
    assert location_matches(job.location)


def test_simplify_active_swe_rows_handle_continuations_and_prefer_direct_apply_url() -> None:
    document = """
# Summer internships

## Software Engineering Internship Roles

<table>
  <thead>
    <tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th><th>Age</th></tr>
  </thead>
  <tbody>
    <tr>
      <td><a href="https://www.acme.example">\U0001f525\U0001f6c2\U0001f1fa\U0001f1f8\U0001f393 Acme</a></td>
      <td>Software Engineering Intern</td>
      <td><details><summary><strong>2 locations</strong></summary>New York, NY<br />SF, CA</details></td>
      <td>
        <a href="https://simplify.jobs/p/acme-intern"><img alt="Apply" /></a>
        <a href="https://jobs.ashbyhq.com/acme/req-123"><img alt="Apply" /></a>
      </td>
      <td>1d</td>
    </tr>
    <tr>
      <td>\u21b3</td>
      <td>Software Engineering Intern, Infrastructure</td>
      <td>SF, CA | Remote</td>
      <td>
        <a href="https://simplify.jobs/p/acme-infra"><img alt="Apply" /></a>
        <a href="https://jobs.ashbyhq.com/acme/req-456"><img alt="Apply" /></a>
      </td>
      <td>2d</td>
    </tr>
  </tbody>
</table>

## Software Engineering Internship Roles - Inactive

<table>
  <tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th><th>Age</th></tr>
  <tr><td>Inactive Co</td><td>Software Intern</td><td>SF, CA</td><td><a href="https://jobs.example.test/inactive">Apply</a></td><td>9d</td></tr>
</table>
"""

    parsed = parse_simplify_source(document, SOURCE_BY_ID["simplify_summer_2027"])

    assert len(parsed.jobs) == 2  # Two rows, not one extra record per Apply link.
    first, continuation = parsed.jobs
    assert first.company == "Acme"
    assert continuation.company == "Acme"
    assert first.application_url == "https://jobs.ashbyhq.com/acme/req-123"
    assert continuation.application_url == "https://jobs.ashbyhq.com/acme/req-456"
    assert first.source_url == "https://simplify.jobs/p/acme-intern"
    assert continuation.source_url == "https://simplify.jobs/p/acme-infra"
    assert "simplify.jobs" not in first.application_url
    assert "locations" not in first.location
    assert location_matches(first.location)
    assert location_matches(continuation.location)

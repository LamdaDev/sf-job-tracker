from __future__ import annotations

from src.renderer import GENERATED_WARNING, render_jobs_markdown
from src.storage import empty_seen_state
from src.tracker import apply_current_jobs
from .test_tracker import make_job


def test_renderer_has_one_canonical_row_with_visible_sources_and_history() -> None:
    active = make_job("https://jobs.example.test/active", company="Active Co")
    duplicate_source = make_job(
        active.application_url,
        source_id="applyguy_new_grad",
        source_label="ApplyGuy",
        company="Active Co",
    )
    inactive = make_job("https://jobs.example.test/inactive", company="Inactive Co")
    baseline = apply_current_jobs(
        empty_seen_state(),
        [active, duplicate_source, inactive],
        successful_source_ids={active.source_id, duplicate_source.source_id},
        timestamp="2026-08-16T12:00:00Z",
    )
    state = apply_current_jobs(
        baseline.state,
        [active, duplicate_source],
        successful_source_ids={active.source_id, duplicate_source.source_id},
        timestamp="2026-08-16T13:00:00Z",
    ).state

    markdown = render_jobs_markdown(state)
    assert markdown.startswith(GENERATED_WARNING)
    assert "# San Francisco SWE Job Tracker" in markdown
    assert "including the explicit SF aliases" in markdown
    assert "## Active Jobs (1)" in markdown
    assert "## Historical / Closed or Removed Jobs (1)" in markdown
    assert markdown.count("Active Co") == 1
    assert "SpeedyApply, ApplyGuy" in markdown
    assert "[Apply](<https://jobs.example.test/active>)" in markdown

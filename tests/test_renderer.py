from __future__ import annotations

from src.renderer import GENERATED_WARNING, render_jobs_markdown
from src.storage import empty_seen_state
from src.tracker import apply_current_jobs
from .test_tracker import make_job


def test_renderer_includes_required_job_data_and_history_sections() -> None:
    active = make_job("https://jobs.example.test/active", company="Active Co")
    inactive = make_job("https://jobs.example.test/inactive", company="Inactive Co")
    baseline = apply_current_jobs(
        empty_seen_state(), [active, inactive], timestamp="2026-08-16T12:00:00Z"
    )
    state = apply_current_jobs(baseline.state, [active], timestamp="2026-08-16T13:00:00Z").state

    markdown = render_jobs_markdown(state)

    assert markdown.startswith(GENERATED_WARNING)
    assert "# San Francisco Bay Area SWE Job Tracker" in markdown
    assert "roughly one hour away by car" in markdown
    assert "## Active Jobs (1)" in markdown
    assert "## Historical / Closed or Removed Jobs (1)" in markdown
    assert "Active Co" in markdown
    assert "Inactive Co" in markdown
    assert "New Grad" in markdown
    assert "Other" in markdown
    assert "San Francisco, CA" in markdown
    assert "[Apply](<https://jobs.example.test/active>)" in markdown

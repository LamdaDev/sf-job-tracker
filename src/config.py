"""Central configuration for the job tracker."""

from __future__ import annotations

from dataclasses import dataclass


UPSTREAM_REPOSITORY = "speedyapply/2027-SWE-College-Jobs"
UPSTREAM_API_URL = f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}/commits/main"
RAW_CONTENT_BASE_URL = f"https://raw.githubusercontent.com/{UPSTREAM_REPOSITORY}"

# The required default intentionally uses a substring match.  This includes
# values such as "San Francisco, CA +1" and "South San Francisco, CA".
TARGET_LOCATION = "San Francisco, CA"
TARGET_LOCATION_LABEL = "San Francisco"

REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3
USER_AGENT = "sf-job-tracker/1.0 (+https://github.com/LamdaDev/sf-job-tracker)"

STATE_SCHEMA_VERSION = 1
CURRENT_JOBS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SourceConfig:
    """A public SpeedyApply Markdown file and the job type it represents."""

    job_type: str
    source_file: str


SOURCES = (
    SourceConfig(job_type="Internship", source_file="README.md"),
    SourceConfig(job_type="New Grad", source_file="NEW_GRAD_USA.md"),
)

# These exact generated-table boundaries are deliberately independent from
# headings and navigation anchors, which may be changed by the upstream repo.
CATEGORY_MARKERS = {
    "FAANG+": ("<!-- TABLE_FAANG_START -->", "<!-- TABLE_FAANG_END -->"),
    "Quant": ("<!-- TABLE_QUANT_START -->", "<!-- TABLE_QUANT_END -->"),
    "Other": ("<!-- TABLE_START -->", "<!-- TABLE_END -->"),
}

CATEGORIES = tuple(CATEGORY_MARKERS)

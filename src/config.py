"""Central configuration for the San Francisco SWE job tracker."""

from __future__ import annotations

from dataclasses import dataclass


# These legacy constants remain useful to callers that only need the original
# SpeedyApply repository. Multi-source fetching uses ``SOURCES`` below.
UPSTREAM_REPOSITORY = "speedyapply/2027-SWE-College-Jobs"
UPSTREAM_API_URL = f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}/commits/main"
RAW_CONTENT_BASE_URL = f"https://raw.githubusercontent.com/{UPSTREAM_REPOSITORY}"

TARGET_LOCATION = "San Francisco, CA"
TARGET_LOCATION_LABEL = "San Francisco"
TARGET_LOCATION_DESCRIPTION = (
    "San Francisco, CA (including the explicit SF aliases and South San Francisco)"
)

# These are deliberately a *San Francisco* matcher, not a Bay Area radius.
# Keep abbreviations as complete location tokens: matching a bare ``sf``
# substring would make ordinary prose and unrelated locations false positives.
SAN_FRANCISCO_LOCATION_ALIASES = (
    "san francisco",
    "san francisco ca",
    "san francisco california",
    "sf",
    "sf ca",
    "sf california",
    "s f",
    "s f ca",
    "s f california",
)

# A small presentation-only qualifier allowlist covers common upstream forms
# such as ``San Francisco (Hybrid), CA`` without accepting regional phrases.
SAN_FRANCISCO_PLACE_QUALIFIERS = ("county", "hybrid", "remote", "onsite", "on site", "in office")

REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 3
USER_AGENT = "sf-job-tracker/2.0 (+https://github.com/LamdaDev/sf-job-tracker)"

STATE_SCHEMA_VERSION = 2
CURRENT_JOBS_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class SourceConfig:
    """One immutable public document and the adapter that understands it."""

    id: str
    label: str
    repository: str
    ref: str
    source_file: str
    job_type: str
    parser_id: str


SOURCES = (
    SourceConfig(
        id="speedyapply_internships",
        label="SpeedyApply",
        repository="speedyapply/2027-SWE-College-Jobs",
        ref="main",
        source_file="README.md",
        job_type="Internship",
        parser_id="speedyapply",
    ),
    SourceConfig(
        id="speedyapply_new_grad",
        label="SpeedyApply",
        repository="speedyapply/2027-SWE-College-Jobs",
        ref="main",
        source_file="NEW_GRAD_USA.md",
        job_type="New Grad",
        parser_id="speedyapply",
    ),
    SourceConfig(
        id="applyguy_internships",
        label="ApplyGuy",
        repository="ApplyGuy/2027-Internships",
        ref="main",
        source_file="data/internships.json",
        job_type="Internship",
        parser_id="applyguy_internships",
    ),
    SourceConfig(
        id="applyguy_new_grad",
        label="ApplyGuy",
        repository="ApplyGuy/2027-New-Grad-Jobs",
        ref="main",
        source_file="data/new-grad-jobs.json",
        job_type="New Grad",
        parser_id="applyguy_new_grad",
    ),
    SourceConfig(
        id="simplify_summer_2027",
        label="Simplify",
        repository="SimplifyJobs/Summer2027-Internships",
        ref="dev",
        source_file="README.md",
        job_type="Internship",
        parser_id="simplify",
    ),
)

if len({source.id for source in SOURCES}) != len(SOURCES):  # pragma: no cover - import-time guard
    raise RuntimeError("Every configured source must have a unique stable id")

SOURCE_BY_ID = {source.id: source for source in SOURCES}
SOURCE_PRIORITY = {source.id: index for index, source in enumerate(SOURCES)}
LEGACY_SPEEDY_SOURCE_IDS = ("speedyapply_internships", "speedyapply_new_grad")

# These exact generated-table boundaries are deliberately independent from
# headings and navigation anchors, which may be changed by the upstream repo.
CATEGORY_MARKERS = {
    "FAANG+": ("<!-- TABLE_FAANG_START -->", "<!-- TABLE_FAANG_END -->"),
    "Quant": ("<!-- TABLE_QUANT_START -->", "<!-- TABLE_QUANT_END -->"),
    "Other": ("<!-- TABLE_START -->", "<!-- TABLE_END -->"),
}

CATEGORIES = tuple(CATEGORY_MARKERS)

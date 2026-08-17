"""Central configuration for the job tracker."""

from __future__ import annotations

from dataclasses import dataclass


UPSTREAM_REPOSITORY = "speedyapply/2027-SWE-College-Jobs"
UPSTREAM_API_URL = f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}/commits/main"
RAW_CONTENT_BASE_URL = f"https://raw.githubusercontent.com/{UPSTREAM_REPOSITORY}"

# Location matching is intentionally deterministic and based only on the
# text SpeedyApply displays. A live one-hour driving-time check would require
# an address, routing API credentials, and a traffic-time decision for every
# row. This curated scope instead covers cities that are roughly one hour from
# San Francisco in favorable traffic, including the user's requested South
# Bay and East Bay cities.
TARGET_LOCATION_LABEL = "San Francisco Bay Area"
TARGET_LOCATION_DESCRIPTION = (
    "an explicit San Francisco Bay Area city and alias policy for locations "
    "roughly one hour away by car in favorable traffic"
)

# Bump this when changing the allowlist below. Existing history from an older
# scope is silently rebaselined once so an intentional coverage expansion does
# not create one giant notification for roles that were already listed.
LOCATION_SCOPE_VERSION = "sf-bay-area-roughly-one-hour-v1"

# State spellings accepted after a city/region name. Comparison text is
# normalized before matching, so punctuation and whitespace variations work.
CALIFORNIA_LOCATION_TOKENS = ("ca", "calif", "california")

# Keep this list explicit rather than matching all of California or every
# nine-county Bay Area city. The outermost cities are only approximately within
# an hour under favorable traffic; edit this list to tune the personal radius.
BAY_AREA_CITY_NAMES = (
    # San Francisco and the Peninsula.
    "san francisco",
    "south san francisco",
    "daly city",
    "colma",
    "brisbane",
    "pacifica",
    "san bruno",
    "millbrae",
    "burlingame",
    "hillsborough",
    "san mateo",
    "foster city",
    "belmont",
    "san carlos",
    "redwood city",
    "atherton",
    "menlo park",
    "east palo alto",
    "palo alto",
    "woodside",
    "portola valley",
    "half moon bay",
    # South Bay.
    "mountain view",
    "los altos",
    "los altos hills",
    "sunnyvale",
    "cupertino",
    "santa clara",
    "san jose",
    "milpitas",
    "campbell",
    "los gatos",
    "saratoga",
    # East Bay.
    "oakland",
    "emeryville",
    "alameda",
    "berkeley",
    "albany",
    "el cerrito",
    "richmond",
    "san pablo",
    "pinole",
    "hercules",
    "piedmont",
    "san leandro",
    "castro valley",
    "hayward",
    "union city",
    "newark",
    "fremont",
    "orinda",
    "lafayette",
    "moraga",
    "walnut creek",
    "pleasant hill",
    "concord",
    "martinez",
    "dublin",
    "pleasanton",
    "san ramon",
    "danville",
    # Marin and the nearest North Bay cities.
    "sausalito",
    "mill valley",
    "tiburon",
    "belvedere",
    "corte madera",
    "larkspur",
    "ross",
    "san anselmo",
    "fairfax",
    "san rafael",
    "novato",
    "vallejo",
    "benicia",
)

# Short forms that still require a California state token. Do not add bare
# "SF" or "Bay Area" substring matching: it would produce avoidable false
# positives in free-form location text.
BAY_AREA_CITY_ALIASES = ("sf", "s f", "san fran", "south sf")

# A few source formats insert a descriptor between a city and its state, such
# as "San Francisco (Hybrid), CA". These are accepted without making the
# matcher fuzzy or treating arbitrary intervening words as location text.
LOCATION_PLACE_QUALIFIERS = ("county", "hybrid", "remote", "onsite", "on site", "in office")

# These names identify the San Francisco region without a separate state
# token, unlike the generic regional phrases below.
BAY_AREA_UNAMBIGUOUS_REGION_ALIASES = (
    "san francisco bay area",
    "sf bay area",
    "s f bay area",
)
BAY_AREA_REGION_ALIASES = ("bay area", "silicon valley")

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

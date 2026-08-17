"""URL identity and conservative cross-source job aggregation."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import SOURCE_PRIORITY
from .models import CanonicalJob, Job


_AGGREGATOR_HOSTS = ("applyguy.ai", "simplify.jobs")
_TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "gh_src",
    "ref",
    "referrer",
    "source",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}
_ALWAYS_SAFE_TRACKING_PARAMETERS = {"fbclid", "gclid", "gh_src"}
_PRESENTATION_QUERY_HOST_SUFFIXES = (
    "ashbyhq.com",
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "rippling.com",
    "smartrecruiters.com",
    "icims.com",
    "amazon.jobs",
    "lifeattiktok.com",
    "jobs.bytedance.com",
)


@dataclass(frozen=True)
class UrlIdentity:
    """A URL's reusable identity plus its safe canonical display form."""

    canonical_id: str
    canonical_url: str
    direct: bool
    stable_requisition: bool


def _normalise_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain.casefold()).split())


def _normalised_url(url: str, *, remove_provider_tracking: bool) -> str:
    """Normalize safe URL representation differences without losing identity."""

    parsed = urlsplit(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return url.strip()
    hostname = (parsed.hostname or "").casefold()
    port = f":{parsed.port}" if parsed.port else ""
    netloc = hostname + port
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")

    parameters: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        always_safe_tracking = lowered.startswith("utm_") or lowered in _ALWAYS_SAFE_TRACKING_PARAMETERS
        is_tracking = always_safe_tracking or lowered in _TRACKING_PARAMETERS
        # ``embed`` is known non-identity presentation state on Ashby. Do not
        # remove it generically, because another vendor could use it as a key.
        known_ats_host = any(hostname.endswith(suffix) for suffix in _PRESENTATION_QUERY_HOST_SUFFIXES)
        if always_safe_tracking:
            continue
        if remove_provider_tracking and (is_tracking or lowered == "embed"):
            continue
        if known_ats_host and lowered in {"ref", "referrer", "source", "embed"}:
            continue
        parameters.append((key, value))
    query = urlencode(sorted(parameters), doseq=True)
    return urlunsplit((parsed.scheme.casefold(), netloc, path, query, ""))


def _path_parts(url: str) -> tuple[str, list[str]]:
    parsed = urlsplit(url)
    return (parsed.hostname or "").casefold(), [part for part in parsed.path.split("/") if part]


def inspect_job_url(url: str) -> UrlIdentity:
    """Return stable ATS identity when it can be extracted safely.

    Direct URLs without a recognised requisition format remain distinct by a
    conservative normalized URL. Aggregator wrappers intentionally have no
    direct identity so they may use the exact fallback fingerprint only when
    there is no conflicting ATS evidence.
    """

    clean = url.strip()
    normalized = _normalised_url(clean, remove_provider_tracking=False)
    hostname, parts = _path_parts(normalized)
    direct = bool(hostname) and not any(
        hostname == aggregator or hostname.endswith(f".{aggregator}")
        for aggregator in _AGGREGATOR_HOSTS
    )

    def stable(provider: str, *items: str) -> UrlIdentity:
        cleaned = [item.casefold() for item in items if item]
        return UrlIdentity(
            canonical_id=f"{provider}:{':'.join(cleaned)}",
            canonical_url=_normalised_url(clean, remove_provider_tracking=True),
            direct=True,
            stable_requisition=True,
        )

    if hostname.endswith("ashbyhq.com") and len(parts) >= 2:
        # jobs.ashbyhq.com/{company}/{job-id}[/application]
        company, job_id = parts[0], parts[1]
        if company and job_id not in {"application", "embed"}:
            return stable("ashby", company, job_id)

    if hostname.endswith("greenhouse.io"):
        # boards.greenhouse.io/{company}/jobs/{id} and the job-boards variant.
        if "jobs" in parts:
            index = parts.index("jobs")
            if index >= 1 and len(parts) > index + 1:
                return stable("greenhouse", parts[index - 1], parts[index + 1])

    if hostname.endswith("lever.co") and len(parts) >= 2:
        return stable("lever", parts[0], parts[1])

    if "myworkdayjobs.com" in hostname:
        # Workday job paths end in a requisition-bearing slug. These often
        # contain underscores (for example ``Role_R12345-2``), so scanning for
        # a simple alphanumeric segment can accidentally select the preceding
        # location and merge distinct requisitions. The terminal path segment
        # after ``/job/`` is the conservative stable unit; include tenant host
        # because a requisition is not globally unique across Workday tenants.
        job_indexes = [index for index, part in enumerate(parts) if part.casefold() == "job"]
        if job_indexes and len(parts) > job_indexes[-1] + 1:
            requisition = parts[-1]
            if len(requisition) >= 5 and any(character.isdigit() for character in requisition):
                return stable("workday", hostname, requisition)

    if hostname == "ats.rippling.com" and "jobs" in parts:
        index = parts.index("jobs")
        if index >= 1 and len(parts) > index + 1:
            return stable("rippling", parts[index - 1], parts[index + 1])

    if hostname.endswith("lifeattiktok.com") or hostname.endswith("jobs.bytedance.com"):
        numeric = next((part for part in reversed(parts) if re.fullmatch(r"\d{5,}", part)), None)
        if numeric:
            return stable("tiktok", hostname, numeric)

    if hostname.endswith("amazon.jobs") and "jobs" in parts:
        index = parts.index("jobs")
        if len(parts) > index + 1:
            return stable("amazon", parts[index + 1])

    if direct:
        canonical_url = _normalised_url(clean, remove_provider_tracking=False)
        return UrlIdentity(
            canonical_id=f"url:{canonical_url}",
            canonical_url=canonical_url,
            direct=True,
            stable_requisition=False,
        )
    return UrlIdentity(
        canonical_id=f"url:{normalized}",
        canonical_url=normalized,
        direct=False,
        stable_requisition=False,
    )


def canonicalize_job_url(url: str) -> str:
    """Return the stable canonical identity used for a single application URL."""

    return inspect_job_url(url).canonical_id


def fallback_fingerprint_for_fields(
    company: str, position: str, job_type: str, location: str, season: str | None
) -> str:
    """Return an exact normalized fallback key shared by jobs and state records."""

    payload = "\x1f".join(
        (
            _normalise_text(company),
            _normalise_text(position),
            _normalise_text(job_type),
            _normalise_text(location),
            _normalise_text(season or ""),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fallback_fingerprint(job: Job) -> str:
    """Return a conservative identity only for observations lacking direct URLs."""

    return fallback_fingerprint_for_fields(
        job.company, job.position, job.job_type, job.location, job.season
    )


def _observation_sort_key(job: Job) -> tuple[int, int, str, str]:
    identity = inspect_job_url(job.application_url)
    # Stable employer requisition > direct employer URL > aggregator wrapper.
    strength = 0 if identity.stable_requisition else 1 if identity.direct else 2
    return (strength, SOURCE_PRIORITY.get(job.source_id, len(SOURCE_PRIORITY)), job.application_url, job.source_id)


def _best(observations: list[Job], attribute: str, *, prefer_longest: bool = False) -> str | None:
    candidates = [job for job in observations if isinstance(getattr(job, attribute), str) and getattr(job, attribute)]
    if not candidates:
        return None
    ordered = sorted(candidates, key=_observation_sort_key)
    if not prefer_longest:
        return getattr(ordered[0], attribute)
    # Prefer a clean, richer string while keeping direct-source and config
    # priority as deterministic tie breakers.
    return getattr(
        min(
            candidates,
            key=lambda job: (-len(str(getattr(job, attribute))), *_observation_sort_key(job)),
        ),
        attribute,
    )


def _canonical_from_cluster(canonical_id: str, cluster: list[Job]) -> CanonicalJob:
    ordered = sorted(cluster, key=_observation_sort_key)
    primary = ordered[0]
    # A source should have at most one presence record for a canonical job.
    by_source: dict[str, Job] = {}
    for observation in ordered:
        previous = by_source.get(observation.source_id)
        if previous is None or _observation_sort_key(observation) < _observation_sort_key(previous):
            by_source[observation.source_id] = observation
    source_observations = tuple(
        sorted(by_source.values(), key=lambda job: (SOURCE_PRIORITY.get(job.source_id, len(SOURCE_PRIORITY)), job.source_id))
    )
    aliases = sorted(
        {
            value
            for observation in source_observations
            for value in (observation.application_url, observation.source_url)
            if value
        }
    )
    return CanonicalJob(
        canonical_id=canonical_id,
        company=_best(ordered, "company", prefer_longest=True) or primary.company,
        position=_best(ordered, "position", prefer_longest=True) or primary.position,
        location=_best(ordered, "location", prefer_longest=True) or primary.location,
        salary=_best(ordered, "salary"),
        application_url=primary.application_url,
        age=_best(ordered, "age"),
        category=_best(ordered, "category") or "Unknown",
        job_type=primary.job_type,
        posted=_best(ordered, "posted"),
        season=_best(ordered, "season"),
        observations=source_observations,
        url_aliases=tuple(aliases),
    )


def aggregate_observations(observations: Iterable[Job]) -> tuple[list[CanonicalJob], int]:
    """Collapse observations into canonical requisitions without fuzzy merging.

    Distinct direct/ATS identities always stay distinct. Exact fallback fields
    are used only for aggregator-only observations, or to attach one such
    observation to exactly one non-conflicting direct requisition.
    """

    ordered = sorted(observations, key=lambda job: (*_observation_sort_key(job), job.company, job.position))
    clusters: dict[str, list[Job]] = {}
    direct_fingerprints: dict[str, set[str]] = {}
    fallback_observations: list[Job] = []
    for observation in ordered:
        identity = inspect_job_url(observation.application_url)
        if identity.direct:
            clusters.setdefault(identity.canonical_id, []).append(observation)
            direct_fingerprints.setdefault(fallback_fingerprint(observation), set()).add(identity.canonical_id)
        else:
            fallback_observations.append(observation)

    for observation in fallback_observations:
        fingerprint = fallback_fingerprint(observation)
        candidates = direct_fingerprints.get(fingerprint, set())
        canonical_id = next(iter(candidates)) if len(candidates) == 1 else f"fallback:{fingerprint}"
        clusters.setdefault(canonical_id, []).append(observation)

    canonical_jobs = [
        _canonical_from_cluster(canonical_id, cluster)
        for canonical_id, cluster in sorted(clusters.items())
    ]
    return canonical_jobs, len(ordered) - len(canonical_jobs)

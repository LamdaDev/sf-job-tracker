"""Reliable, dependency-free retrieval of immutable multi-source snapshots."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import (
    REQUEST_RETRIES,
    REQUEST_TIMEOUT_SECONDS,
    SOURCES,
    UPSTREAM_REPOSITORY,
    USER_AGENT,
    SourceConfig,
)


LOGGER = logging.getLogger(__name__)


class UpstreamFetchError(RuntimeError):
    """Raised when no usable upstream snapshot can be retrieved."""


@dataclass(frozen=True)
class FetchedSnapshot:
    """Fetched documents, immutable revisions, and recoverable source errors.

    ``commit_sha`` remains for compatibility with the original one-repository
    test seam. Multi-source runs use ``revisions`` keyed by ``repo@ref``.
    """

    commit_sha: str
    documents: Mapping[SourceConfig, str]
    revisions: Mapping[str, str] = field(default_factory=dict)
    errors: Mapping[str, str] = field(default_factory=dict)

    def document_for(self, source: SourceConfig) -> str | None:
        return self.documents.get(source)


def fetch_text(url: str, *, headers: Mapping[str, str] | None = None) -> str:
    """Fetch UTF-8 text with a timeout, retries, and useful failures."""

    request_headers = {"User-Agent": USER_AGENT, "Accept": "text/plain, */*"}
    if headers:
        request_headers.update(headers)

    last_error: Exception | None = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            request = Request(url, headers=request_headers)
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
                status = response.getcode()
                if status != 200:
                    raise UpstreamFetchError(f"GET {url} returned HTTP {status}")
                body = response.read()
            return body.decode("utf-8")
        except HTTPError as error:
            last_error = error
            if error.code < 500:
                break
            LOGGER.warning("Attempt %s/%s to fetch %s failed with HTTP %s", attempt, REQUEST_RETRIES, url, error.code)
        except (URLError, OSError, TimeoutError, UnicodeDecodeError) as error:
            last_error = error
            LOGGER.warning("Attempt %s/%s to fetch %s failed: %s", attempt, REQUEST_RETRIES, url, error)
        if attempt < REQUEST_RETRIES:
            time.sleep(attempt)

    detail = str(last_error) if last_error else "unknown network error"
    raise UpstreamFetchError(f"Unable to fetch {url}: {detail}") from last_error


def commit_api_url(repository: str, ref: str) -> str:
    """Return GitHub's immutable commit-resolution endpoint for a source."""

    return f"https://api.github.com/repos/{repository}/commits/{quote(ref, safe='')}"


def resolve_commit_sha(repository: str = UPSTREAM_REPOSITORY, ref: str = "main") -> str:
    """Resolve a repository ref once before fetching its configured files."""

    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        payload = json.loads(fetch_text(commit_api_url(repository, ref), headers=headers))
    except (json.JSONDecodeError, UpstreamFetchError) as error:
        raise UpstreamFetchError(f"Could not resolve {repository}@{ref}: {error}") from error
    sha = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(sha, str) or len(sha) < 7:
        raise UpstreamFetchError(f"Commit response for {repository}@{ref} did not contain a valid SHA")
    return sha


def raw_source_url(commit_sha: str, source: SourceConfig) -> str:
    """Build an immutable raw-content URL for a configured source."""

    return f"https://raw.githubusercontent.com/{source.repository}/{commit_sha}/{source.source_file}"


def _source_groups() -> dict[tuple[str, str], list[SourceConfig]]:
    groups: dict[tuple[str, str], list[SourceConfig]] = {}
    for source in SOURCES:
        groups.setdefault((source.repository, source.ref), []).append(source)
    return groups


def fetch_upstream_sources() -> FetchedSnapshot:
    """Fetch each source at an immutable per-repository revision.

    Independent providers cannot share one global commit. A provider failure is
    represented in ``errors`` so successful sources can still update safely;
    the tracker preserves membership state for failed sources.
    """

    documents: dict[SourceConfig, str] = {}
    revisions: dict[str, str] = {}
    errors: dict[str, str] = {}
    for (repository, ref), sources in _source_groups().items():
        key = f"{repository}@{ref}"
        try:
            commit_sha = resolve_commit_sha(repository, ref)
        except UpstreamFetchError as error:
            message = str(error)
            LOGGER.error("Could not fetch source group %s: %s", key, message)
            errors.update({source.id: message for source in sources})
            continue
        revisions[key] = commit_sha
        for source in sources:
            try:
                LOGGER.info("Fetching %s at %s", source.id, commit_sha[:12])
                documents[source] = fetch_text(raw_source_url(commit_sha, source))
            except UpstreamFetchError as error:
                message = str(error)
                LOGGER.error("Could not fetch %s: %s", source.id, message)
                errors[source.id] = message
    if not documents:
        raise UpstreamFetchError("All configured upstream sources failed to fetch")
    legacy_sha = revisions.get(f"{UPSTREAM_REPOSITORY}@main", "")
    return FetchedSnapshot(
        commit_sha=legacy_sha,
        documents=documents,
        revisions=revisions,
        errors=errors,
    )

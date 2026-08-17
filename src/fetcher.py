"""Reliable, dependency-free retrieval of the public upstream snapshot."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import (
    RAW_CONTENT_BASE_URL,
    REQUEST_RETRIES,
    REQUEST_TIMEOUT_SECONDS,
    SOURCES,
    UPSTREAM_API_URL,
    USER_AGENT,
    SourceConfig,
)

LOGGER = logging.getLogger(__name__)


class UpstreamFetchError(RuntimeError):
    """Raised when a complete, valid upstream snapshot cannot be retrieved."""


@dataclass(frozen=True)
class FetchedSnapshot:
    """The two source documents fetched from one immutable upstream revision."""

    commit_sha: str
    documents: Mapping[SourceConfig, str]


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
            # A client error is deterministic until the upstream is fixed; do
            # not add a pointless retry delay.
            if error.code < 500:
                break
            LOGGER.warning(
                "Attempt %s/%s to fetch %s failed with HTTP %s",
                attempt,
                REQUEST_RETRIES,
                url,
                error.code,
            )
        except (URLError, OSError, TimeoutError, UnicodeDecodeError) as error:
            last_error = error
            LOGGER.warning(
                "Attempt %s/%s to fetch %s failed: %s",
                attempt,
                REQUEST_RETRIES,
                url,
                error,
            )

        if attempt < REQUEST_RETRIES:
            time.sleep(attempt)

    detail = str(last_error) if last_error else "unknown network error"
    raise UpstreamFetchError(f"Unable to fetch {url}: {detail}") from last_error


def resolve_commit_sha() -> str:
    """Resolve upstream main once so both files come from the same revision."""

    try:
        payload = json.loads(
            fetch_text(
                UPSTREAM_API_URL,
                headers={"Accept": "application/vnd.github+json"},
            )
        )
    except (json.JSONDecodeError, UpstreamFetchError) as error:
        raise UpstreamFetchError(f"Could not resolve upstream main commit: {error}") from error

    sha = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(sha, str) or len(sha) < 7:
        raise UpstreamFetchError("Upstream commit response did not contain a valid SHA")
    return sha


def raw_source_url(commit_sha: str, source: SourceConfig) -> str:
    """Build an immutable raw-content URL for a configured source."""

    return f"{RAW_CONTENT_BASE_URL}/{commit_sha}/{source.source_file}"


def fetch_upstream_sources() -> FetchedSnapshot:
    """Fetch every configured USA source from one resolved commit SHA."""

    commit_sha = resolve_commit_sha()
    documents: dict[SourceConfig, str] = {}
    for source in SOURCES:
        url = raw_source_url(commit_sha, source)
        LOGGER.info("Fetching %s from upstream commit %s", source.source_file, commit_sha[:12])
        documents[source] = fetch_text(url)
    return FetchedSnapshot(commit_sha=commit_sha, documents=documents)


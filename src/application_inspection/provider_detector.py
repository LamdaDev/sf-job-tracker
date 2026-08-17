"""Hostname-based, deliberately non-brittle ATS detection."""

from __future__ import annotations

from urllib.parse import urlsplit

from .models import ApplicationProvider


def detect_application_provider(url: str) -> ApplicationProvider:
    """Identify a public ATS from its hostname, otherwise use ``generic``.

    This only selects an inspection strategy.  It is intentionally not part of
    canonical job identity, which is owned by :mod:`src.canonical`.
    """

    try:
        hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
    except (TypeError, ValueError):
        return ApplicationProvider.GENERIC
    if hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io"):
        return ApplicationProvider.GREENHOUSE
    if hostname == "ashbyhq.com" or hostname.endswith(".ashbyhq.com"):
        return ApplicationProvider.ASHBY
    if hostname == "lever.co" or hostname.endswith(".lever.co"):
        return ApplicationProvider.LEVER
    if hostname == "myworkdayjobs.com" or hostname.endswith(".myworkdayjobs.com"):
        return ApplicationProvider.WORKDAY
    return ApplicationProvider.GENERIC

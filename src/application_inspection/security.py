"""Small safety guards shared by public, read-only inspectors."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


_SENSITIVE_KEY_PARTS = (
    "auth",
    "cookie",
    "csrf",
    "localstorage",
    "password",
    "secret",
    "session",
    "token",
)


def is_safe_public_http_url(url: str) -> bool:
    """Allow only ordinary public HTTP(S) destinations.

    Application URLs arrive from public job feeds, but this inexpensive guard
    still rejects filesystem/data schemes and obvious local/private targets.
    It deliberately does not resolve DNS, avoiding a second network operation
    and keeping this module deterministic in tests.
    """

    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
    except (TypeError, ValueError):
        return False
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        return False
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def is_sensitive_key(key: object) -> bool:
    """Identify metadata keys that must not enter public scan state."""

    lowered = str(key).casefold().replace("_", "")
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def safe_error_message(error: BaseException, *, stage: str) -> str:
    """Expose a concise diagnostic without preserving exception payloads."""

    return f"{type(error).__name__} while {stage}."

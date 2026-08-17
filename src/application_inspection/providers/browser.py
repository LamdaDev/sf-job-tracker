"""Lazy, read-only browser inspection for public dynamic application forms."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..models import ApplicationProvider, ApplicationScanResult, ScanStatus
from ..security import is_safe_public_http_url, safe_error_message
from .generic_html import inspect_static_html


class PlaywrightUnavailable(RuntimeError):
    """The optional browser dependency or its Chromium runtime is unavailable."""


def _default_playwright_factory() -> Any:
    """Import Playwright only when a dynamic scan is actually requested."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:  # pragma: no cover - depends on optional dependency
        raise PlaywrightUnavailable("Playwright is not installed") from error
    return sync_playwright()


class BrowserScanner:
    """Inspect only the initially visible page, without any form interaction.

    The implementation intentionally has no click/fill/select/upload APIs.
    It opens a new ephemeral context, reads ``page.content()``, and closes the
    context/browser in ``finally`` even if navigation or parsing fails.
    """

    def __init__(
        self,
        *,
        timeout_seconds: int = 30,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.timeout_seconds = max(1, int(timeout_seconds))
        self._playwright_factory = playwright_factory or _default_playwright_factory

    def scan(
        self,
        *,
        canonical_job_id: str,
        application_url: str,
        provider: ApplicationProvider,
        scanned_at: str | None = None,
    ) -> ApplicationScanResult:
        """Navigate once and inspect the rendered first page, never mutate it."""

        if not is_safe_public_http_url(application_url):
            return ApplicationScanResult(
                canonical_job_id=canonical_job_id,
                provider=provider,
                application_url=application_url,
                status=ScanStatus.UNAVAILABLE,
                completeness_reason="Application URL is not a safe public HTTP(S) URL.",
                scanned_at=scanned_at,
                metadata={"inspection_method": "browser"},
            )

        browser: Any | None = None
        context: Any | None = None
        try:
            with self._playwright_factory() as playwright:
                browser = playwright.chromium.launch(headless=True)
                # No persistent profile, cookies, or user credentials.
                context = browser.new_context()
                page = context.new_page()
                timeout_ms = self.timeout_seconds * 1000
                page.set_default_navigation_timeout(timeout_ms)
                page.set_default_timeout(timeout_ms)
                # This is the only browser interaction: open the supplied
                # public page.  We never click, type, submit, upload, or log in.
                response = page.goto(application_url, wait_until="domcontentloaded", timeout=timeout_ms)
                html = page.content()
                response_status = response.status if response is not None else None
                if response_status in {404, 410}:
                    return ApplicationScanResult(
                        canonical_job_id=canonical_job_id,
                        provider=provider,
                        application_url=application_url,
                        status=ScanStatus.UNAVAILABLE,
                        completeness_reason="Application appears to be closed.",
                        scanned_at=scanned_at,
                        http_status=response_status,
                        metadata={"inspection_method": "browser"},
                    )
                result = inspect_static_html(
                    canonical_job_id=canonical_job_id,
                    application_url=application_url,
                    html=html,
                    provider=provider,
                    http_status=response_status,
                    scanned_at=scanned_at,
                )
                # A browser can render client-side controls, but first-page
                # inspection still cannot prove conditional/later steps.
                return ApplicationScanResult(
                    canonical_job_id=result.canonical_job_id,
                    provider=result.provider,
                    application_url=result.application_url,
                    status=result.status,
                    questions=result.questions,
                    completeness_reason=result.completeness_reason,
                    scanned_at=result.scanned_at,
                    http_status=result.http_status,
                    error_type=result.error_type,
                    error_message=result.error_message,
                    metadata={**result.metadata, "inspection_method": "browser"},
                )
        except PlaywrightUnavailable as error:
            return ApplicationScanResult(
                canonical_job_id=canonical_job_id,
                provider=provider,
                application_url=application_url,
                status=ScanStatus.UNSUPPORTED,
                completeness_reason="Browser inspection is unavailable in this runtime.",
                scanned_at=scanned_at,
                error_type=type(error).__name__,
                error_message=safe_error_message(error, stage="starting browser inspection"),
                metadata={"inspection_method": "browser"},
            )
        except Exception as error:  # navigation/browser errors are isolated per job
            return ApplicationScanResult(
                canonical_job_id=canonical_job_id,
                provider=provider,
                application_url=application_url,
                status=ScanStatus.FAILED,
                completeness_reason="Browser inspection failed before a safe form scan could finish.",
                scanned_at=scanned_at,
                error_type=type(error).__name__,
                error_message=safe_error_message(error, stage="running browser inspection"),
                metadata={"inspection_method": "browser"},
            )
        finally:
            # The context can exist even if the page load failed.  Both close
            # calls are best-effort and intentionally do not mask scan status.
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass

"""Provider-agnostic orchestration for safe application inspection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .models import ApplicationProvider, ApplicationScanResult, ScanStatus
from .provider_detector import detect_application_provider
from .providers.browser import BrowserScanner
from .providers.generic_html import (
    FetchedPage,
    PublicFetchError,
    fetch_public_html,
    find_public_apply_link,
    inspect_static_html,
)
from .providers.greenhouse import fetch_greenhouse_job, inspect_greenhouse_application
from .security import is_safe_public_http_url, safe_error_message

if TYPE_CHECKING:  # avoid coupling the package to the tracker at import time
    from ..models import CanonicalJob


def _configured_int(name: str, default: int) -> int:
    """Use tracker configuration when present without making it a dependency."""

    try:
        from .. import config

        value = getattr(config, name, default)
        return max(1, int(value))
    except (ImportError, TypeError, ValueError):
        return default


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _job_identity(job: object, application_url: str | None = None) -> tuple[str, str]:
    canonical_id = getattr(job, "canonical_id", None)
    url = application_url if application_url is not None else getattr(job, "application_url", None)
    if not isinstance(canonical_id, str) or not canonical_id:
        canonical_id = "unknown-canonical-job"
    if not isinstance(url, str) or not url:
        url = "about:blank"
    return canonical_id, url


def failed_scan_result(
    job: object,
    application_url: str | None = None,
    provider: ApplicationProvider | str | None = None,
    error: BaseException | None = None,
    *,
    scanned_at: str | None = None,
    stage: str = "inspecting the application",
) -> ApplicationScanResult:
    """Return a safe, serialisable failure without leaking exception payloads."""

    canonical_job_id, url = _job_identity(job, application_url)
    selected_provider = (
        ApplicationProvider(provider)
        if provider is not None
        else detect_application_provider(url)
    )
    error_type = type(error).__name__ if error is not None else None
    error_message = safe_error_message(error, stage=stage) if error is not None else None
    return ApplicationScanResult(
        canonical_job_id=canonical_job_id,
        provider=selected_provider,
        application_url=url,
        status=ScanStatus.FAILED,
        completeness_reason="Application inspection failed without affecting the job notification.",
        scanned_at=scanned_at,
        error_type=error_type,
        error_message=error_message,
        metadata={"inspection_method": "orchestrator"},
    )


class ApplicationInspector:
    """Select the safest applicable public inspection strategy for one job.

    The class owns no persistence and does not call GitHub.  It is deliberately
    invoked only after canonical new-job detection and Issue creation by the
    tracker integration layer.
    """

    def __init__(
        self,
        *,
        http_fetcher: Callable[..., FetchedPage] = fetch_public_html,
        greenhouse_fetcher: Callable[..., Any] = fetch_greenhouse_job,
        browser_scanner: BrowserScanner | None = None,
        http_timeout_seconds: int | None = None,
        browser_timeout_seconds: int | None = None,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        self.http_fetcher = http_fetcher
        self.greenhouse_fetcher = greenhouse_fetcher
        self.http_timeout_seconds = http_timeout_seconds or _configured_int(
            "APPLICATION_SCAN_HTTP_TIMEOUT_SECONDS", 25
        )
        self.browser_timeout_seconds = browser_timeout_seconds or _configured_int(
            "APPLICATION_SCAN_BROWSER_TIMEOUT_SECONDS", 30
        )
        self.browser_scanner = browser_scanner or BrowserScanner(timeout_seconds=self.browser_timeout_seconds)
        self.now = now

    def _fetch_static_page(self, url: str) -> FetchedPage:
        try:
            return self.http_fetcher(url, timeout_seconds=self.http_timeout_seconds)
        except TypeError as error:
            # A simple deterministic test seam may only accept the URL.
            if "timeout_seconds" not in str(error):
                raise
            return self.http_fetcher(url)

    @staticmethod
    def _is_blocking_static_result(result: ApplicationScanResult) -> bool:
        return result.status is ScanStatus.UNAVAILABLE and result.completeness_reason != (
            "No publicly accessible application form fields were detected."
        )

    def _browser_or_static_fallback(
        self,
        *,
        canonical_job_id: str,
        browser_url: str,
        provider: ApplicationProvider,
        scanned_at: str,
        static_result: ApplicationScanResult,
    ) -> ApplicationScanResult:
        """Prefer the provider browser form, retaining usable static fields on outage."""

        browser_result = self.browser_scanner.scan(
            canonical_job_id=canonical_job_id,
            application_url=browser_url,
            provider=provider,
            scanned_at=scanned_at,
        )
        # Never lose visible static fields merely because the rendered page
        # subsequently presents a CAPTCHA, login, or browser-runtime outage.
        # Those fields make the result partial rather than wholly unavailable.
        if static_result.questions and not browser_result.questions:
            return replace(
                static_result,
                completeness_reason=(
                    "Public static fields were extracted; the rendered application could not expose "
                    "additional fields safely, so this scan is partial."
                ),
                metadata={**static_result.metadata, "browser_fallback_status": browser_result.status.value},
            )
        # ``browser_url`` can be a redirected strategy URL.  Return the
        # canonical/direct application URL carried by the static result, never
        # an arbitrary redirected URL with query state.
        return replace(browser_result, application_url=static_result.application_url)

    def _greenhouse_result(
        self,
        *,
        canonical_job_id: str,
        strategy_url: str,
        displayed_url: str,
        scanned_at: str,
    ) -> ApplicationScanResult:
        """Call Greenhouse's public API but never persist a redirected URL."""

        result = inspect_greenhouse_application(
            canonical_job_id=canonical_job_id,
            application_url=strategy_url,
            fetcher=self.greenhouse_fetcher,
            timeout_seconds=self.http_timeout_seconds,
            scanned_at=scanned_at,
        )
        return replace(result, application_url=displayed_url)

    def inspect(self, job: "CanonicalJob") -> ApplicationScanResult:
        """Inspect a canonical application exactly once, without side effects.

        Every exception is converted to a result.  A caller can therefore
        enrich one issue without a bad provider preventing later jobs or the
        original job-alert notification.
        """

        canonical_job_id, application_url = _job_identity(job)
        provider = detect_application_provider(application_url)
        scanned_at = self.now()
        if not is_safe_public_http_url(application_url):
            return ApplicationScanResult(
                canonical_job_id=canonical_job_id,
                provider=provider,
                application_url=application_url,
                status=ScanStatus.UNAVAILABLE,
                completeness_reason="Application URL is not a safe public HTTP(S) URL.",
                scanned_at=scanned_at,
                metadata={"inspection_method": "orchestrator"},
            )
        try:
            # Greenhouse's documented questions=true endpoint is a stronger,
            # faster source than rendering a browser form.  It is only the one
            # strategy allowed to claim complete form visibility here.
            greenhouse_attempted = False
            if provider is ApplicationProvider.GREENHOUSE:
                greenhouse_attempted = True
                greenhouse = self._greenhouse_result(
                    canonical_job_id=canonical_job_id,
                    strategy_url=application_url,
                    displayed_url=application_url,
                    scanned_at=scanned_at,
                )
                if greenhouse.status is ScanStatus.COMPLETE or greenhouse.questions:
                    return greenhouse
                if greenhouse.status is ScanStatus.UNAVAILABLE and greenhouse.http_status in {401, 403, 404, 410}:
                    return greenhouse

            try:
                page = self._fetch_static_page(application_url)
            except PublicFetchError as error:
                if error.status in {401, 403, 404, 410}:
                    return ApplicationScanResult(
                        canonical_job_id=canonical_job_id,
                        provider=provider,
                        application_url=application_url,
                        status=ScanStatus.UNAVAILABLE,
                        completeness_reason=(
                            "Application appears to be closed or unavailable."
                            if error.status in {404, 410}
                            else "Public application page requires authentication or denies access."
                        ),
                        scanned_at=scanned_at,
                        http_status=error.status,
                        error_type=type(error).__name__,
                        error_message=safe_error_message(error, stage="fetching the public application page"),
                        metadata={"inspection_method": "static_html"},
                    )
                return failed_scan_result(
                    job,
                    application_url,
                    provider,
                    error,
                    scanned_at=scanned_at,
                    stage="fetching the public application page",
                )

            # A public redirect may reveal a known provider.  Do not persist
            # that final URL (it might contain tracking parameters); retain the
            # canonical direct job URL while choosing the appropriate strategy.
            if not is_safe_public_http_url(page.resolved_url):
                return ApplicationScanResult(
                    canonical_job_id=canonical_job_id,
                    provider=provider,
                    application_url=application_url,
                    status=ScanStatus.UNAVAILABLE,
                    completeness_reason="Application redirect did not lead to a safe public HTTP(S) URL.",
                    scanned_at=scanned_at,
                    http_status=page.status,
                    metadata={"inspection_method": "static_html"},
                )
            strategy_url = page.resolved_url
            resolved_provider = detect_application_provider(page.resolved_url)
            if provider is ApplicationProvider.GENERIC and resolved_provider is not ApplicationProvider.GENERIC:
                provider = resolved_provider
            if provider is ApplicationProvider.GREENHOUSE and not greenhouse_attempted:
                greenhouse_attempted = True
                greenhouse = self._greenhouse_result(
                    canonical_job_id=canonical_job_id,
                    strategy_url=strategy_url,
                    displayed_url=application_url,
                    scanned_at=scanned_at,
                )
                if greenhouse.status is ScanStatus.COMPLETE or greenhouse.questions:
                    return greenhouse
                if greenhouse.status is ScanStatus.UNAVAILABLE and greenhouse.http_status in {401, 403, 404, 410}:
                    return greenhouse
            static_result = inspect_static_html(
                canonical_job_id=canonical_job_id,
                application_url=application_url,
                html=page.text,
                provider=provider,
                http_status=page.status,
                scanned_at=scanned_at,
            )
            # Do not launch a browser against authentication, anti-bot, or
            # closed pages.  That would not be useful and could be unsafe.
            if self._is_blocking_static_result(static_result):
                return static_result
            browser_provider = provider in {
                ApplicationProvider.ASHBY,
                ApplicationProvider.LEVER,
                ApplicationProvider.WORKDAY,
            }
            # Generic static extraction is useful on its own.  Known dynamic
            # ATS pages deliberately continue to a rendered first-page scan.
            if static_result.questions and not browser_provider:
                return static_result
            # Some hosted job pages (especially Lever) place the visible form
            # on a clearly marked public /apply anchor.  Read that explicit
            # destination only; never click an opaque button or submit action.
            apply_url = find_public_apply_link(page.text, strategy_url)
            if apply_url and apply_url != strategy_url:
                try:
                    apply_page = self._fetch_static_page(apply_url)
                    apply_provider = detect_application_provider(apply_url)
                    if apply_provider is ApplicationProvider.GENERIC:
                        apply_provider = provider
                    if apply_provider is ApplicationProvider.GREENHOUSE:
                        greenhouse = self._greenhouse_result(
                            canonical_job_id=canonical_job_id,
                            strategy_url=apply_url,
                            displayed_url=apply_url,
                            scanned_at=scanned_at,
                        )
                        if greenhouse.status is ScanStatus.COMPLETE or greenhouse.questions:
                            return greenhouse
                        if greenhouse.status is ScanStatus.UNAVAILABLE and greenhouse.http_status in {401, 403, 404, 410}:
                            return greenhouse
                    apply_static = inspect_static_html(
                        canonical_job_id=canonical_job_id,
                        application_url=apply_url,
                        html=apply_page.text,
                        provider=apply_provider,
                        http_status=apply_page.status,
                        scanned_at=scanned_at,
                    )
                    if self._is_blocking_static_result(apply_static):
                        return apply_static
                    if apply_static.questions and apply_provider not in {
                        ApplicationProvider.ASHBY,
                        ApplicationProvider.LEVER,
                        ApplicationProvider.WORKDAY,
                    }:
                        return apply_static
                    return self._browser_or_static_fallback(
                        canonical_job_id=canonical_job_id,
                        browser_url=apply_url,
                        provider=apply_provider,
                        scanned_at=scanned_at,
                        static_result=apply_static,
                    )
                except PublicFetchError as error:
                    if error.status in {401, 403, 404, 410}:
                        return ApplicationScanResult(
                            canonical_job_id=canonical_job_id,
                            provider=provider,
                            application_url=apply_url,
                            status=ScanStatus.UNAVAILABLE,
                            completeness_reason=(
                                "Application appears to be closed or unavailable."
                                if error.status in {404, 410}
                                else "Public application page requires authentication or denies access."
                            ),
                            scanned_at=scanned_at,
                            http_status=error.status,
                            error_type=type(error).__name__,
                            error_message=safe_error_message(error, stage="following a public application link"),
                            metadata={"inspection_method": "static_html"},
                        )
                    return failed_scan_result(
                        job,
                        apply_url,
                        provider,
                        error,
                        scanned_at=scanned_at,
                        stage="following a public application link",
                    )
            return self._browser_or_static_fallback(
                canonical_job_id=canonical_job_id,
                browser_url=strategy_url,
                provider=provider,
                scanned_at=scanned_at,
                static_result=static_result,
            )
        except Exception as error:  # strict failure isolation for one job
            return failed_scan_result(
                job,
                application_url,
                provider,
                error,
                scanned_at=scanned_at,
                stage="orchestrating application inspection",
            )

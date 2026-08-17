"""Read-only inspection of public job-application forms.

This package deliberately has no dependency on tracker persistence, GitHub, or
job discovery.  Its public entry point accepts an already-deduplicated
``CanonicalJob`` and returns a serialisable description of the public form.
"""

from .inspector import ApplicationInspector, failed_scan_result
from .models import ApplicationProvider, ApplicationQuestion, ApplicationScanResult, ScanStatus
from .provider_detector import detect_application_provider
from .renderer import render_application_scan_block

__all__ = [
    "ApplicationInspector",
    "ApplicationProvider",
    "ApplicationQuestion",
    "ApplicationScanResult",
    "ScanStatus",
    "detect_application_provider",
    "failed_scan_result",
    "render_application_scan_block",
]

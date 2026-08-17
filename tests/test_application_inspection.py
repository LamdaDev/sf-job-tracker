from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.application_inspection import (
    ApplicationInspector,
    ApplicationProvider,
    ApplicationQuestion,
    ApplicationScanResult,
    ScanStatus,
    detect_application_provider,
    render_application_scan_block,
)
from src.application_inspection.providers.browser import BrowserScanner, PlaywrightUnavailable
from src.application_inspection.providers.generic_html import (
    FetchedPage,
    PublicFetchError,
    extract_questions_from_html,
    find_public_apply_link,
    inspect_static_html,
)
from src.application_inspection.providers.greenhouse import (
    GreenhouseJobReference,
    greenhouse_job_reference,
    inspect_greenhouse_application,
    parse_greenhouse_payload,
)
from src.application_inspection.normalizer import normalize_field_type


FIXTURES = Path(__file__).parent / "fixtures" / "application_inspection"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_models_have_stable_answer_free_dicts_and_tolerate_integration_fields() -> None:
    result = ApplicationScanResult(
        canonical_job_id="ashby:acme:42",
        provider=ApplicationProvider.ASHBY,
        application_url="https://jobs.ashbyhq.com/acme/42",
        status=ScanStatus.PARTIAL,
        questions=(
            ApplicationQuestion(
                label="Why this role?",
                field_type="long_text",
                ordinal=2,
                category="job_specific",
                is_custom=True,
            ),
            ApplicationQuestion(label="Email", field_type="email", ordinal=1, category="profile"),
        ),
        metadata={"z": 2, "a": {"token": "must-not-survive", "safe": "yes"}},
    )

    serialized = result.to_dict()

    assert [question["label"] for question in serialized["questions"]] == ["Email", "Why this role?"]
    assert serialized["metadata"] == {"a": {"safe": "yes"}, "z": 2}
    restored = ApplicationScanResult.from_dict({**serialized, "issue_number": 7, "attempt_count": 1})
    assert restored.to_dict() == serialized
    assert "answer" not in json.dumps(serialized).casefold()


def test_hostname_provider_detection_is_not_tied_to_exact_subdomains() -> None:
    assert detect_application_provider("https://job-boards.greenhouse.io/acme/jobs/12") is ApplicationProvider.GREENHOUSE
    assert detect_application_provider("https://jobs.ashbyhq.com/acme/abc/application") is ApplicationProvider.ASHBY
    assert detect_application_provider("https://jobs.lever.co/acme/abc") is ApplicationProvider.LEVER
    assert detect_application_provider("https://tenant.wd12.myworkdayjobs.com/en-US/job/abc") is ApplicationProvider.WORKDAY
    assert detect_application_provider("https://careers.example.test/opening/1") is ApplicationProvider.GENERIC


def test_greenhouse_select_type_variants_are_normalized() -> None:
    assert normalize_field_type("multi_value_single_select") == "single_select"
    assert normalize_field_type("single_value_single_select") == "single_select"
    assert normalize_field_type("multi_value_multi_select") == "multi_select"


def test_static_extractor_uses_semantic_labels_groups_options_and_categories() -> None:
    extraction = extract_questions_from_html(fixture("generic_form.html"))
    by_label = {question.label: question for question in extraction.questions}

    assert extraction.has_form is True
    assert by_label["Email address"].field_type == "email"
    assert by_label["Email address"].required is True
    assert by_label["LinkedIn URL"].category == "profile"
    assert by_label["Expected graduation date"].category == "education"
    assert by_label["Why are you interested in this role?"].is_custom is True
    assert by_label["Preferred office"].options == ("San Francisco", "New York")
    assert by_label["Are you legally authorized to work in the United States?"].options == ("Yes", "No")
    assert by_label["Are you legally authorized to work in the United States?"].required is True
    assert by_label["Skills"].field_type == "multi_select"
    assert by_label["Resume"].field_type == "file"


def test_static_page_safety_signals_are_unavailable_without_any_bypass() -> None:
    result = inspect_static_html(
        canonical_job_id="url:captcha",
        application_url="https://careers.example.test/job/1",
        html=fixture("captcha.html"),
    )

    assert result.status is ScanStatus.UNAVAILABLE
    assert result.completeness_reason == "Application page requires anti-bot verification."
    assert result.questions == ()


def test_closed_and_blank_zero_control_pages_are_never_complete() -> None:
    closed = inspect_static_html(
        canonical_job_id="url:closed",
        application_url="https://careers.example.test/job/closed",
        html=fixture("closed.html"),
    )
    blank = inspect_static_html(
        canonical_job_id="url:blank",
        application_url="https://careers.example.test/job/blank",
        html=fixture("blank_error.html"),
    )

    assert closed.status is ScanStatus.UNAVAILABLE
    assert closed.completeness_reason == "Application appears to be closed."
    assert blank.status is ScanStatus.UNAVAILABLE
    assert blank.status is not ScanStatus.COMPLETE


def test_static_login_with_visible_fields_and_multi_step_are_partial() -> None:
    result = inspect_static_html(
        canonical_job_id="url:login",
        application_url="https://careers.example.test/job/1",
        html="""
          <form><label for='email'>Email</label><input id='email' type='email'></form>
          <p>Step 1 of 3</p><a>Sign in</a>
        """,
    )

    assert result.status is ScanStatus.PARTIAL
    assert result.questions[0].label == "Email"
    assert result.completeness_reason == "Additional application questions require authentication."


def test_public_apply_anchor_is_derived_without_clicking() -> None:
    assert find_public_apply_link(
        fixture("lever_job_page.html"), "https://jobs.lever.co/acme/123"
    ) == "https://jobs.lever.co/acme/123/apply"


def test_greenhouse_reference_and_structured_question_groups() -> None:
    reference = greenhouse_job_reference("https://job-boards.greenhouse.io/acme/jobs/12345?gh_src=feed")
    assert reference == GreenhouseJobReference("acme", "12345")
    assert reference.api_url.endswith("/v1/boards/acme/jobs/12345?questions=true")

    questions = parse_greenhouse_payload(json.loads(fixture("greenhouse_job.json")))
    by_label = {question.label: question for question in questions}
    assert by_label["Will you now or in the future require sponsorship?"].category == "work_authorization"
    assert by_label["Will you now or in the future require sponsorship?"].options == ("Yes", "No")
    assert by_label["Tell us about a project you are proud of."].is_custom is True
    assert by_label["Veteran status"].category == "compliance_demographic"
    assert by_label["Compliance acknowledgement"].category == "compliance_demographic"


def test_greenhouse_structured_result_is_complete_but_zero_schema_is_not() -> None:
    payload = json.loads(fixture("greenhouse_job.json"))

    result = inspect_greenhouse_application(
        canonical_job_id="greenhouse:acme:12345",
        application_url="https://boards.greenhouse.io/acme/jobs/12345",
        fetcher=lambda _: (payload, 200),
        scanned_at="2026-08-17T00:00:00Z",
    )
    empty = inspect_greenhouse_application(
        canonical_job_id="greenhouse:acme:empty",
        application_url="https://boards.greenhouse.io/acme/jobs/999",
        fetcher=lambda _: ({"questions": [], "location_questions": []}, 200),
    )

    assert result.status is ScanStatus.COMPLETE
    assert len(result.questions) == 6
    assert result.metadata == {"inspection_method": "greenhouse_api"}
    assert empty.status is ScanStatus.PARTIAL


def test_greenhouse_hidden_api_inputs_are_not_presentation_questions() -> None:
    assert parse_greenhouse_payload(
        {
            "location_questions": [
                {
                    "label": "Latitude",
                    "required": True,
                    "fields": [{"name": "latitude", "type": "input_hidden", "values": []}],
                }
            ]
        }
    ) == ()


def test_malformed_greenhouse_schema_is_failed_not_an_empty_complete_scan() -> None:
    result = inspect_greenhouse_application(
        canonical_job_id="greenhouse:acme:bad",
        application_url="https://boards.greenhouse.io/acme/jobs/456",
        fetcher=lambda _: ({"questions": "invalid"}, 200),
    )

    assert result.status is ScanStatus.FAILED
    assert result.questions == ()


def test_greenhouse_optional_null_group_is_valid_and_not_a_parser_failure() -> None:
    questions = parse_greenhouse_payload({"questions": [], "demographic_questions": None})

    assert questions == ()


def test_named_ashby_fixture_has_standard_authorization_and_custom_questions() -> None:
    questions = {question.label: question for question in extract_questions_from_html(fixture("ashby_form.html")).questions}

    assert questions["Name"].required is True
    assert questions["Name"].category == "profile"
    assert questions["Are you legally authorized to work in the United States?"].field_type == "single_select"
    assert questions["Are you legally authorized to work in the United States?"].options == ("Yes", "No")
    assert questions["Why are you interested in this role?"].is_custom is True


def test_named_lever_fixture_handles_standard_custom_radio_checkbox_and_file_fields() -> None:
    questions = {question.label: question for question in extract_questions_from_html(fixture("lever_apply_form.html")).questions}

    assert questions["Full name"].category == "profile"
    assert questions["Resume / CV"].field_type == "file"
    assert questions["Describe a project you are proud of."].is_custom is True
    assert questions["Will you require sponsorship now or in the future?"].field_type == "radio"
    assert questions["Will you require sponsorship now or in the future?"].category == "work_authorization"
    assert questions["Technical areas"].field_type == "multi_select"


class _FakeResponse:
    status = 200


class _FakePage:
    def __init__(self, html: str) -> None:
        self.html = html
        self.goto_calls: list[tuple[str, str, int]] = []

    def set_default_navigation_timeout(self, value: int) -> None:
        self.navigation_timeout = value

    def set_default_timeout(self, value: int) -> None:
        self.default_timeout = value

    def goto(self, url: str, *, wait_until: str, timeout: int) -> _FakeResponse:
        self.goto_calls.append((url, wait_until, timeout))
        return _FakeResponse()

    def content(self) -> str:
        return self.html


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.closed = False

    def new_page(self) -> _FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self.context = context
        self.closed = False

    def new_context(self) -> _FakeContext:
        return self.context

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.browser = browser

    def launch(self, *, headless: bool) -> _FakeBrowser:
        assert headless is True
        return self.browser


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.chromium = _FakeChromium(browser)

    def __enter__(self) -> "_FakePlaywright":
        return self

    def __exit__(self, *unused: object) -> None:
        return None


def test_browser_scanner_only_navigates_once_and_always_closes_resources() -> None:
    page = _FakePage(fixture("generic_form.html"))
    context = _FakeContext(page)
    browser = _FakeBrowser(context)
    scanner = BrowserScanner(playwright_factory=lambda: _FakePlaywright(browser), timeout_seconds=7)

    result = scanner.scan(
        canonical_job_id="ashby:acme:42",
        application_url="https://jobs.ashbyhq.com/acme/42",
        provider=ApplicationProvider.ASHBY,
    )

    assert result.status is ScanStatus.PARTIAL
    assert len(page.goto_calls) == 1
    assert page.goto_calls[0] == ("https://jobs.ashbyhq.com/acme/42", "domcontentloaded", 7000)
    assert context.closed is True
    assert browser.closed is True


def test_browser_unavailability_is_an_explicit_supported_status() -> None:
    def unavailable_factory() -> object:
        raise PlaywrightUnavailable("Chromium is unavailable")

    result = BrowserScanner(playwright_factory=unavailable_factory).scan(
        canonical_job_id="generic:acme:1",
        application_url="https://careers.example.test/jobs/1",
        provider=ApplicationProvider.GENERIC,
    )

    assert result.status is ScanStatus.UNSUPPORTED


@dataclass(frozen=True)
class _Job:
    canonical_id: str
    application_url: str


class _RoutingBrowser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ApplicationProvider]] = []

    def scan(self, *, canonical_job_id: str, application_url: str, provider: ApplicationProvider, scanned_at: str) -> ApplicationScanResult:
        self.calls.append((application_url, provider))
        return ApplicationScanResult(
            canonical_job_id=canonical_job_id,
            provider=provider,
            application_url=application_url,
            status=ScanStatus.PARTIAL,
            scanned_at=scanned_at,
            completeness_reason="Visible first page only.",
            metadata={"inspection_method": "browser"},
        )


def test_known_dynamic_provider_routes_to_browser_even_when_static_controls_exist() -> None:
    browser = _RoutingBrowser()
    inspector = ApplicationInspector(
        http_fetcher=lambda _: FetchedPage(
            fixture("generic_form.html"), 200, "https://jobs.ashbyhq.com/acme/42"
        ),
        browser_scanner=browser,  # type: ignore[arg-type]
        now=lambda: "2026-08-17T00:00:00Z",
    )

    result = inspector.inspect(_Job("ashby:acme:42", "https://jobs.ashbyhq.com/acme/42"))

    assert result.status is ScanStatus.PARTIAL
    assert browser.calls == [("https://jobs.ashbyhq.com/acme/42", ApplicationProvider.ASHBY)]


def test_workday_and_generic_no_control_pages_use_conservative_browser_fallback() -> None:
    workday_url = "https://tenant.wd1.myworkdayjobs.com/en-US/job/San-Francisco/Engineer_1"
    generic_url = "https://careers.example.test/jobs/1"
    for canonical_id, url, expected_provider in (
        ("workday:tenant:engineer_1", workday_url, ApplicationProvider.WORKDAY),
        ("url:careers:1", generic_url, ApplicationProvider.GENERIC),
    ):
        browser = _RoutingBrowser()
        inspector = ApplicationInspector(
            http_fetcher=lambda _, current_url=url: FetchedPage(
                "<main>Application form loads dynamically.</main>", 200, current_url
            ),
            browser_scanner=browser,  # type: ignore[arg-type]
            now=lambda: "2026-08-17T00:00:00Z",
        )

        result = inspector.inspect(_Job(canonical_id, url))

        assert result.status is ScanStatus.PARTIAL
        assert browser.calls == [(url, expected_provider)]


def test_redirect_strategy_does_not_persist_a_redirect_query_url() -> None:
    browser = _RoutingBrowser()
    original_url = "https://careers.example.test/jobs/42"
    inspector = ApplicationInspector(
        http_fetcher=lambda _: FetchedPage(
            "<main>This form is rendered by JavaScript.</main>",
            200,
            "https://jobs.ashbyhq.com/acme/42/application?embed=true",
        ),
        browser_scanner=browser,  # type: ignore[arg-type]
        now=lambda: "2026-08-17T00:00:00Z",
    )

    result = inspector.inspect(_Job("url:employer:42", original_url))

    assert browser.calls == [
        ("https://jobs.ashbyhq.com/acme/42/application?embed=true", ApplicationProvider.ASHBY)
    ]
    assert result.application_url == original_url


def test_greenhouse_structured_lookup_is_not_repeated_before_static_fallback() -> None:
    calls: list[GreenhouseJobReference] = []
    browser = _RoutingBrowser()

    def empty_greenhouse(reference: GreenhouseJobReference) -> tuple[dict[str, list[object]], int]:
        calls.append(reference)
        return {"questions": []}, 200

    inspector = ApplicationInspector(
        greenhouse_fetcher=empty_greenhouse,
        http_fetcher=lambda _: FetchedPage(
            "<main>Application form loads dynamically.</main>",
            200,
            "https://boards.greenhouse.io/acme/jobs/12",
        ),
        browser_scanner=browser,  # type: ignore[arg-type]
        now=lambda: "2026-08-17T00:00:00Z",
    )

    inspector.inspect(_Job("greenhouse:acme:12", "https://boards.greenhouse.io/acme/jobs/12"))

    assert calls == [GreenhouseJobReference("acme", "12")]


def test_lever_explicit_apply_link_routes_browser_to_public_apply_page() -> None:
    browser = _RoutingBrowser()
    pages = {
        "https://jobs.lever.co/acme/123": FetchedPage(
            fixture("lever_job_page.html"), 200, "https://jobs.lever.co/acme/123"
        ),
        "https://jobs.lever.co/acme/123/apply": FetchedPage(
            "<main>Application form is rendered by JavaScript.</main>",
            200,
            "https://jobs.lever.co/acme/123/apply",
        ),
    }
    inspector = ApplicationInspector(
        http_fetcher=lambda url: pages[url],
        browser_scanner=browser,  # type: ignore[arg-type]
        now=lambda: "2026-08-17T00:00:00Z",
    )

    result = inspector.inspect(_Job("lever:acme:123", "https://jobs.lever.co/acme/123"))

    assert browser.calls == [("https://jobs.lever.co/acme/123/apply", ApplicationProvider.LEVER)]
    assert result.application_url == "https://jobs.lever.co/acme/123/apply"


def test_protected_public_page_is_unavailable_not_a_retryable_technical_failure() -> None:
    def protected_page(*unused: object, **unused_keywords: object) -> FetchedPage:
        raise PublicFetchError("denied", status=403)

    inspector = ApplicationInspector(
        http_fetcher=protected_page,
        browser_scanner=_RoutingBrowser(),  # type: ignore[arg-type]
        now=lambda: "2026-08-17T00:00:00Z",
    )

    result = inspector.inspect(_Job("url:protected", "https://careers.example.test/jobs/42"))

    assert result.status is ScanStatus.UNAVAILABLE
    assert result.http_status == 403


def test_renderer_keeps_custom_questions_first_and_does_not_add_markers() -> None:
    result = ApplicationScanResult(
        canonical_job_id="greenhouse:acme:1",
        provider=ApplicationProvider.GREENHOUSE,
        application_url="https://boards.greenhouse.io/acme/jobs/1",
        status=ScanStatus.PARTIAL,
        questions=(
            ApplicationQuestion("Email", "email", category="profile", ordinal=1),
            ApplicationQuestion("Why this role?", "long_text", category="job_specific", ordinal=2, is_custom=True),
            ApplicationQuestion("Will you require sponsorship?", "radio", category="work_authorization", ordinal=3),
        ),
    )
    rendered = render_application_scan_block(result)

    assert rendered.index("Custom screening questions") < rendered.index("Work authorization")
    assert "<details>" in rendered
    assert "application-scan:start" not in rendered
    assert "No application was submitted" in rendered

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

import src.check_jobs as check_jobs
from src.notifier import DeliveryResult, GitHubIssueNotifier, replace_application_scan_block
from src.storage import (
    StorageError,
    empty_application_questions_state,
    load_application_questions_state,
    serialise_json,
    validate_application_questions_state,
    write_json_if_changed,
    write_texts_transactionally,
)
from src.tracker import apply_current_jobs
from .test_tracker import make_job


class FakeScanResult:
    """Small public-result stand-in; integration tests do not need a browser."""

    def __init__(
        self,
        canonical_job_id: str,
        application_url: str,
        *,
        status: str = "complete",
        error_message: str | None = None,
    ) -> None:
        self.canonical_job_id = canonical_job_id
        self.application_url = application_url
        self.status = status
        self.error_message = error_message

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_url": self.application_url,
            "canonical_job_id": self.canonical_job_id,
            "completeness_reason": "Public fields only." if self.status == "partial" else None,
            "error_message": self.error_message,
            "error_type": "TimeoutError" if self.status == "failed" else None,
            "http_status": None,
            "metadata": {},
            "provider": "generic",
            "questions": [
                {
                    "category": "custom_screening",
                    "field_type": "text",
                    "label": "Why are you interested in this role?",
                    "options": [],
                    "ordinal": 1,
                    "required": True,
                }
            ],
            "scanned_at": "2026-08-18T12:00:00Z",
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FakeScanResult":
        return cls(
            str(value["canonical_job_id"]),
            str(value["application_url"]),
            status=str(value["status"]),
            error_message=value.get("error_message") if isinstance(value.get("error_message"), str) else None,
        )


class RecordingNotifier:
    def __init__(self, events: list[str], *, fail_updates_for: set[str] | None = None) -> None:
        self.events = events
        self.fail_updates_for = fail_updates_for or set()
        self.updated: list[tuple[int, str, str]] = []

    def update_issue_with_application_scan(
        self, issue_number: int, canonical_job_id: str, rendered_block: str
    ) -> bool:
        self.events.append(f"update:{canonical_job_id}")
        if canonical_job_id in self.fail_updates_for:
            raise RuntimeError("simulated GitHub PATCH failure")
        self.updated.append((issue_number, canonical_job_id, rendered_block))
        return True


def _fake_api() -> SimpleNamespace:
    def failed_scan_result(job: Any, _url: str, _provider: Any, error: Exception) -> FakeScanResult:
        return FakeScanResult(job.canonical_id, job.application_url, status="failed", error_message=str(error))

    return SimpleNamespace(
        ApplicationInspector=object,
        ApplicationScanResult=FakeScanResult,
        detect_application_provider=lambda _url: "generic",
        failed_scan_result=failed_scan_result,
        render_application_scan_block=lambda result: f"### Application Scan\n\nStatus: {result.status}",
    )


def _state_with_new_alerts(*names: str) -> tuple[dict[str, Any], str, dict[str, str]]:
    first = make_job("https://jobs.example.test/baseline", company="Baseline")
    baseline = apply_current_jobs(
        # The first source baseline never creates an application scan target.
        {"initialized": False, "initialized_at": None, "initialized_sources": {}, "jobs": {}, "location_scope_version": "sf-bay-area-roughly-one-hour-v1", "pending_notifications": {}, "schema_version": 2},
        [first],
        successful_source_ids={first.source_id},
        timestamp="2026-08-18T11:00:00Z",
    )
    new_jobs = [
        make_job(f"https://jobs.example.test/{name.casefold()}", company=name)
        for name in names
    ]
    transition = apply_current_jobs(
        baseline.state,
        [first, *new_jobs],
        successful_source_ids={first.source_id},
        timestamp="2026-08-18T12:00:00Z",
    )
    batch_id = next(iter(transition.state["pending_notifications"]))
    assert transition.state["pending_notifications"][batch_id]["application_scan_eligible"] is True
    ids = {job.company: job.canonical_id for job in transition.new_jobs}
    return transition.state, batch_id, ids


def _delivery_for(batch_id: str, issue_number: int = 42) -> DeliveryResult:
    return DeliveryResult((batch_id,), (), (), ((batch_id, issue_number),))


def test_scan_state_is_deterministic_and_rejects_answers_or_credentials() -> None:
    state = empty_application_questions_state()
    record = FakeScanResult("generic:example:one", "https://jobs.example.test/one").to_dict()
    record.update(
        {
            "attempt_count": 1,
            "first_scanned": "2026-08-18T12:00:00Z",
            "issue_number": 42,
            "issue_update_pending": True,
            "last_scanned": "2026-08-18T12:00:00Z",
        }
    )
    state["scans"]["generic:example:one"] = record

    assert validate_application_questions_state(state) == state
    assert serialise_json(state) == serialise_json(validate_application_questions_state(state))

    unsafe = empty_application_questions_state()
    unsafe_record = dict(record)
    unsafe_record["questions"] = [{"label": "Will you need sponsorship?", "answer": "No"}]
    unsafe["scans"]["generic:example:one"] = unsafe_record
    with pytest.raises(StorageError, match="prohibited"):
        validate_application_questions_state(unsafe)

    sanitized = check_jobs._scan_record_for_result(
        FakeScanResult(
            "generic:example:error",
            "https://jobs.example.test/error",
            status="failed",
            error_message="request https://jobs.example.test/error?access_token=secret cookie=hidden failed",
        ),
        canonical_job_id="generic:example:error",
        prior_record=None,
        issue_number=42,
    )
    assert "secret" not in str(sanitized["error_message"])
    assert "hidden" not in str(sanitized["error_message"])
    assert "?" not in str(sanitized["error_message"])

    unsafe_record = dict(record)
    unsafe_record["metadata"] = {"csrf_token": "secret"}
    unsafe["scans"]["generic:example:one"] = unsafe_record
    with pytest.raises(StorageError, match="prohibited"):
        validate_application_questions_state(unsafe)


def test_scan_failure_is_isolated_and_persisted_before_each_issue_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, batch_id, ids = _state_with_new_alerts("Alpha", "Broken", "Charlie")
    events: list[str] = []
    notifier = RecordingNotifier(events)
    calls: list[str] = []

    class Inspector:
        def inspect(self, job: Any) -> FakeScanResult:
            calls.append(job.canonical_id)
            events.append(f"inspect:{job.canonical_id}")
            if job.company == "Broken":
                raise TimeoutError("temporary timeout")
            return FakeScanResult(job.canonical_id, job.application_url)

    real_write = check_jobs.write_json_if_changed

    def recording_write(path: Path, value: Any) -> bool:
        events.append("persist")
        return real_write(path, value)

    monkeypatch.setattr(check_jobs, "_application_inspection_api", _fake_api)
    monkeypatch.setattr(check_jobs, "write_json_if_changed", recording_write)

    result = check_jobs.enrich_delivered_job_issues(
        root=tmp_path,
        state=state,
        delivery=_delivery_for(batch_id),
        notifier=notifier,
        inspector_factory=Inspector,
    )

    assert set(calls) == set(ids.values())
    assert set(result.attempted_job_ids) == set(ids.values())
    assert ids["Broken"] in result.failed_job_ids
    assert {canonical_id for _, canonical_id, _ in notifier.updated} == set(ids.values())
    assert all(events[index - 1] == "persist" for index, event in enumerate(events) if event.startswith("update:"))

    scans = load_application_questions_state(tmp_path / "data" / "application_questions.json")["scans"]
    assert scans[ids["Broken"]]["status"] == "failed"
    assert scans[ids["Broken"]]["attempt_count"] == 1
    assert all(record["issue_update_pending"] is False for record in scans.values())


def test_pending_issue_update_reuses_one_marker_after_a_retry() -> None:
    class BodyNotifier(GitHubIssueNotifier):
        def __init__(self) -> None:
            self.body = "# New job\n\n<!-- application-scan:start canonical-id=ashby:one -->\nold\n<!-- application-scan:end canonical-id=ashby:one -->\n"
            self.patch_calls = 0

        def get_issue_body(self, _issue_number: int) -> str:
            return self.body

        def update_issue_body(self, _issue_number: int, body: str) -> None:
            self.patch_calls += 1
            self.body = body

    notifier = BodyNotifier()
    assert notifier.update_issue_with_application_scan(42, "ashby:one", "### Application Scan\nnew") is True
    assert notifier.update_issue_with_application_scan(42, "ashby:one", "### Application Scan\nnew") is False
    assert notifier.patch_calls == 1
    assert notifier.body.count("application-scan:start canonical-id=ashby:one") == 1
    assert "old" not in notifier.body

    body = replace_application_scan_block("# Alert", "ashby:two", "scan")
    assert body.count("application-scan:start canonical-id=ashby:two") == 1

    legacy = "<!-- application-scan:start:ashby:legacy -->\nold\n<!-- application-scan:end:ashby:legacy -->"
    migrated = replace_application_scan_block(legacy, "ashby:legacy", "new")
    assert migrated.count("application-scan:start") == 1
    assert "old" not in migrated


def test_one_issue_update_failure_leaves_its_saved_scan_pending_but_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, batch_id, ids = _state_with_new_alerts("First", "PatchFails", "Last")
    events: list[str] = []
    notifier = RecordingNotifier(events, fail_updates_for={ids["PatchFails"]})

    class Inspector:
        def inspect(self, job: Any) -> FakeScanResult:
            return FakeScanResult(job.canonical_id, job.application_url)

    monkeypatch.setattr(check_jobs, "_application_inspection_api", _fake_api)
    result = check_jobs.enrich_delivered_job_issues(
        root=tmp_path,
        state=state,
        delivery=_delivery_for(batch_id),
        notifier=notifier,
        inspector_factory=Inspector,
    )

    assert set(result.attempted_job_ids) == set(ids.values())
    assert ids["PatchFails"] in result.failed_job_ids
    assert {canonical_id for _, canonical_id, _ in notifier.updated} == {
        ids["First"],
        ids["Last"],
    }
    scans = load_application_questions_state(tmp_path / "data" / "application_questions.json")["scans"]
    assert scans[ids["PatchFails"]]["issue_update_pending"] is True
    assert scans[ids["First"]]["issue_update_pending"] is False
    assert scans[ids["Last"]]["issue_update_pending"] is False

    retry_notifier = RecordingNotifier([])
    retry = check_jobs.enrich_delivered_job_issues(
        root=tmp_path,
        state=state,
        delivery=DeliveryResult((), (), ()),
        notifier=retry_notifier,
        inspector_factory=lambda: pytest.fail("an Issue PATCH retry must not rescan the application"),
    )
    assert retry.attempted_job_ids == ()
    assert retry.updated_issue_job_ids == (ids["PatchFails"],)
    assert retry_notifier.updated[0][1] == ids["PatchFails"]


def test_failed_scans_retry_only_up_to_the_configured_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, batch_id, ids = _state_with_new_alerts("RetryOnly")
    monkeypatch.setattr(check_jobs, "_application_inspection_api", _fake_api)

    class AlwaysFailingInspector:
        def __init__(self) -> None:
            self.calls = 0

        def inspect(self, job: Any) -> FakeScanResult:
            self.calls += 1
            raise TimeoutError(f"temporary failure for {job.canonical_id}")

    first = AlwaysFailingInspector()
    check_jobs.enrich_delivered_job_issues(
        root=tmp_path,
        state=state,
        delivery=_delivery_for(batch_id),
        notifier=RecordingNotifier([]),
        inspector_factory=lambda: first,
        max_attempts=2,
    )
    second = AlwaysFailingInspector()
    retry = check_jobs.enrich_delivered_job_issues(
        root=tmp_path,
        state=state,
        delivery=DeliveryResult((), (), ()),
        notifier=RecordingNotifier([]),
        inspector_factory=lambda: second,
        max_attempts=2,
    )
    stopped = check_jobs.enrich_delivered_job_issues(
        root=tmp_path,
        state=state,
        delivery=DeliveryResult((), (), ()),
        notifier=RecordingNotifier([]),
        inspector_factory=lambda: pytest.fail("retry cap must prevent another inspector launch"),
        max_attempts=2,
    )

    assert first.calls == 1
    assert second.calls == 1
    assert retry.attempted_job_ids == (ids["RetryOnly"],)
    assert stopped.attempted_job_ids == ()
    scan = load_application_questions_state(tmp_path / "data" / "application_questions.json")["scans"]
    assert scan[ids["RetryOnly"]]["attempt_count"] == 2


def test_known_canonical_job_with_a_source_update_is_not_scanned_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, batch_id, ids = _state_with_new_alerts("Stable")
    monkeypatch.setattr(check_jobs, "_application_inspection_api", _fake_api)

    class Inspector:
        def inspect(self, job: Any) -> FakeScanResult:
            return FakeScanResult(job.canonical_id, job.application_url)

    check_jobs.enrich_delivered_job_issues(
        root=tmp_path,
        state=state,
        delivery=_delivery_for(batch_id),
        notifier=RecordingNotifier([]),
        inspector_factory=Inspector,
    )
    # A source metadata refresh is intentionally irrelevant to canonical job
    # newness. No new batch exists, so no inspector can be constructed.
    result = check_jobs.enrich_delivered_job_issues(
        root=tmp_path,
        state=state,
        delivery=DeliveryResult((), (), ()),
        notifier=RecordingNotifier([]),
        inspector_factory=lambda: pytest.fail("known canonical jobs must not be rescanned"),
    )

    assert result.attempted_job_ids == ()
    assert ids["Stable"] not in result.failed_job_ids


def test_no_delivery_does_not_launch_inspector_or_write_scan_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    constructed = False

    def fail_if_constructed() -> object:
        nonlocal constructed
        constructed = True
        raise AssertionError("an inspector must not launch without new/retry targets")

    monkeypatch.setattr(check_jobs, "_application_inspection_api", _fake_api)
    result = check_jobs.enrich_delivered_job_issues(
        root=tmp_path,
        state={"jobs": {}, "pending_notifications": {}},
        delivery=DeliveryResult((), (), ()),
        notifier=RecordingNotifier([]),
        inspector_factory=fail_if_constructed,
    )

    assert result.attempted_job_ids == ()
    assert constructed is False
    assert not (tmp_path / "data" / "application_questions.json").exists()


def test_legacy_notification_batch_is_not_an_application_scan_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, batch_id, _ids = _state_with_new_alerts("Legacy")
    # Simulate a batch written before this feature's prospective eligibility
    # marker existed. It can still deliver its job alert, but not scan history.
    state["pending_notifications"][batch_id].pop("application_scan_eligible")
    monkeypatch.setattr(check_jobs, "_application_inspection_api", _fake_api)
    result = check_jobs.enrich_delivered_job_issues(
        root=tmp_path,
        state=state,
        delivery=_delivery_for(batch_id),
        notifier=RecordingNotifier([]),
        inspector_factory=lambda: pytest.fail("legacy jobs must not be scanned"),
    )

    assert result.attempted_job_ids == ()
    assert not (tmp_path / "data" / "application_questions.json").exists()


def test_disabled_scan_flag_preserves_alert_delivery_and_never_constructs_inspector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, batch_id, _ids = _state_with_new_alerts("EnabledAlert")
    seen_path = tmp_path / "data" / "seen_jobs.json"
    write_texts_transactionally({seen_path: serialise_json(state)})
    events: list[str] = []

    class AlertNotifier:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def find_issue_for_batch(self, _batch_id: str) -> None:
            return None

        def create_issue(self, _jobs: list[Any], _batch_id: str) -> int:
            events.append("alert")
            return 73

        def update_issue_with_application_scan(self, *_args: object) -> bool:
            raise AssertionError("disabled enrichment must not update Issues")

    monkeypatch.setattr(check_jobs, "GitHubIssueNotifier", AlertNotifier)
    monkeypatch.setattr(
        check_jobs,
        "_application_inspection_api",
        lambda: pytest.fail("disabled enrichment must not import the inspector"),
    )

    delivered = check_jobs.deliver_pending(
        root=tmp_path,
        environment={
            "GITHUB_TOKEN": "not-a-real-token",
            "GITHUB_REPOSITORY": "LamdaDev/sf-job-tracker",
            "APPLICATION_SCAN_ENABLED": "false",
        },
        inspector_factory=lambda: pytest.fail("disabled enrichment must not launch an inspector"),
    )

    assert delivered is not None
    assert delivered.delivered_batches == (batch_id,)
    assert events == ["alert"]
    assert not (tmp_path / "data" / "application_questions.json").exists()
    persisted_seen = check_jobs.load_seen_state(seen_path)
    assert persisted_seen["pending_notifications"][batch_id]["status"] == "sent"


def test_alert_issue_is_created_before_a_scan_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, batch_id, ids = _state_with_new_alerts("FailsAfterAlert")
    seen_path = tmp_path / "data" / "seen_jobs.json"
    write_texts_transactionally({seen_path: serialise_json(state)})
    events: list[str] = []

    class AlertNotifier:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def find_issue_for_batch(self, _batch_id: str) -> None:
            return None

        def create_issue(self, _jobs: list[Any], _batch_id: str) -> int:
            events.append("alert")
            return 74

        def update_issue_with_application_scan(
            self, _issue: int, canonical_job_id: str, _block: str
        ) -> bool:
            events.append(f"update:{canonical_job_id}")
            return True

    class FailingInspector:
        def inspect(self, job: Any) -> FakeScanResult:
            events.append(f"inspect:{job.canonical_id}")
            raise RuntimeError("application page parser crashed")

    monkeypatch.setattr(check_jobs, "GitHubIssueNotifier", AlertNotifier)
    monkeypatch.setattr(check_jobs, "_application_inspection_api", _fake_api)

    delivered = check_jobs.deliver_pending(
        root=tmp_path,
        environment={"GITHUB_TOKEN": "not-a-real-token", "APPLICATION_SCAN_ENABLED": "true"},
        inspector_factory=FailingInspector,
    )

    assert delivered is not None
    assert delivered.delivered_batches == (batch_id,)
    assert events[0] == "alert"
    assert f"inspect:{ids['FailsAfterAlert']}" in events
    assert events.index("alert") < events.index(f"inspect:{ids['FailsAfterAlert']}")
    assert check_jobs.load_seen_state(seen_path)["pending_notifications"][batch_id]["status"] == "sent"
    scan = load_application_questions_state(tmp_path / "data" / "application_questions.json")["scans"]
    assert scan[ids["FailsAfterAlert"]]["status"] == "failed"

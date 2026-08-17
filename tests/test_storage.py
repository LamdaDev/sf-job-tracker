from __future__ import annotations

from pathlib import Path

import pytest

import src.storage as storage
from src.canonical import canonicalize_job_url
from src.config import LOCATION_SCOPE_VERSION
from src.storage import StorageError, validate_current_state, validate_seen_state, write_texts_transactionally


def test_multi_file_write_replaces_every_staged_file(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("old first", encoding="utf-8")
    second.write_text("old second", encoding="utf-8")

    changed = write_texts_transactionally({first: "new first", second: "new second"})

    assert changed == (first, second)
    assert first.read_text(encoding="utf-8") == "new first"
    assert second.read_text(encoding="utf-8") == "new second"


def test_multi_file_write_rolls_back_if_a_later_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("old first", encoding="utf-8")
    second.write_text("old second", encoding="utf-8")
    real_replace = storage.os.replace
    failed = False

    def fail_once(source: str | Path, destination: str | Path) -> None:
        nonlocal failed
        if Path(destination) == second and not failed:
            failed = True
            raise OSError("simulated replacement failure")
        real_replace(source, destination)

    monkeypatch.setattr(storage.os, "replace", fail_once)

    with pytest.raises(StorageError, match="Could not replace generated tracker files"):
        write_texts_transactionally({first: "new first", second: "new second"})

    assert first.read_text(encoding="utf-8") == "old first"
    assert second.read_text(encoding="utf-8") == "old second"


def test_v1_speedyapply_state_and_current_snapshot_migrate_idempotently() -> None:
    url = "https://jobs.ashbyhq.com/notion/ABC/application?utm_source=SpeedyApply"
    legacy_job = {
        "company": "Notion",
        "position": "Software Engineer Intern",
        "location": "San Francisco, CA",
        "salary": "$60/hr",
        "application_url": url,
        "age": "1d",
        "category": "FAANG+",
        "job_type": "Internship",
        "source_file": "README.md",
        "first_seen": "2026-08-10T12:00:00Z",
        "last_seen": "2026-08-11T12:00:00Z",
        "active": True,
        "inactive_at": None,
    }
    legacy_seen = {
        "schema_version": 1,
        "initialized": True,
        "initialized_at": "2026-08-10T12:00:00Z",
        "location_scope_version": LOCATION_SCOPE_VERSION,
        "jobs": {url: legacy_job},
        "pending_notifications": {
            "old-url-hash": {"created_at": "2026-08-11T12:00:00Z", "job_urls": [url], "status": "pending", "issue_number": None}
        },
    }

    migrated = validate_seen_state(legacy_seen)
    key = canonicalize_job_url(url)
    assert migrated["schema_version"] == 2
    assert migrated["jobs"][key]["first_seen"] == legacy_job["first_seen"]
    assert migrated["jobs"][key]["sources"]["speedyapply_internships"]["active"] is True
    assert migrated["initialized_sources"]["speedyapply_internships"] is True
    assert migrated["initialized_sources"]["applyguy_internships"] is False
    assert migrated["location_scope_version"] == LOCATION_SCOPE_VERSION
    assert migrated["pending_notifications"] == legacy_seen["pending_notifications"]
    assert validate_seen_state(migrated) == migrated

    migrated_current = validate_current_state({"schema_version": 1, "jobs": {url: legacy_job}})
    assert migrated_current["schema_version"] == 2
    assert list(migrated_current["jobs"]) == [key]

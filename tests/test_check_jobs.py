from __future__ import annotations

from pathlib import Path

import pytest

import src.check_jobs as check_jobs
from src.check_jobs import main, run_tracker
from src.config import SOURCES
from src.fetcher import FetchedSnapshot
from src.notifier import DeliveryResult
from src.parser import UpstreamFormatError


FIXTURES = Path(__file__).parent / "fixtures"


def snapshot_from_fixtures() -> FetchedSnapshot:
    return FetchedSnapshot(
        commit_sha="a" * 40,
        documents={
            SOURCES[0]: (FIXTURES / "internships.md").read_text(encoding="utf-8"),
            SOURCES[1]: (FIXTURES / "new_grads.md").read_text(encoding="utf-8"),
        },
    )


def test_dry_run_does_not_write_state_or_dashboard(tmp_path: Path) -> None:
    summary = run_tracker(
        root=tmp_path,
        dry_run=True,
        snapshot_fetcher=snapshot_from_fixtures,
        timestamp="2026-08-16T12:00:00Z",
    )

    assert summary.transition.baseline is True
    assert summary.matching_count == 5
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "jobs.md").exists()


def test_parse_failure_preserves_existing_state_files(tmp_path: Path) -> None:
    run_tracker(
        root=tmp_path,
        snapshot_fetcher=snapshot_from_fixtures,
        timestamp="2026-08-16T12:00:00Z",
    )
    seen_path = tmp_path / "data" / "seen_jobs.json"
    current_path = tmp_path / "data" / "current_jobs.json"
    dashboard_path = tmp_path / "jobs.md"
    before = {path: path.read_text(encoding="utf-8") for path in (seen_path, current_path, dashboard_path)}

    def broken_snapshot() -> FetchedSnapshot:
        snapshot = snapshot_from_fixtures()
        return FetchedSnapshot(
            commit_sha="b" * 40,
            documents={SOURCES[0]: "not a compatible source", SOURCES[1]: snapshot.documents[SOURCES[1]]},
        )

    with pytest.raises(UpstreamFormatError):
        run_tracker(root=tmp_path, snapshot_fetcher=broken_snapshot)

    assert {path: path.read_text(encoding="utf-8") for path in before} == before


def test_delivery_command_returns_nonzero_after_persisting_notification_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed_delivery = DeliveryResult(
        delivered_batches=("sent",), failed_batches=("pending",), existing_issue_batches=()
    )
    monkeypatch.setattr(check_jobs, "deliver_pending", lambda *, root: failed_delivery)

    assert main(["--deliver-pending", "--root", str(tmp_path)]) == 2

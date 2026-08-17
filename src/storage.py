"""Validated, deterministic, atomic JSON storage with v1 history migration."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .canonical import aggregate_observations
from .config import (
    CURRENT_JOBS_SCHEMA_VERSION,
    LEGACY_SPEEDY_SOURCE_IDS,
    SOURCES,
    STATE_SCHEMA_VERSION,
)
from .models import Job


class StorageError(RuntimeError):
    """Raised when persisted tracker state is missing or invalid."""


def _source_initialization_defaults() -> dict[str, bool]:
    return {source.id: False for source in SOURCES}


def empty_seen_state() -> dict[str, Any]:
    """Return the explicit uninitialized permanent-history schema."""

    return {
        "initialized": False,
        "initialized_at": None,
        "initialized_sources": _source_initialization_defaults(),
        "jobs": {},
        "pending_notifications": {},
        "schema_version": STATE_SCHEMA_VERSION,
    }


def empty_current_state() -> dict[str, Any]:
    """Return the deterministic empty current-snapshot schema."""

    return {"jobs": {}, "schema_version": CURRENT_JOBS_SCHEMA_VERSION}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as error:
        raise StorageError(f"Could not read valid JSON from {path}: {error}") from error


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise StorageError(f"{label} must be a JSON object")
    return value


def _legacy_lifecycle(record: Mapping[str, Any]) -> dict[str, Any]:
    """Copy stateful fields exactly from a v1 source record."""

    active = record.get("active")
    first_seen = record.get("first_seen")
    last_seen = record.get("last_seen")
    inactive_at = record.get("inactive_at")
    if not isinstance(active, bool):
        raise StorageError("legacy seen job record has an invalid active flag")
    for name, value in (("first_seen", first_seen), ("last_seen", last_seen)):
        if not isinstance(value, str):
            raise StorageError(f"legacy seen job record has an invalid {name}")
    if inactive_at is not None and not isinstance(inactive_at, str):
        raise StorageError("legacy seen job record has an invalid inactive_at")
    return {
        "active": active,
        "first_seen": first_seen,
        "inactive_at": inactive_at,
        "last_seen": last_seen,
    }


def _record_for_migrated_job(job: Job, lifecycle: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    canonical_jobs, _ = aggregate_observations([job])
    canonical = canonical_jobs[0]
    record = canonical.to_dict()
    record.update(lifecycle)
    source_record = job.source_dict()
    source_record.update(lifecycle)
    record["sources"] = {job.source_id: source_record}
    return canonical.canonical_id, record


def _merge_migrated_records(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Coalesce a rare legacy canonical collision without losing history."""

    existing_sources = existing.setdefault("sources", {})
    existing_sources.update(incoming.get("sources", {}))
    existing_aliases = set(existing.get("url_aliases", [])) | set(incoming.get("url_aliases", []))
    existing["url_aliases"] = sorted(alias for alias in existing_aliases if isinstance(alias, str))
    if str(incoming.get("first_seen", "")) < str(existing.get("first_seen", "")):
        for name in ("first_seen", "last_seen", "inactive_at"):
            existing[name] = incoming.get(name)
    existing["active"] = bool(existing.get("active")) or bool(incoming.get("active"))


def _migrate_seen_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade URL-keyed SpeedyApply history into canonical source-aware state."""

    if not isinstance(value.get("initialized"), bool):
        raise StorageError("seen job state has an invalid initialized flag")
    if value.get("initialized_at") is not None and not isinstance(value.get("initialized_at"), str):
        raise StorageError("seen job state has an invalid initialized_at value")
    jobs = value.get("jobs")
    pending = value.get("pending_notifications")
    if not isinstance(jobs, Mapping) or not isinstance(pending, Mapping):
        raise StorageError("legacy seen job state has invalid jobs or pending_notifications")

    migrated = empty_seen_state()
    migrated["initialized"] = value["initialized"]
    migrated["initialized_at"] = value.get("initialized_at")
    # A v1 state only contained the original SpeedyApply sources. Mark them as
    # onboarded so adding providers cannot re-alert historical jobs.
    for source_id in LEGACY_SPEEDY_SOURCE_IDS:
        migrated["initialized_sources"][source_id] = value["initialized"]
    for raw_url, raw_record in jobs.items():
        if not isinstance(raw_url, str) or not isinstance(raw_record, Mapping):
            raise StorageError("legacy seen job records must be keyed by URL objects")
        try:
            job = Job.from_mapping(raw_record)
        except ValueError as error:
            raise StorageError(f"Could not migrate legacy job {raw_url}: {error}") from error
        canonical_id, record = _record_for_migrated_job(job, _legacy_lifecycle(raw_record))
        current = migrated["jobs"].get(canonical_id)
        if current is None:
            migrated["jobs"][canonical_id] = record
        else:
            _merge_migrated_records(current, record)
    # Preserve old batch IDs and URL lists exactly: their hidden Issue markers
    # were derived from those URL lists and must remain idempotent on retry.
    migrated["pending_notifications"] = copy.deepcopy(dict(pending))
    return migrated


def _migrate_current_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    jobs = value.get("jobs")
    if not isinstance(jobs, Mapping):
        raise StorageError("legacy current job state has an invalid jobs mapping")
    observations: list[Job] = []
    for raw_url, raw_record in jobs.items():
        if not isinstance(raw_url, str) or not isinstance(raw_record, Mapping):
            raise StorageError("legacy current jobs must be URL-keyed objects")
        try:
            observations.append(Job.from_mapping(raw_record))
        except ValueError as error:
            raise StorageError(f"Could not migrate legacy current job {raw_url}: {error}") from error
    canonical_jobs, _ = aggregate_observations(observations)
    current = empty_current_state()
    for canonical in canonical_jobs:
        record = canonical.to_dict()
        record["sources"] = {job.source_id: job.source_dict() for job in canonical.observations}
        current["jobs"][canonical.canonical_id] = record
    return current


def _validate_initialized_sources(value: Any) -> dict[str, bool]:
    if not isinstance(value, Mapping):
        raise StorageError("seen job state has an invalid initialized_sources mapping")
    result = _source_initialization_defaults()
    for source_id, initialized in value.items():
        if not isinstance(source_id, str) or not isinstance(initialized, bool):
            raise StorageError("seen job state has invalid initialized_sources values")
        result[source_id] = initialized
    return result


def validate_seen_state(value: Any) -> dict[str, Any]:
    """Validate a v2 state or safely migrate the original v1 format."""

    state = dict(_require_mapping(value, "seen job state"))
    version = state.get("schema_version")
    if version == 1:
        state = _migrate_seen_v1(state)
    elif version != STATE_SCHEMA_VERSION:
        raise StorageError("Unsupported seen job state schema; refusing to overwrite existing history")
    if not isinstance(state.get("initialized"), bool):
        raise StorageError("seen job state has an invalid initialized flag")
    if state.get("initialized_at") is not None and not isinstance(state.get("initialized_at"), str):
        raise StorageError("seen job state has an invalid initialized_at value")
    state["initialized_sources"] = _validate_initialized_sources(state.get("initialized_sources", {}))
    if not isinstance(state.get("jobs"), dict):
        raise StorageError("seen job state has an invalid jobs mapping")
    if not isinstance(state.get("pending_notifications"), dict):
        raise StorageError("seen job state has an invalid pending_notifications mapping")
    state["schema_version"] = STATE_SCHEMA_VERSION
    return state


def validate_current_state(value: Any) -> dict[str, Any]:
    """Validate a current snapshot, transparently upgrading v1 when needed."""

    current = dict(_require_mapping(value, "current job state"))
    version = current.get("schema_version")
    if version == 1:
        current = _migrate_current_v1(current)
    elif version != CURRENT_JOBS_SCHEMA_VERSION:
        raise StorageError("Unsupported current job state schema")
    if not isinstance(current.get("jobs"), dict):
        raise StorageError("current job state has an invalid jobs mapping")
    current["schema_version"] = CURRENT_JOBS_SCHEMA_VERSION
    return current


def load_seen_state(path: Path) -> dict[str, Any]:
    """Load permanent history, or return the explicit first-run state."""

    try:
        return validate_seen_state(_read_json(path))
    except FileNotFoundError:
        return empty_seen_state()


def load_current_state(path: Path) -> dict[str, Any]:
    """Load the last current snapshot, or return an empty one."""

    try:
        return validate_current_state(_read_json(path))
    except FileNotFoundError:
        return empty_current_state()


def serialise_json(value: Any) -> str:
    """Create stable, UTF-8-friendly JSON with no incidental formatting noise."""

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def atomic_write_text(path: Path, content: str) -> None:
    """Replace a file atomically after fully writing a sibling temporary file."""

    temporary_path: Path | None = None
    try:
        temporary_path = _stage_bytes(path, content.encode("utf-8"))
        os.replace(temporary_path, path)
    except OSError as error:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise StorageError(f"Could not atomically write {path}: {error}") from error


def _stage_bytes(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def write_json_if_changed(path: Path, value: Any) -> bool:
    content = serialise_json(value)
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except FileNotFoundError:
        pass
    except OSError as error:
        raise StorageError(f"Could not compare {path}: {error}") from error
    atomic_write_text(path, content)
    return True


def write_text_if_changed(path: Path, content: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == content:
            return False
    except FileNotFoundError:
        pass
    except OSError as error:
        raise StorageError(f"Could not compare {path}: {error}") from error
    atomic_write_text(path, content)
    return True


def write_texts_transactionally(entries: Mapping[Path, str]) -> tuple[Path, ...]:
    """Stage generated files and roll back replacements if one write fails."""

    originals: dict[Path, bytes | None] = {}
    staged: dict[Path, Path] = {}
    try:
        for path, content in entries.items():
            desired = content.encode("utf-8")
            try:
                original = path.read_bytes()
            except FileNotFoundError:
                original = None
            except OSError as error:
                raise StorageError(f"Could not compare {path}: {error}") from error
            if original == desired:
                continue
            originals[path] = original
            staged[path] = _stage_bytes(path, desired)
    except OSError as error:
        for temporary_path in staged.values():
            temporary_path.unlink(missing_ok=True)
        raise StorageError(f"Could not stage generated tracker files: {error}") from error

    replaced: list[Path] = []
    try:
        for path, temporary_path in staged.items():
            os.replace(temporary_path, path)
            replaced.append(path)
    except OSError as error:
        cleanup_errors: list[str] = []
        for temporary_path in staged.values():
            temporary_path.unlink(missing_ok=True)
        for path in reversed(replaced):
            original = originals[path]
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    restore_path = _stage_bytes(path, original)
                    os.replace(restore_path, path)
            except OSError as restore_error:
                cleanup_errors.append(f"{path}: {restore_error}")
        rollback_note = f" Rollback errors: {'; '.join(cleanup_errors)}" if cleanup_errors else ""
        raise StorageError(f"Could not replace generated tracker files: {error}.{rollback_note}") from error
    return tuple(staged)

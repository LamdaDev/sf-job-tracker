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
    APPLICATION_QUESTIONS_SCHEMA_VERSION,
    CURRENT_JOBS_SCHEMA_VERSION,
    LEGACY_SPEEDY_SOURCE_IDS,
    LOCATION_SCOPE_VERSION,
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
        "location_scope_version": LOCATION_SCOPE_VERSION,
        "pending_notifications": {},
        "schema_version": STATE_SCHEMA_VERSION,
    }


def empty_current_state() -> dict[str, Any]:
    """Return the deterministic empty current-snapshot schema."""

    return {"jobs": {}, "schema_version": CURRENT_JOBS_SCHEMA_VERSION}


def empty_application_questions_state() -> dict[str, Any]:
    """Return the durable, intentionally empty application-scan state.

    Application scans are keyed by canonical job identity rather than a source
    URL, so a requisition observed through several upstream feeds still has
    exactly one public question record.
    """

    return {"scans": {}, "schema_version": APPLICATION_QUESTIONS_SCHEMA_VERSION}


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
    location_scope_version = value.get("location_scope_version")
    if location_scope_version is not None and not isinstance(location_scope_version, str):
        raise StorageError("legacy seen job state has an invalid location_scope_version")
    jobs = value.get("jobs")
    pending = value.get("pending_notifications")
    if not isinstance(jobs, Mapping) or not isinstance(pending, Mapping):
        raise StorageError("legacy seen job state has invalid jobs or pending_notifications")

    migrated = empty_seen_state()
    migrated["initialized"] = value["initialized"]
    migrated["initialized_at"] = value.get("initialized_at")
    # Preserve the old scope marker when available. Its absence is meaningful:
    # the tracker will silently rebaseline it instead of alerting a scope
    # expansion after migration.
    if location_scope_version is None:
        migrated.pop("location_scope_version")
    else:
        migrated["location_scope_version"] = location_scope_version
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
    location_scope_version = state.get("location_scope_version")
    if location_scope_version is not None and not isinstance(location_scope_version, str):
        raise StorageError("seen job state has an invalid location_scope_version")
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


# Answers and candidate data have no place in this public repository. These
# are deliberately key-based checks: a question label such as "Email" is safe
# and useful, while a field named ``answer`` or ``resume_contents`` is not.
_PROHIBITED_APPLICATION_SCAN_KEYS = frozenset(
    {
        "answer",
        "answers",
        "suggestedanswer",
        "personalanswer",
        "candidateanswer",
        "resumetext",
        "resumecontents",
        "coverletter",
        "candidateprofile",
        "candidateemail",
        "candidatephone",
        "sessioncookie",
        "sessioncookies",
        "cookie",
        "cookies",
        "setcookie",
        "csrf",
        "csrftoken",
        "token",
        "accesstoken",
        "authtoken",
        "refreshtoken",
        "idtoken",
        "sessiontoken",
        "bearertoken",
        "authorization",
        "authorizationheader",
        "credential",
        "credentials",
        "password",
        "passwordhash",
        "localstorage",
    }
)

_PROHIBITED_APPLICATION_SCAN_KEY_FRAGMENTS = (
    "answer",
    "resume",
    "coverletter",
    "candidate",
    "cookie",
    "csrf",
    "token",
    "authorization",
    "credential",
    "password",
    "localstorage",
)


def _normalized_scan_key(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())


def _validate_safe_application_value(value: Any, *, label: str) -> None:
    """Reject accidental candidate/secrets fields at every nested level."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise StorageError(f"{label} has a non-string key")
            normalized = _normalized_scan_key(key)
            if normalized in _PROHIBITED_APPLICATION_SCAN_KEYS or any(
                fragment in normalized for fragment in _PROHIBITED_APPLICATION_SCAN_KEY_FRAGMENTS
            ):
                raise StorageError(f"{label} contains prohibited candidate or credential field {key!r}")
            _validate_safe_application_value(nested, label=f"{label}.{key}")
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_safe_application_value(nested, label=f"{label}[{index}]")
        return
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise StorageError(f"{label} has an unsupported JSON value")


def _validate_application_scan_record(canonical_job_id: str, value: Any) -> dict[str, Any]:
    record = dict(_require_mapping(value, f"application scan {canonical_job_id}"))
    _validate_safe_application_value(record, label=f"application scan {canonical_job_id}")

    required_strings = ("canonical_job_id", "provider", "application_url", "status")
    for name in required_strings:
        if not isinstance(record.get(name), str) or not record[name].strip():
            raise StorageError(f"application scan {canonical_job_id} has an invalid {name}")
    if record["status"] not in {"complete", "partial", "unsupported", "unavailable", "failed"}:
        raise StorageError(f"application scan {canonical_job_id} has an unsupported status")
    if record["canonical_job_id"] != canonical_job_id:
        raise StorageError("application scan record key must match canonical_job_id")
    if not isinstance(record.get("questions"), list):
        raise StorageError(f"application scan {canonical_job_id} has invalid questions")
    if not all(isinstance(question, Mapping) for question in record["questions"]):
        raise StorageError(f"application scan {canonical_job_id} questions must be objects")

    for name in ("completeness_reason", "scanned_at", "first_scanned", "last_scanned", "error_type", "error_message"):
        if name in record and record[name] is not None and not isinstance(record[name], str):
            raise StorageError(f"application scan {canonical_job_id} has an invalid {name}")
    if "http_status" in record and record["http_status"] is not None and not isinstance(record["http_status"], int):
        raise StorageError(f"application scan {canonical_job_id} has an invalid http_status")
    if "attempt_count" in record and (
        not isinstance(record["attempt_count"], int) or isinstance(record["attempt_count"], bool) or record["attempt_count"] < 1
    ):
        raise StorageError(f"application scan {canonical_job_id} has an invalid attempt_count")
    if "issue_number" in record and record["issue_number"] is not None and (
        not isinstance(record["issue_number"], int) or isinstance(record["issue_number"], bool) or record["issue_number"] < 1
    ):
        raise StorageError(f"application scan {canonical_job_id} has an invalid issue_number")
    if "issue_update_pending" in record and not isinstance(record["issue_update_pending"], bool):
        raise StorageError(f"application scan {canonical_job_id} has an invalid issue_update_pending")
    if "metadata" in record and not isinstance(record["metadata"], Mapping):
        raise StorageError(f"application scan {canonical_job_id} has invalid metadata")
    return record


def validate_application_questions_state(value: Any) -> dict[str, Any]:
    """Validate the separate public question-definition store.

    This intentionally has no migration from job history: enabling enrichment
    must never turn every historical canonical job into a scan candidate.
    """

    state = dict(_require_mapping(value, "application question state"))
    if state.get("schema_version") != APPLICATION_QUESTIONS_SCHEMA_VERSION:
        raise StorageError("Unsupported application question state schema")
    scans = state.get("scans")
    if not isinstance(scans, Mapping):
        raise StorageError("application question state has an invalid scans mapping")
    validated_scans: dict[str, dict[str, Any]] = {}
    for canonical_job_id, record in scans.items():
        if not isinstance(canonical_job_id, str) or not canonical_job_id:
            raise StorageError("application question state has an invalid canonical job ID")
        validated_scans[canonical_job_id] = _validate_application_scan_record(canonical_job_id, record)
    state["scans"] = validated_scans
    state["schema_version"] = APPLICATION_QUESTIONS_SCHEMA_VERSION
    return state


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


def load_application_questions_state(path: Path) -> dict[str, Any]:
    """Load public application question definitions without touching job state."""

    try:
        return validate_application_questions_state(_read_json(path))
    except FileNotFoundError:
        return empty_application_questions_state()


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

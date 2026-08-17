"""Validated, deterministic, atomic JSON storage helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .config import CURRENT_JOBS_SCHEMA_VERSION, STATE_SCHEMA_VERSION


class StorageError(RuntimeError):
    """Raised when persisted tracker state is missing or invalid."""


def empty_seen_state() -> dict[str, Any]:
    """Return the explicit uninitialized permanent-history schema."""

    return {
        "initialized": False,
        "initialized_at": None,
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


def validate_seen_state(value: Any) -> dict[str, Any]:
    """Validate enough of the state contract to prevent unsafe mutations."""

    state = dict(_require_mapping(value, "seen job state"))
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise StorageError(
            "Unsupported seen job state schema; refusing to overwrite existing history"
        )
    if not isinstance(state.get("initialized"), bool):
        raise StorageError("seen job state has an invalid initialized flag")
    if state.get("initialized_at") is not None and not isinstance(state.get("initialized_at"), str):
        raise StorageError("seen job state has an invalid initialized_at value")
    if not isinstance(state.get("jobs"), dict):
        raise StorageError("seen job state has an invalid jobs mapping")
    if not isinstance(state.get("pending_notifications"), dict):
        raise StorageError("seen job state has an invalid pending_notifications mapping")
    return state


def validate_current_state(value: Any) -> dict[str, Any]:
    """Validate the current snapshot before using it for a byte comparison."""

    current = dict(_require_mapping(value, "current job state"))
    if current.get("schema_version") != CURRENT_JOBS_SCHEMA_VERSION:
        raise StorageError("Unsupported current job state schema")
    if not isinstance(current.get("jobs"), dict):
        raise StorageError("current job state has an invalid jobs mapping")
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

    path.parent.mkdir(parents=True, exist_ok=True)
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
    """Write and fsync a sibling temporary file without changing its target."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
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
    """Atomically write JSON only when its deterministic bytes are different."""

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
    """Atomically write arbitrary generated text only when it has changed."""

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
    """Stage generated files and roll back replacements if one write fails.

    A filesystem cannot atomically replace several independent files in a
    single operation. This provides the next best reliability guarantee: all
    replacement content is fully staged before anything changes, and a failed
    replacement restores the original bytes of every file already replaced.
    """

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

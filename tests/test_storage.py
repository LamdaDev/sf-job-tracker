from __future__ import annotations

from pathlib import Path

import pytest

import src.storage as storage
from src.storage import StorageError, write_texts_transactionally


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

from __future__ import annotations

import src.fetcher as fetcher
from src.config import SourceConfig


def test_fetcher_resolves_each_repository_ref_once_and_uses_immutable_raw_urls(
    monkeypatch,
) -> None:
    first = SourceConfig("one", "One", "owner/repo", "main", "one.txt", "Internship", "fixture")
    second = SourceConfig("two", "One", "owner/repo", "main", "two.txt", "Internship", "fixture")
    third = SourceConfig("three", "Three", "other/repo", "dev", "three.txt", "New Grad", "fixture")
    resolved: list[tuple[str, str]] = []
    fetched: list[str] = []

    def resolve(repository: str, ref: str) -> str:
        resolved.append((repository, ref))
        return {("owner/repo", "main"): "a" * 40, ("other/repo", "dev"): "b" * 40}[(repository, ref)]

    def fetch(url: str, **_: object) -> str:
        fetched.append(url)
        return f"document:{url.rsplit('/', 1)[-1]}"

    monkeypatch.setattr(fetcher, "SOURCES", (first, second, third))
    monkeypatch.setattr(fetcher, "resolve_commit_sha", resolve)
    monkeypatch.setattr(fetcher, "fetch_text", fetch)

    snapshot = fetcher.fetch_upstream_sources()

    assert resolved == [("owner/repo", "main"), ("other/repo", "dev")]
    assert set(snapshot.documents) == {first, second, third}
    assert snapshot.revisions == {"owner/repo@main": "a" * 40, "other/repo@dev": "b" * 40}
    assert all("/main/" not in url and "/dev/" not in url for url in fetched)
    assert snapshot.errors == {}


def test_fetcher_keeps_a_single_source_failure_as_unknown_when_others_succeed(monkeypatch) -> None:
    good = SourceConfig("good", "Good", "owner/repo", "main", "good.txt", "Internship", "fixture")
    bad = SourceConfig("bad", "Bad", "owner/repo", "main", "bad.txt", "Internship", "fixture")

    monkeypatch.setattr(fetcher, "SOURCES", (good, bad))
    monkeypatch.setattr(fetcher, "resolve_commit_sha", lambda *_: "a" * 40)

    def fetch(url: str, **_: object) -> str:
        if url.endswith("bad.txt"):
            raise fetcher.UpstreamFetchError("fixture failure")
        return "ok"

    monkeypatch.setattr(fetcher, "fetch_text", fetch)
    snapshot = fetcher.fetch_upstream_sources()
    assert snapshot.documents == {good: "ok"}
    assert snapshot.errors == {"bad": "fixture failure"}

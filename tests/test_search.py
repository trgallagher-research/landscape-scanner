"""Tests for the search layer: aggregation, dedup, cross-index agreement,
and graceful degradation. No network — providers are stubbed."""

from scanner.models import SearchHit
from scanner.search.base import MultiSearch, SearchError, SearchProvider, normalise_url


class StubProvider(SearchProvider):
    """A canned provider for tests."""

    def __init__(self, name: str, hits: list[SearchHit] | None = None, fail: bool = False):
        self.name = name
        self._hits = hits or []
        self._fail = fail

    def search(self, query: str, max_results: int = 10) -> list[SearchHit]:
        if self._fail:
            raise SearchError(f"{self.name} is down")
        return self._hits[:max_results]


def hit(url: str, index: str) -> SearchHit:
    return SearchHit(url=url, title=url, snippet="", index=index)


def test_merges_and_dedupes_by_url():
    a = StubProvider("serper", [hit("https://example.org/x", "serper")])
    b = StubProvider("brave", [hit("https://example.org/x", "brave"), hit("https://other.org", "brave")])
    multi = MultiSearch([a, b])
    results = multi.search("anything")
    urls = sorted(h.url for h in results)
    assert urls == ["https://example.org/x", "https://other.org"]


def test_cross_index_agreement_counts_independent_indexes():
    a = StubProvider("serper", [hit("https://example.org/x", "serper")])
    b = StubProvider("brave", [hit("https://example.org/x", "brave")])
    multi = MultiSearch([a, b])
    multi.search("anything")
    assert multi.index_count("https://example.org/x") == 2
    assert multi.index_count("https://nowhere.org") == 0


def test_failed_provider_degrades_not_crashes():
    """One provider down -> results from the other, failure recorded."""
    good = StubProvider("serper", [hit("https://example.org", "serper")])
    bad = StubProvider("brave", fail=True)
    multi = MultiSearch([good, bad])
    results = multi.search("anything")
    assert len(results) == 1
    assert "brave" in multi.failures


def test_requires_at_least_one_provider():
    import pytest

    with pytest.raises(ValueError):
        MultiSearch([])


def test_normalise_url_dedupe_rules():
    """Fragments and trailing slashes never distinguish two pages; host
    case is ignored; path case is preserved."""
    assert normalise_url("HTTPS://Example.ORG/Path/") == "https://example.org/Path"
    assert normalise_url("https://example.org/page#section") == "https://example.org/page"
    assert normalise_url("") == ""

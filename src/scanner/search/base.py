"""The search provider interface and the multi-index aggregator.

Design rules:

* A provider that is not configured (no key) is simply NOT constructed —
  there is no fake fallback. The pipeline decides up front which providers
  are live and the report manifest records it.
* A provider that fails at runtime degrades the run, never crashes it:
  the aggregator records the failure and continues with the others. The
  failure note ends up in the report's coverage notes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import SearchHit


class SearchError(Exception):
    """A search provider failed (bad key, network error, rate limit)."""


class SearchProvider(ABC):
    """One search index (Serper, Brave, ...). Implementations are thin
    HTTP wrappers that normalise results into ``SearchHit``s."""

    #: Short name used as the hit's ``index`` label and in the manifest.
    name: str = "unnamed"

    @abstractmethod
    def search(self, query: str, max_results: int = 10) -> list[SearchHit]:
        """Run one query and return normalised hits.

        Raises ``SearchError`` on failure; the aggregator handles it.
        """


class MultiSearch:
    """Runs every live provider for each query and merges the results.

    Deduplicates by URL while REMEMBERING every index that returned each
    URL — that record is what powers cross-index agreement (an entity found
    by two independent engines deserves more confidence than one engine's
    single hit).
    """

    def __init__(self, providers: list[SearchProvider]):
        """providers: the live, key-configured providers for this run."""
        if not providers:
            raise ValueError("MultiSearch needs at least one live search provider.")
        self.providers = providers
        # url -> set of index names that returned it (across ALL queries this run)
        self.indexes_by_url: dict[str, set[str]] = {}
        # provider name -> error string, recorded once per run for the manifest
        self.failures: dict[str, str] = {}

    def provider_names(self) -> list[str]:
        """Names of the providers this run is using (for the manifest)."""
        return [provider.name for provider in self.providers]

    def search(self, query: str, max_results_per_provider: int = 10) -> list[SearchHit]:
        """Run one query across all providers; return URL-deduplicated hits.

        The first hit seen for a URL wins (title/snippet), but every
        provider that returned the URL is recorded in ``indexes_by_url``.
        """
        merged: dict[str, SearchHit] = {}
        for provider in self.providers:
            try:
                hits = provider.search(query, max_results=max_results_per_provider)
            except SearchError as error:
                # Degrade, don't die: remember the failure, use the other providers.
                self.failures.setdefault(provider.name, str(error))
                continue
            for hit in hits:
                url = normalise_url(hit.url)
                if not url:
                    continue
                self.indexes_by_url.setdefault(url, set()).add(provider.name)
                if url not in merged:
                    merged[url] = hit
        return list(merged.values())

    def index_count(self, url: str) -> int:
        """How many independent indexes returned this URL during the run."""
        return len(self.indexes_by_url.get(normalise_url(url), set()))


def normalise_url(url: str) -> str:
    """Canonicalise a URL enough to deduplicate sensibly.

    Lowercases the scheme/host, strips fragments and trailing slashes.
    Deliberately conservative — over-merging different pages is worse than
    keeping a near-duplicate.
    """
    url = (url or "").strip()
    if not url:
        return ""
    # Split off the fragment (#...) — never significant for identity.
    url = url.split("#", 1)[0]
    # Lowercase scheme and host only (paths can be case-sensitive).
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if "/" in rest:
            host, path = rest.split("/", 1)
            url = f"{scheme.lower()}://{host.lower()}/{path}"
        else:
            url = f"{scheme.lower()}://{rest.lower()}"
    return url.rstrip("/")

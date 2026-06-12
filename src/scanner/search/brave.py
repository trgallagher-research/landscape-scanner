"""Brave Search — an independent second index.

Brave maintains its own crawl rather than wrapping Google, which is what
makes "found by both Serper AND Brave" a meaningful confidence signal
(cross-index agreement) instead of the same engine agreeing with itself.
"""

from __future__ import annotations

import httpx

from ..models import SearchHit
from .base import SearchError, SearchProvider

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveProvider(SearchProvider):
    """Thin wrapper over the Brave Web Search API."""

    name = "brave"

    def __init__(self, api_key: str, timeout_s: float = 15.0):
        """api_key: the Brave Search key (caller resolves it via ProviderKeys)."""
        if not api_key:
            raise ValueError("BraveProvider requires an API key.")
        self.api_key = api_key
        self.timeout_s = timeout_s

    def search(self, query: str, max_results: int = 10) -> list[SearchHit]:
        """GET the query from Brave and normalise the web results."""
        try:
            response = httpx.get(
                BRAVE_ENDPOINT,
                headers={
                    "X-Subscription-Token": self.api_key,
                    "Accept": "application/json",
                },
                params={"q": query, "count": min(max_results, 20)},
                timeout=self.timeout_s,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise SearchError(f"Brave request failed: {error}") from error

        payload = response.json()
        results = payload.get("web", {}).get("results", [])
        hits: list[SearchHit] = []
        for row in results[:max_results]:
            url = row.get("url", "")
            if not url:
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=row.get("title", ""),
                    snippet=row.get("description", ""),
                    index=self.name,
                )
            )
        return hits

"""Serper.dev — Google-backed web search. The primary discovery index."""

from __future__ import annotations

import httpx

from ..models import SearchHit
from .base import SearchError, SearchProvider

SERPER_ENDPOINT = "https://google.serper.dev/search"


class SerperProvider(SearchProvider):
    """Thin wrapper over the Serper JSON API.

    Serper returns Google results as JSON; we map its "organic" list into
    ``SearchHit``s labelled with index "serper".
    """

    name = "serper"

    def __init__(self, api_key: str, timeout_s: float = 15.0):
        """api_key: the Serper key (caller resolves it via ProviderKeys)."""
        if not api_key:
            raise ValueError("SerperProvider requires an API key.")
        self.api_key = api_key
        self.timeout_s = timeout_s

    def search(self, query: str, max_results: int = 10) -> list[SearchHit]:
        """POST the query to Serper and normalise the organic results."""
        try:
            response = httpx.post(
                SERPER_ENDPOINT,
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                json={"q": query, "num": max_results},
                timeout=self.timeout_s,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise SearchError(f"Serper request failed: {error}") from error

        payload = response.json()
        hits: list[SearchHit] = []
        for row in payload.get("organic", [])[:max_results]:
            url = row.get("link", "")
            if not url:
                continue
            hits.append(
                SearchHit(
                    url=url,
                    title=row.get("title", ""),
                    snippet=row.get("snippet", ""),
                    index=self.name,
                )
            )
        return hits

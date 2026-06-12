"""Search layer: one interface, multiple providers, merged results.

The rest of the pipeline never knows which search engine produced a hit —
it sees a deduplicated list of ``SearchHit``s, each labelled with the index
that returned it so cross-index agreement can be computed.
"""

from .base import SearchError, SearchProvider, MultiSearch
from .serper import SerperProvider
from .brave import BraveProvider

__all__ = [
    "SearchError",
    "SearchProvider",
    "MultiSearch",
    "SerperProvider",
    "BraveProvider",
]

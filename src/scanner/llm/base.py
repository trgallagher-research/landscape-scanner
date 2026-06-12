"""The provider-agnostic LLM client interface.

Every adapter (Anthropic, OpenRouter) implements ``complete()`` and returns
an ``LLMResponse`` carrying the REAL token usage the API reported — that
usage is what the budget meter bills, so adapters must never fabricate or
omit it.

Hard rules encoded at this layer:

* ``max_tokens`` is generous by default (8192) and is NEVER used as a cost
  control — a tight cap on a reasoning-capable model burns the whole budget
  on hidden thinking and returns nothing, which is the worst possible spend.
* Reasoning/thinking is explicitly disabled in every adapter; any reasoning
  tokens that appear anyway are reported in usage so the meter's alarm fires.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..budget import TokenUsage


class LLMError(Exception):
    """A model call failed (auth, network, rate limit, empty response).

    The router catches this and falls back to the next model in the chain.
    """


@dataclass
class LLMResponse:
    """One completed model call."""

    text: str                       # the model's visible output
    model: str                      # the model id that actually served the call
    usage: TokenUsage = field(default_factory=TokenUsage)  # real counts from the API


class LLMClient(ABC):
    """One provider (Anthropic direct, OpenRouter, ...)."""

    #: Short name used in the provider manifest ("anthropic", "openrouter").
    name: str = "unnamed"

    @abstractmethod
    def complete(
        self,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Run one completion and return text + real usage.

        Raises ``LLMError`` on any failure. Must populate
        ``LLMResponse.usage`` from the API's usage fields, including
        reasoning tokens when the API reports them.
        """

"""Anthropic adapter — Claude models via the Messages API.

Claude holds the trust-critical roles in every profile: the verification
relation-check, framing, and the final synthesis. The adapter is a thin
httpx wrapper (no SDK dependency) with two cost behaviours worth noting:

* **Prompt caching.** The system prompt is sent with ``cache_control`` so
  repeated task calls (the same system prompt dozens of times per run)
  read it from cache at 10% of the input price. Harmless no-op when the
  prompt is below the model's minimum cacheable length.
* **No extended thinking.** We never enable thinking on pipeline tasks —
  they are extraction/selection jobs, and hidden reasoning spend was a
  named failure mode this project is designed against.

The usage we report folds cache reads/writes into a single
cost-equivalent input figure so the budget meter stays simple and never
under-bills.
"""

from __future__ import annotations

import httpx

from ..budget import TokenUsage
from .base import LLMClient, LLMError, LLMResponse

ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicClient(LLMClient):
    """Thin wrapper over the Anthropic Messages API."""

    name = "anthropic"

    def __init__(self, api_key: str, timeout_s: float = 120.0):
        """api_key: the Anthropic key (caller resolves it via ProviderKeys)."""
        if not api_key:
            raise ValueError("AnthropicClient requires an API key.")
        self.api_key = api_key
        self.timeout_s = timeout_s

    def complete(
        self,
        model: str,
        system: str,
        prompt: str,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """One message completion. Raises LLMError on any failure."""
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # System prompt as a cacheable block: per-task system prompts
            # repeat many times per run, so cache reads cut input cost.
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            response = httpx.post(
                ANTHROPIC_ENDPOINT,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
                json=body,
                timeout=self.timeout_s,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise LLMError(f"Anthropic request failed: {error}") from error

        payload = response.json()
        text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )
        if not text.strip():
            raise LLMError(f"Anthropic model {model} returned empty content.")

        return LLMResponse(
            text=text,
            model=payload.get("model", model),
            usage=_usage_from_payload(payload),
        )


def _usage_from_payload(payload: dict) -> TokenUsage:
    """Convert Anthropic usage into the meter's TokenUsage.

    Cache economics are folded into a cost-equivalent input figure:
    cache WRITES bill at 1.25x the input price and cache READS at 0.10x,
    so: equivalent_input = input + 1.25*cache_write + 0.10*cache_read.
    This slightly simplifies (one number instead of three) without ever
    under-billing.
    """
    usage_block = payload.get("usage", {}) or {}
    input_tokens = int(usage_block.get("input_tokens", 0) or 0)
    cache_write = int(usage_block.get("cache_creation_input_tokens", 0) or 0)
    cache_read = int(usage_block.get("cache_read_input_tokens", 0) or 0)
    equivalent_input = round(input_tokens + 1.25 * cache_write + 0.10 * cache_read)
    return TokenUsage(
        input_tokens=equivalent_input,
        output_tokens=int(usage_block.get("output_tokens", 0) or 0),
        reasoning_tokens=0,  # thinking is never enabled on pipeline tasks
    )

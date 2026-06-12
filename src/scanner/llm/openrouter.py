"""OpenRouter adapter — access to economy extraction models (DeepSeek, Kimi).

OpenRouter exposes an OpenAI-compatible chat-completions API over many
models. We use it ONLY with non-thinking model variants, and we explicitly
disable reasoning in the request as a second line of defence. If a routed
model emits reasoning tokens anyway, they are reported in usage so the
budget meter's alarm fires and the report manifest carries a note.
"""

from __future__ import annotations

import httpx

from ..budget import TokenUsage
from .base import LLMClient, LLMError, LLMResponse

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterClient(LLMClient):
    """Thin wrapper over the OpenRouter chat-completions API."""

    name = "openrouter"

    def __init__(self, api_key: str, timeout_s: float = 120.0):
        """api_key: the OpenRouter key (caller resolves it via ProviderKeys)."""
        if not api_key:
            raise ValueError("OpenRouterClient requires an API key.")
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
        """One chat completion with reasoning explicitly disabled."""
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Belt and braces: we already pin non-thinking variants, and we
            # also tell OpenRouter not to spend on hidden reasoning.
            "reasoning": {"enabled": False},
        }
        try:
            response = httpx.post(
                OPENROUTER_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.timeout_s,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise LLMError(f"OpenRouter request failed: {error}") from error

        payload = response.json()
        try:
            choice = payload["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError) as error:
            raise LLMError(f"OpenRouter returned an unexpected shape: {error}") from error

        if not text.strip():
            # An empty completion is a failure — most often a model burning
            # its budget on reasoning. Surface it; the router will fall back.
            raise LLMError(f"OpenRouter model {model} returned empty content.")

        usage = _usage_from_payload(payload)
        served_model = payload.get("model", model)
        return LLMResponse(text=text, model=served_model, usage=usage)


def _usage_from_payload(payload: dict) -> TokenUsage:
    """Extract real token usage, including hidden reasoning tokens.

    OpenRouter reports reasoning under
    ``usage.completion_tokens_details.reasoning_tokens`` when present.
    Visible output = completion_tokens - reasoning_tokens, so the meter
    never double-bills.
    """
    usage_block = payload.get("usage", {}) or {}
    completion_tokens = int(usage_block.get("completion_tokens", 0) or 0)
    details = usage_block.get("completion_tokens_details", {}) or {}
    reasoning_tokens = int(details.get("reasoning_tokens", 0) or 0)
    return TokenUsage(
        input_tokens=int(usage_block.get("prompt_tokens", 0) or 0),
        output_tokens=max(0, completion_tokens - reasoning_tokens),
        reasoning_tokens=reasoning_tokens,
    )

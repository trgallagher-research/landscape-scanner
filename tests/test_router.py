"""Tests for the model router: profile routing, JSON validation with retry,
fallback to the trusted model, and budget metering. No network — clients
are stubs."""

import pytest
from pydantic import BaseModel

from scanner.budget import BudgetExceeded, BudgetMeter, TokenUsage
from scanner.llm.base import LLMClient, LLMError, LLMResponse
from scanner.llm.router import PROFILES, ModelRouter


class ReplySchema(BaseModel):
    """Tiny schema for the JSON-call tests."""

    answer: str


class StubClient(LLMClient):
    """Returns scripted replies in order; records every call it gets."""

    def __init__(self, name: str, replies: list[str] | None = None, fail: bool = False):
        self.name = name
        self._replies = list(replies or [])
        self._fail = fail
        self.calls: list[str] = []  # models requested, in order

    def complete(self, model, system, prompt, max_tokens=8192, temperature=0.0):
        self.calls.append(model)
        if self._fail:
            raise LLMError(f"{self.name} is down")
        text = self._replies.pop(0) if self._replies else '{"answer": "ok"}'
        return LLMResponse(text=text, model=model, usage=TokenUsage(input_tokens=100, output_tokens=10))


def make_router(anthropic=None, openrouter=None, profile="economy", budget=10.0):
    clients = {"anthropic": anthropic or StubClient("anthropic")}
    if openrouter is not None:
        clients["openrouter"] = openrouter
    return ModelRouter(clients, BudgetMeter(budget_usd=budget), profile=profile)


def test_economy_routes_extraction_to_openrouter():
    openrouter = StubClient("openrouter")
    router = make_router(openrouter=openrouter)
    router.call_json("span_discovery", "sys", "prompt", ReplySchema)
    assert openrouter.calls  # extraction went to the cheap provider


def test_relation_check_always_routes_to_anthropic():
    """The verification verdict never runs on an economy model."""
    for profile in PROFILES:
        client_name, model, _ = PROFILES[profile]["relation_check"]
        assert client_name == "anthropic"


def test_missing_openrouter_falls_back_transparently():
    """No OpenRouter key -> economy tasks run on Claude instead of failing."""
    anthropic = StubClient("anthropic")
    router = make_router(anthropic=anthropic, openrouter=None)
    result = router.call_json("span_discovery", "sys", "prompt", ReplySchema)
    assert result.answer == "ok"
    assert anthropic.calls


def test_invalid_json_retried_once_then_falls_back():
    """Bad JSON twice from the cheap model -> the call lands on Claude."""
    openrouter = StubClient("openrouter", replies=["not json", "still not json"])
    anthropic = StubClient("anthropic", replies=['{"answer": "rescued"}'])
    router = make_router(anthropic=anthropic, openrouter=openrouter)
    result = router.call_json("triage", "sys", "prompt", ReplySchema)
    assert result.answer == "rescued"
    assert len(openrouter.calls) == 2  # original + one retry
    assert len(anthropic.calls) == 1   # fallback
    assert "fallback" in router.models_used["triage"]


def test_provider_error_goes_straight_to_fallback():
    openrouter = StubClient("openrouter", fail=True)
    anthropic = StubClient("anthropic", replies=['{"answer": "rescued"}'])
    router = make_router(anthropic=anthropic, openrouter=openrouter)
    result = router.call_json("triage", "sys", "prompt", ReplySchema)
    assert result.answer == "rescued"
    assert len(openrouter.calls) == 1  # no pointless retry on a dead provider


def test_markdown_fenced_json_is_tolerated():
    openrouter = StubClient("openrouter", replies=['```json\n{"answer": "fenced"}\n```'])
    router = make_router(openrouter=openrouter)
    result = router.call_json("triage", "sys", "prompt", ReplySchema)
    assert result.answer == "fenced"


def test_every_call_is_metered():
    openrouter = StubClient("openrouter")
    router = make_router(openrouter=openrouter)
    router.call_json("triage", "sys", "prompt", ReplySchema)
    assert router.meter.llm_calls == 1
    assert router.meter.calls_by_task == {"triage": 1}


def test_budget_stop_propagates():
    """When the meter cap fires mid-run, the exception reaches the caller
    (the pipeline persists state and halts resumably)."""
    openrouter = StubClient("openrouter")
    router = make_router(openrouter=openrouter, budget=0.0000001)
    with pytest.raises(BudgetExceeded):
        router.call_json("triage", "sys", "prompt", ReplySchema)


def test_router_requires_anthropic():
    """The verification anchor is non-negotiable."""
    with pytest.raises(ValueError, match="anthropic"):
        ModelRouter({"openrouter": StubClient("openrouter")}, BudgetMeter())

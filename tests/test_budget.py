"""Tests for the budget meter: real-token cost maths, the hard stop, and
the reasoning-token alarm."""

import pytest

from scanner.budget import (
    DEFAULT_PRICE,
    BudgetExceeded,
    BudgetMeter,
    TokenUsage,
    cost_of,
    price_for,
)


def test_known_model_priced_exactly():
    """1M input + 1M output tokens on Haiku = $1 + $5."""
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_of("claude-haiku-4-5", usage) == pytest.approx(6.00)


def test_dated_model_id_matches_base_price():
    """Model ids with date suffixes resolve to the base model's price."""
    assert price_for("claude-haiku-4-5-20251001") == price_for("claude-haiku-4-5")


def test_unknown_model_uses_high_fallback_price():
    """Unpriced models must OVER-estimate so the budget stop fires early."""
    assert price_for("mystery-model-9000") == DEFAULT_PRICE


def test_reasoning_tokens_are_billed_as_output():
    """Hidden chain-of-thought is billed by providers — meter it the same way."""
    visible_only = cost_of("claude-haiku-4-5", TokenUsage(output_tokens=1000))
    with_reasoning = cost_of(
        "claude-haiku-4-5", TokenUsage(output_tokens=1000, reasoning_tokens=4000)
    )
    assert with_reasoning == pytest.approx(visible_only * 5)


def test_meter_accumulates_and_reports():
    meter = BudgetMeter(budget_usd=10.0)
    meter.record("triage", "claude-haiku-4-5", TokenUsage(input_tokens=10_000, output_tokens=1_000))
    meter.record("triage", "claude-haiku-4-5", TokenUsage(input_tokens=10_000, output_tokens=1_000))
    summary = meter.summary()
    assert summary.llm_calls == 2
    assert summary.by_task == {"triage": 2}
    assert summary.input_tokens == 20_000
    assert summary.total_usd > 0


def test_hard_stop_fires_when_budget_reached():
    """Spend reaching the cap raises BudgetExceeded — after recording the
    breaching call so the books stay accurate."""
    meter = BudgetMeter(budget_usd=0.01)
    big_call = TokenUsage(input_tokens=5_000_000, output_tokens=0)  # ~$5 on Haiku
    with pytest.raises(BudgetExceeded):
        meter.record("span_discovery", "claude-haiku-4-5", big_call)
    assert meter.llm_calls == 1  # the call was still recorded
    assert meter.spent_usd > 0.01


def test_reasoning_alarm_latches():
    """Any reasoning tokens set the alarm, and it stays set."""
    meter = BudgetMeter(budget_usd=100.0)
    assert meter.reasoning_alarm is False
    meter.record("triage", "claude-haiku-4-5", TokenUsage(output_tokens=10, reasoning_tokens=5))
    meter.record("triage", "claude-haiku-4-5", TokenUsage(output_tokens=10))
    assert meter.reasoning_alarm is True


def test_remaining_never_negative():
    meter = BudgetMeter(budget_usd=0.001)
    try:
        meter.record(
            "read", "claude-sonnet-4-6", TokenUsage(input_tokens=1_000_000, output_tokens=100_000)
        )
    except BudgetExceeded:
        pass
    assert meter.remaining_usd() == 0.0

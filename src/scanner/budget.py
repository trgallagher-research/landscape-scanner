"""Run-level cost metering from REAL token usage.

Lessons from v1 baked in here:

* Cost is measured from the ``usage`` fields each API returns (input,
  output, and reasoning token counts), not from a flat per-call guess.
* The user's budget is a HARD, RESUMABLE stop: when measured spend reaches
  the cap, the pipeline halts with ``BudgetExceeded`` and can resume after
  the user raises the budget. It never silently overspends.
* Reasoning tokens should always be ZERO (the pipeline pins non-thinking
  model variants). If any appear, the meter raises an alarm flag that ends
  up in the report's provider manifest — hidden chain-of-thought spend was
  a real money sink in past projects and must never be invisible again.
* Cost control NEVER happens by strangling a call's max_tokens (that
  produces expensive null responses on reasoning-capable models); it only
  happens here, at the run level.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import CostSummary

# ---------------------------------------------------------------------------
# Pricing table — USD per MILLION tokens (input, output).
#
# These are approximate list prices, recorded 2026-06. Update when providers
# change pricing. Unknown models fall back to DEFAULT_PRICE, which is set
# deliberately HIGH so an unpriced model can only over-estimate cost (the
# budget stop fires early, never late).
# ---------------------------------------------------------------------------

MODEL_PRICES: dict[str, tuple[float, float]] = {
    # Anthropic (direct API)
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    # Economy extraction models via OpenRouter (non-thinking variants)
    "deepseek/deepseek-chat-v3-0324": (0.30, 1.20),
    "moonshotai/kimi-k2": (0.60, 2.50),
}

# Conservative fallback for any model not in the table (over-estimates).
DEFAULT_PRICE: tuple[float, float] = (5.00, 25.00)


@dataclass
class TokenUsage:
    """Token counts reported by one API response."""

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0  # hidden chain-of-thought tokens; should be zero


class BudgetExceeded(Exception):
    """Raised when measured spend reaches the user's budget.

    The pipeline catches this, persists state, and halts resumably — the
    user can raise the budget and continue the same run.
    """


def price_for(model: str) -> tuple[float, float]:
    """Return (input, output) USD-per-million-token prices for a model.

    Tries an exact match first, then a match ignoring any date/version
    suffix (e.g. "claude-haiku-4-5-20251001" matches "claude-haiku-4-5").
    Unknown models get the deliberately-high DEFAULT_PRICE.
    """
    if model in MODEL_PRICES:
        return MODEL_PRICES[model]
    for known, prices in MODEL_PRICES.items():
        if model.startswith(known):
            return prices
    return DEFAULT_PRICE


def cost_of(model: str, usage: TokenUsage) -> float:
    """Compute the USD cost of one call from its real token usage.

    Reasoning tokens are billed as output tokens (that is how providers
    bill them), which is exactly why they must be metered.
    """
    input_price, output_price = price_for(model)
    billed_output = usage.output_tokens + usage.reasoning_tokens
    return (usage.input_tokens * input_price + billed_output * output_price) / 1_000_000


@dataclass
class BudgetMeter:
    """Accumulates real spend across a run and enforces the budget cap.

    One meter lives for the whole run; every LLM call records its usage
    here immediately after the response arrives.
    """

    budget_usd: float = 2.0
    spent_usd: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    calls_by_task: dict[str, int] = field(default_factory=dict)
    reasoning_alarm: bool = False  # set permanently if any reasoning tokens appear

    def record(self, task: str, model: str, usage: TokenUsage) -> float:
        """Record one completed call; returns its cost in USD.

        Raises ``BudgetExceeded`` AFTER recording if the cap is now
        reached, so the spend that breached the cap is still accounted for.
        """
        call_cost = cost_of(model, usage)
        self.spent_usd += call_cost
        self.llm_calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.reasoning_tokens += usage.reasoning_tokens
        self.calls_by_task[task] = self.calls_by_task.get(task, 0) + 1

        if usage.reasoning_tokens > 0:
            # A pinned non-thinking model emitted hidden reasoning anyway.
            # Flag it loudly; the manifest will carry a note.
            self.reasoning_alarm = True

        if self.spent_usd >= self.budget_usd:
            raise BudgetExceeded(
                f"Budget reached: spent ${self.spent_usd:.2f} of ${self.budget_usd:.2f} "
                f"after {self.llm_calls} calls. Raise the budget to resume this run."
            )
        return call_cost

    def remaining_usd(self) -> float:
        """How much of the budget is left (never negative)."""
        return max(0.0, self.budget_usd - self.spent_usd)

    def summary(self) -> CostSummary:
        """Snapshot for the report's cost section."""
        return CostSummary(
            total_usd=round(self.spent_usd, 4),
            llm_calls=self.llm_calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            reasoning_tokens=self.reasoning_tokens,
            by_task=dict(self.calls_by_task),
        )

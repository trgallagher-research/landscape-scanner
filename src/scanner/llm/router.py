"""Per-task model routing, profiles, and schema-validated JSON calls.

This is where the cost strategy lives. Each pipeline task is routed to a
model chosen by the active profile:

* **economy** (default): cheap non-thinking models via OpenRouter handle
  the high-volume extraction work; Claude Haiku keeps the trust-critical
  verification verdict and the final synthesis. Cheap models can only
  degrade RECALL here, never truth — every span they produce is later
  validated verbatim against the source by deterministic string matching.
* **quality**: Claude everywhere.

Reliability strategy for cheap models: every JSON call is validated against
a Pydantic schema. On a parse/validation failure the same model gets ONE
retry with the error explained; if it fails again (or the provider errors),
the call falls back to the task's designated fallback model (always Claude).
Slightly slower sometimes, never broken.
"""

from __future__ import annotations

import json
import re
from typing import Literal, TypeVar

from pydantic import BaseModel, ValidationError

from ..budget import BudgetMeter
from .base import LLMClient, LLMError

# The eight pipeline tasks. Names appear in the budget's per-task call
# counts and the report manifest.
TaskName = Literal[
    "frame",              # turn the user's question into queries + landscape carve
    "triage",             # rank discovered candidates from snippets (batched)
    "extract_entities",   # pull candidate entity names from search results
    "span_discovery",     # extract value-spans from one scraped page  (cost dominator)
    "attribute_population",  # select attribute values from the span inventory
    "relation_check",     # verification verdict — trust-critical
    "segment",            # assign entities to landscape segments
    "read",               # final executive overview synthesis
]

# Route spec: (client_name, model_id, temperature).
# Extraction tasks run slightly warm (diverse phrasings reach more spans);
# verification runs cold (deterministic verdicts).
Route = tuple[str, str, float]

PROFILES: dict[str, dict[str, Route]] = {
    "economy": {
        "frame": ("anthropic", "claude-haiku-4-5", 0.2),
        "triage": ("openrouter", "deepseek/deepseek-chat-v3-0324", 0.0),
        "extract_entities": ("openrouter", "deepseek/deepseek-chat-v3-0324", 0.0),
        "span_discovery": ("openrouter", "deepseek/deepseek-chat-v3-0324", 0.4),
        "attribute_population": ("openrouter", "deepseek/deepseek-chat-v3-0324", 0.4),
        "relation_check": ("anthropic", "claude-haiku-4-5", 0.0),  # always Claude
        "segment": ("openrouter", "deepseek/deepseek-chat-v3-0324", 0.2),
        "read": ("anthropic", "claude-haiku-4-5", 0.2),
    },
    "quality": {
        "frame": ("anthropic", "claude-haiku-4-5", 0.2),
        "triage": ("anthropic", "claude-haiku-4-5", 0.0),
        "extract_entities": ("anthropic", "claude-haiku-4-5", 0.0),
        "span_discovery": ("anthropic", "claude-haiku-4-5", 0.4),
        "attribute_population": ("anthropic", "claude-haiku-4-5", 0.4),
        "relation_check": ("anthropic", "claude-haiku-4-5", 0.0),
        "segment": ("anthropic", "claude-haiku-4-5", 0.2),
        "read": ("anthropic", "claude-sonnet-4-6", 0.2),
    },
}

# Where a failed call falls back to: always the trusted provider.
FALLBACK_ROUTE: Route = ("anthropic", "claude-haiku-4-5", 0.0)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class ModelRouter:
    """Routes tasks to models, meters spend, validates JSON replies."""

    def __init__(
        self,
        clients: dict[str, LLMClient],
        meter: BudgetMeter,
        profile: str = "economy",
    ):
        """
        Parameters
        ----------
        clients:
            The live adapters, keyed by name (e.g. {"anthropic": ...,
            "openrouter": ...}). If "openrouter" is missing, economy-profile
            tasks transparently use the fallback route instead — the run
            still works, just at Claude prices.
        meter:
            The run's budget meter; every call records real usage here.
        profile:
            "economy" or "quality".
        """
        if profile not in PROFILES:
            raise ValueError(f"Unknown profile: {profile}")
        if "anthropic" not in clients:
            raise ValueError("ModelRouter requires the 'anthropic' client (verification anchor).")
        self.clients = clients
        self.meter = meter
        self.profile = profile
        # task -> model id actually used (for the report manifest)
        self.models_used: dict[str, str] = {}

    def route_for(self, task: str) -> Route:
        """The (client, model, temperature) the active profile assigns to a
        task — or the fallback route when that client isn't live."""
        client_name, model, temperature = PROFILES[self.profile][task]
        if client_name not in self.clients:
            return FALLBACK_ROUTE
        return client_name, model, temperature

    def call_json(
        self,
        task: str,
        system: str,
        prompt: str,
        schema: type[SchemaT],
        max_tokens: int = 8192,
    ) -> SchemaT:
        """Run a task and return its reply validated against ``schema``.

        Attempt order:
          1. The profile's model for the task.
          2. Same model once more, with the validation error explained.
          3. The fallback model (Claude Haiku), once.

        Raises ``LLMError`` only if all three attempts fail.
        """
        client_name, model, temperature = self.route_for(task)
        primary = self.clients[client_name]

        json_instruction = (
            "\n\nRespond with ONLY a JSON object matching this schema "
            "(no prose, no markdown fences):\n"
            + json.dumps(schema.model_json_schema(), indent=None)
        )
        full_prompt = prompt + json_instruction

        # Attempt 1: the routed model.
        error_note = ""
        for attempt in (1, 2):
            try:
                response = primary.complete(
                    model=model,
                    system=system,
                    prompt=full_prompt + error_note,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                self.meter.record(task, response.model, response.usage)
                result = _parse_json_reply(response.text, schema)
                self.models_used.setdefault(task, response.model)
                return result
            except (ValidationError, json.JSONDecodeError) as error:
                # Attempt 2 gets told exactly what was wrong with attempt 1.
                error_note = (
                    f"\n\nYour previous reply was invalid JSON for the schema "
                    f"({type(error).__name__}: {str(error)[:300]}). "
                    f"Reply again with ONLY the corrected JSON object."
                )
                if attempt == 2:
                    break
            except LLMError:
                break  # provider problem: go straight to fallback

        # Attempt 3: the trusted fallback model.
        fallback_client_name, fallback_model, fallback_temperature = FALLBACK_ROUTE
        fallback = self.clients[fallback_client_name]
        response = fallback.complete(
            model=fallback_model,
            system=system,
            prompt=full_prompt,
            max_tokens=max_tokens,
            temperature=fallback_temperature,
        )
        self.meter.record(task, response.model, response.usage)
        result = _parse_json_reply(response.text, schema)  # raises if still invalid
        self.models_used[task] = f"{response.model} (fallback)"
        return result


def _parse_json_reply(text: str, schema: type[SchemaT]) -> SchemaT:
    """Parse a model reply into the schema, tolerating markdown fences.

    Models occasionally wrap JSON in ```json fences or add a stray
    sentence; we extract the outermost JSON object before validating.
    Raises json.JSONDecodeError or pydantic.ValidationError on failure.
    """
    cleaned = text.strip()
    # Strip markdown code fences if present.
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    # Fall back to the outermost {...} if there is leading/trailing prose.
    if not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    return schema.model_validate(json.loads(cleaned))

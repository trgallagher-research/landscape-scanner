"""LLM layer: provider adapters, per-task model routing, and validated
JSON calls.

The pipeline never talks to a model directly. It asks the router to run a
named TASK ("triage", "span_discovery", "relation_check", ...); the router
picks the model from the active profile (economy/quality), makes the call,
meters real token usage against the run budget, validates the JSON reply
against a Pydantic schema, and retries/falls back on failure.
"""

from .base import LLMClient, LLMError, LLMResponse
from .router import ModelRouter, TaskName, PROFILES

__all__ = [
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "ModelRouter",
    "TaskName",
    "PROFILES",
]

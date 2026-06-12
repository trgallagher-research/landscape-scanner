"""Core data models for the Landscape Scanner.

Everything that flows through the pipeline — search hits, sources, claims,
entity cards, and the final report — is defined here as a Pydantic model so
it validates on construction and serialises cleanly to disk (resumable runs)
and to the HTML report.

Design principles carried over from the v1 Desk Research Engine:

* A claim is only "verified" when a VERBATIM quote supporting it was found
  in a real scraped source. There is no third state and no escalation.
* Entities whose existence cannot be confirmed are QUARANTINED (kept and
  flagged), never silently dropped and never silently included.
* Every report carries a provider manifest so a reader can see exactly
  which search engines and models produced it (no silent fake data).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Type aliases used across the models
# ---------------------------------------------------------------------------

# Outcome of attempting to fetch a source's full text.
#   "scraped"       — full text retrieved successfully
#   "unreachable"   — fetch attempted and failed (distinct from "no source")
#   "not_attempted" — outside the scrape depth for this entity
ScrapeStatus = Literal["scraped", "unreachable", "not_attempted"]

# A claim is verified only when a verbatim supporting quote was found.
Verdict = Literal["verified", "unverified"]

# How a verified claim was grounded (cheapest first).
#   restore_exact / restore_fuzzy / restore_value — deterministic matching,
#       no LLM involved; "restore_value" means a normalised number/date match.
#   llm_extract — the LLM relation-check returned a span, which was then
#       validated to be verbatim in the source before acceptance.
VerifyMethod = Literal["restore_exact", "restore_fuzzy", "restore_value", "llm_extract"]

# Confidence bands shown on every entity card.
ConfidenceLevel = Literal["high", "medium", "low"]

# What kind of thing an entity is (tagged during profiling; one run can mix forms).
EntityForm = Literal["programme", "product", "initiative", "policy_scheme", "organisation", "other"]


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Used to stamp sources and reports so the audit trail records when
    material was retrieved.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Search and sources
# ---------------------------------------------------------------------------

class SearchHit(BaseModel):
    """One result row returned by a search provider.

    Carries only what search APIs give us (URL, title, snippet) plus the
    label of the index that returned it, so cross-index agreement can be
    computed later. Snippets are used for discovery and triage ONLY —
    verification always runs on scraped full text.
    """

    url: str
    title: str = ""
    snippet: str = ""
    index: str = "serper"  # which provider returned this hit (e.g. "serper", "brave")


class SourceRef(BaseModel):
    """A source attached to an entity, before and after scraping.

    Starts life as a search hit; the scrape step fills in ``full_text`` and
    sets ``scrape_status``. ``full_text`` is what claims are verified
    against; it is stripped before the source goes into a report (reports
    keep the URL and title, not megabytes of page text).
    """

    url: str
    title: str = ""
    snippet: str = ""
    index: str = "serper"
    retrieved_at: str = Field(default_factory=utc_now_iso)
    scrape_status: ScrapeStatus = "not_attempted"
    full_text: Optional[str] = None

    def without_full_text(self) -> "SourceRef":
        """Return a copy with the page text removed (for compact reports)."""
        return self.model_copy(update={"full_text": None})


# ---------------------------------------------------------------------------
# Claims — the unit of verification
# ---------------------------------------------------------------------------

class Claim(BaseModel):
    """A single, atomic, checkable statement about an entity.

    Examples: "Founded in 2015", "Operates in 14 counties", "Funded by the
    Mastercard Foundation". A claim with ``label`` set doubles as a key
    feature shown on the entity card.

    Verification contract:
    * ``verdict == "verified"`` REQUIRES ``supporting_span`` (a verbatim
      quote from a scraped source) and ``source_url``.
    * Anything that could not be grounded stays "unverified" and the UI
      must show it as such — unverified is information, not an error.
    """

    label: str = ""                 # short feature name, e.g. "Founded" (may be empty for plain claims)
    text: str                       # the claim itself, atomic and self-contained
    verdict: Verdict = "unverified"
    supporting_span: Optional[str] = None   # verbatim quote; required when verified
    source_url: Optional[str] = None        # which source the span came from
    method: Optional[VerifyMethod] = None   # how it was grounded
    note: str = ""                  # e.g. "source unreachable" — why unverified, when known


class Confidence(BaseModel):
    """Confidence band plus a plain-English explanation of why.

    The ``basis`` string is the audit trail a reader sees on hover, e.g.
    "existence verified; 3 of 4 key claims grounded; found by 2 independent
    search indexes".
    """

    level: ConfidenceLevel = "low"
    basis: str = ""


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

class EntityCard(BaseModel):
    """Everything the report knows about one entity in the landscape.

    Deep-profiled entities carry features, claims, and confidence. Long-tail
    entities (found but not deep-profiled, to keep cost down) carry just the
    name, a one-liner, and their discovery sources — ``deep_profiled`` is
    False and the UI renders them as a compact list.
    """

    name: str
    form: EntityForm = "other"
    segment: str = ""               # which bucket of the landscape carve this sits in
    one_liner: str = ""             # at-a-glance description (model-written, marked as such)
    features: list[Claim] = Field(default_factory=list)   # labelled key features with verdicts
    confidence: Confidence = Field(default_factory=Confidence)
    quarantined: bool = False       # True = existence could not be verified; shown flagged, never hidden
    cross_index_agreement: bool = False  # found by >= 2 independent search indexes
    deep_profiled: bool = True      # False = long-tail entry (name + sources only)
    sources: list[SourceRef] = Field(default_factory=list)
    limitations: str = ""           # honest per-entity caveats, e.g. "outcome data not found"


# ---------------------------------------------------------------------------
# Run configuration
# ---------------------------------------------------------------------------

class RunConfig(BaseModel):
    """Everything the user chooses for one scan, with safe defaults.

    The user typically sets only ``question`` (and optionally geography and
    the budget); the rest are tuned defaults that keep a run inside the
    cost target.
    """

    question: str                   # e.g. "entrepreneurship programmes, current and past, in Kenya"
    geography: list[str] = Field(default_factory=list)
    budget_usd: float = 2.0         # hard, resumable stop — never exceeded
    model_profile: Literal["economy", "quality"] = "economy"

    # Funnel shape (the cost levers, with comments on their effect)
    shortlist_size: int = 25        # how many entities get deep profiling
    scrape_pages_per_entity: int = 3  # sources scraped per shortlisted entity
    max_page_chars: int = 15000     # truncate page text before extraction (input-token cap)
    max_search_queries: int = 30    # breadth of discovery

    demo_mode: bool = False         # True = run entirely on canned fixtures, clearly bannered


# ---------------------------------------------------------------------------
# Report-level bookkeeping
# ---------------------------------------------------------------------------

class ProviderManifest(BaseModel):
    """Stamped into every report: exactly what produced this result.

    This exists because v1 could silently substitute synthetic data when a
    key was missing. Here, the manifest records each provider as live or
    absent, and ``demo_mode`` is an explicit, unmissable flag.
    """

    search_providers: dict[str, bool] = Field(default_factory=dict)  # name -> was live
    models_by_task: dict[str, str] = Field(default_factory=dict)     # task -> model id used
    demo_mode: bool = False
    notes: list[str] = Field(default_factory=list)  # degradations, outages, budget caps hit


class CostSummary(BaseModel):
    """Real, measured spend for the run (from API usage fields, not guesses)."""

    total_usd: float = 0.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0       # should be ZERO; non-zero triggers a manifest note
    by_task: dict[str, int] = Field(default_factory=dict)  # task -> call count


class SegmentSummary(BaseModel):
    """One bucket of the landscape carve, for the executive overview."""

    label: str
    description: str = ""
    entity_names: list[str] = Field(default_factory=list)


class Overview(BaseModel):
    """The executive summary a reader absorbs in under a minute.

    Assembled in the final read step. The synthesis sentences are
    model-written but may only reference verified material; gaps are
    surfaced, not smoothed over.
    """

    headline: str = ""              # one-sentence summary of the landscape
    segments: list[SegmentSummary] = Field(default_factory=list)
    key_players: list[str] = Field(default_factory=list)
    crowded_areas: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    confidence_counts: dict[str, int] = Field(default_factory=dict)  # level -> count


class Report(BaseModel):
    """The final deliverable for one scan — everything the UI and the
    shareable HTML render from."""

    run_config: RunConfig
    created_at: str = Field(default_factory=utc_now_iso)
    overview: Overview = Field(default_factory=Overview)
    entities: list[EntityCard] = Field(default_factory=list)      # deep-profiled, card view
    long_tail: list[EntityCard] = Field(default_factory=list)     # found-but-not-profiled, list view
    manifest: ProviderManifest = Field(default_factory=ProviderManifest)
    cost: CostSummary = Field(default_factory=CostSummary)
    coverage_notes: list[str] = Field(default_factory=list)       # honest limits of this run

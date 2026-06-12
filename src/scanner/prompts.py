"""Prompts and structured-output schemas for every pipeline task.

Each task has a SYSTEM prompt (constant per task — cacheable) and a
function that builds the USER prompt from run data. The paired Pydantic
schema is what the router validates the model's JSON against.

The anchor-constraint rule appears wherever the model could be tempted to
write from its own knowledge: extraction tasks are told, repeatedly, that
they may only use text present in the material given to them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .models import EntityForm

# ---------------------------------------------------------------------------
# frame — turn the question into search queries and a proposed carve
# ---------------------------------------------------------------------------

FRAME_SYSTEM = (
    "You are a desk-research planner. Given a landscape question, you produce "
    "diverse web search queries and a proposed segmentation of the landscape. "
    "You answer in strict JSON only."
)


class ProposedSegment(BaseModel):
    """One bucket of the proposed landscape carve."""

    label: str
    description: str = ""


class FrameOutput(BaseModel):
    """The run plan the frame task returns."""

    search_queries: list[str] = Field(min_length=5)
    segments: list[ProposedSegment] = Field(min_length=2)
    notes: str = ""  # anything ambiguous about the question worth surfacing


def frame_prompt(question: str, geography: list[str], max_queries: int) -> str:
    """Build the frame task's user prompt."""
    geo = ", ".join(geography) if geography else "not specified"
    return (
        f"Landscape question: {question}\n"
        f"Geography focus: {geo}\n\n"
        f"Produce up to {max_queries} diverse web search queries that together "
        f"would surface the entities in this landscape — vary phrasing, include "
        f"synonyms, sector jargon, list-style queries ('list of...', 'top...'), "
        f"and queries for past/discontinued efforts, not just current ones. "
        f"Also propose 3-6 segments that would usefully carve this landscape "
        f"for a reader (by focus area, delivery model, or stage — choose what "
        f"fits the question)."
    )


# ---------------------------------------------------------------------------
# extract_entities — pull entity names from a batch of search results
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM = (
    "You extract named entities (programmes, organisations, initiatives, "
    "products, schemes) from web search results. You only report names that "
    "appear in the result text given to you — never from your own knowledge. "
    "You answer in strict JSON only."
)


class ExtractedEntity(BaseModel):
    """One candidate entity found in search results."""

    name: str
    source_urls: list[str] = Field(default_factory=list)  # which results mentioned it


class ExtractOutput(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)


def extract_prompt(question: str, hits_block: str) -> str:
    """hits_block: numbered list of 'title — snippet — url' lines."""
    return (
        f"Research question: {question}\n\n"
        f"Search results:\n{hits_block}\n\n"
        f"List every distinct entity relevant to the question that is NAMED in "
        f"these results. Use the exact name as written. For each, list the "
        f"result URLs that mention it. Do not add entities you know of that "
        f"are not named in these results."
    )


# ---------------------------------------------------------------------------
# triage — rank candidates from snippets (the funnel gate)
# ---------------------------------------------------------------------------

TRIAGE_SYSTEM = (
    "You rank candidate entities by relevance to a research question, using "
    "only the snippet evidence provided. You answer in strict JSON only."
)


class TriageScore(BaseModel):
    """Relevance of one candidate, 0 (irrelevant) to 10 (core example)."""

    name: str
    relevance: int = Field(ge=0, le=10)


class TriageOutput(BaseModel):
    scores: list[TriageScore] = Field(default_factory=list)


def triage_prompt(question: str, candidates_block: str) -> str:
    """candidates_block: one line per candidate: 'name — snippets...'."""
    return (
        f"Research question: {question}\n\n"
        f"Candidates with their search-snippet evidence:\n{candidates_block}\n\n"
        f"Score every candidate 0-10 for how clearly it is an entity IN this "
        f"landscape (a programme/initiative/intervention the question asks "
        f"about), not merely a related topic, place, or generic term. "
        f"Score conservatively when snippets are ambiguous."
    )


# ---------------------------------------------------------------------------
# span_discovery — extract the value-spans present in one scraped page
# ---------------------------------------------------------------------------

SPAN_SYSTEM = (
    "You are an extraction engine. You copy short verbatim spans out of a "
    "document — exact substrings, never paraphrases, never knowledge of your "
    "own. You answer in strict JSON only."
)


class Span(BaseModel):
    """One verbatim span found in a page."""

    text: str                     # EXACT substring of the page
    kind: str = "fact"            # date | number | name | location | fact


class SpanOutput(BaseModel):
    spans: list[Span] = Field(default_factory=list)


def span_prompt(entity_name: str, page_text: str) -> str:
    return (
        f"Entity of interest: {entity_name}\n\n"
        f"Document:\n---\n{page_text}\n---\n\n"
        f"Copy out every short span (a phrase or sentence fragment) in this "
        f"document that states a fact about the entity: founding dates, "
        f"locations, scale numbers, funders, focus areas, status (active/"
        f"closed), outcomes. Each span must be an EXACT substring of the "
        f"document. Skip facts about other entities."
    )


# ---------------------------------------------------------------------------
# attribute_population — build the profile by SELECTING from spans
# ---------------------------------------------------------------------------

POPULATE_SYSTEM = (
    "You assemble entity profiles strictly from the verbatim spans given to "
    "you. Every claim you write must be directly supported by one of the "
    "provided spans — you never add facts from your own knowledge. You answer "
    "in strict JSON only."
)


class PopulatedFeature(BaseModel):
    """One key feature, assembled from the span inventory."""

    label: str                    # e.g. "Founded", "Scale", "Funder", "Status"
    text: str                     # atomic claim, phrased close to the span


class PopulateOutput(BaseModel):
    one_liner: str                # at-a-glance description (marked model-written)
    form: EntityForm = "other"
    segment: str = ""             # one of the run's segment labels
    features: list[PopulatedFeature] = Field(default_factory=list)


def populate_prompt(entity_name: str, question: str, segments: list[str], spans_block: str) -> str:
    return (
        f"Entity: {entity_name}\n"
        f"Research question: {question}\n"
        f"Available segments: {', '.join(segments)}\n\n"
        f"Verbatim spans found in this entity's sources:\n{spans_block}\n\n"
        f"Build the entity's profile: a one-line description, its form, the "
        f"segment it belongs to, and 3-8 key features (founded, status, "
        f"scale, focus, funders, geography, outcomes — whatever the spans "
        f"actually support). Each feature's text must be one atomic claim "
        f"directly supported by a span above. If the spans don't cover an "
        f"aspect, OMIT it — do not fill gaps from your own knowledge."
    )


# ---------------------------------------------------------------------------
# relation_check — the verification verdict (trust-critical, always Claude)
# ---------------------------------------------------------------------------

RELATION_SYSTEM = (
    "You are a strict verification engine. Given a claim and passages from a "
    "source, you decide whether any passage states the claim — the WHOLE "
    "assertion, not just a value it contains. If yes, you return the exact "
    "verbatim passage text. You never paraphrase, infer, or combine passages. "
    "When in doubt, the claim is unsupported. You answer in strict JSON only."
)


class RelationOutput(BaseModel):
    supported: bool = False
    span: str = ""                # verbatim passage covering the assertion, when supported


def relation_prompt(claim_text: str, passages: list[str]) -> str:
    numbered = "\n".join(f"[{i + 1}] {p}" for i, p in enumerate(passages))
    return (
        f"Claim: {claim_text}\n\n"
        f"Passages from the source:\n{numbered}\n\n"
        f"Does any single passage state this claim — including the connection "
        f"it asserts, not merely a number or name it mentions? If yes, return "
        f"supported=true and copy that passage EXACTLY as written above. "
        f"If no single passage states it, return supported=false."
    )


# ---------------------------------------------------------------------------
# read — the executive overview (assembled from verified material)
# ---------------------------------------------------------------------------

READ_SYSTEM = (
    "You write executive summaries of landscape research. You may only use "
    "the verified entity data given to you; you surface gaps honestly rather "
    "than smoothing them over. You answer in strict JSON only."
)


class ReadOutput(BaseModel):
    headline: str
    key_players: list[str] = Field(default_factory=list)
    crowded_areas: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    segment_descriptions: dict[str, str] = Field(default_factory=dict)


def read_prompt(question: str, entities_block: str) -> str:
    return (
        f"Research question: {question}\n\n"
        f"Verified landscape data (entity, segment, confidence, key verified "
        f"features):\n{entities_block}\n\n"
        f"Write the executive read: a one-sentence headline; the key players "
        f"(highest-confidence, most substantial entities); areas where many "
        f"entities crowd; and the gaps — segments or needs with little or "
        f"nothing in them, INCLUDING systematic absences (e.g. outcome data "
        f"rarely published). Name only entities from the data above."
    )

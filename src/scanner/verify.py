"""Claim verification: restoration first (free), LLM relation-check last.

This module is the accuracy heart of the scanner, carried over from the
v1 engine's load-bearing design:

1. **Restoration (no LLM, costs nothing).** Try to find the claim verbatim
   in a scraped source — exact sentence match first, then fuzzy matching.
   Because claims are assembled FROM spans found in the sources (anchor-
   constrained extraction), most of them restore here and never touch a
   model.

2. **Value-only matches are NOT verification.** If only the claim's
   numbers/values appear in a source (but not the assertion connecting
   them to the entity), that is exactly the dominant real-world failure
   mode — "the value is in the source but the source doesn't attribute it
   to this entity". Those claims go to the LLM relation-check.

3. **Relation check (LLM, narrowed).** The most relevant passages are
   selected by cheap lexical similarity, and the model is asked to return
   a span COVERING THE ASSERTION — which is then validated to be verbatim
   in the source text. A span the model invented fails the verbatim check
   and the claim stays unverified. The model can therefore never launder a
   fabrication into a verified claim.

An unverified verdict is terminal and honest: it means "no source we read
states this", which the report must show rather than hide.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from rapidfuzz import fuzz

from .models import Claim, SourceRef

# Fuzzy-match score (0-100) at or above which a source sentence counts as
# a restoration of the claim. Set high: a loose paraphrase is the LLM's
# job to judge, not fuzzy matching's.
FUZZY_RESTORE_THRESHOLD = 92.0

# How many candidate passages the relation-check sees (cost control on the
# only LLM rung).
RELATION_CHECK_TOP_K = 6


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def split_sentences(text: str) -> list[str]:
    """Split source text into sentence-ish spans.

    Splits on sentence punctuation and newlines, keeps spans with enough
    substance to ground anything (>= 20 characters).
    """
    raw_spans = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [span.strip() for span in raw_spans if len(span.strip()) >= 20]


def normalise(text: str) -> str:
    """Normalise text for comparison: lowercase, collapse whitespace,
    standardise quotes/dashes, drop thousands separators in numbers."""
    text = text.lower()
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    # "1,000" -> "1000" so separator style never blocks a match
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_numbers(text: str) -> set[str]:
    """Pull the numeric values out of a string in canonical form.

    "$5", "5%", "1,000" and "1000" all yield comparable tokens. Used to
    detect value-only matches (which must NOT count as verification).
    """
    normalised = normalise(text)
    return set(re.findall(r"\d+(?:\.\d+)?", normalised))


# ---------------------------------------------------------------------------
# Path 1: restoration
# ---------------------------------------------------------------------------

def restore(claim_text: str, source_text: str) -> Optional[tuple[str, str]]:
    """Try to restore a claim to a verbatim span of one source.

    Returns (method, span) on success — where ``span`` is the ORIGINAL
    sentence from the source (verbatim, quotable) — or None when the claim
    does not restore. A value-only match deliberately returns None.
    """
    claim_normalised = normalise(claim_text)
    if not claim_normalised:
        return None

    best_fuzzy: tuple[float, str] | None = None
    for sentence in split_sentences(source_text):
        sentence_normalised = normalise(sentence)

        # Rung 1: the claim appears verbatim inside this sentence.
        if claim_normalised in sentence_normalised:
            return "restore_exact", sentence

        # Rung 2: near-verbatim (punctuation/word-order wobble). We track
        # the best score and accept it only above a high threshold.
        score = fuzz.token_sort_ratio(claim_normalised, sentence_normalised)
        if score >= FUZZY_RESTORE_THRESHOLD and (best_fuzzy is None or score > best_fuzzy[0]):
            best_fuzzy = (score, sentence)

    if best_fuzzy is not None:
        return "restore_fuzzy", best_fuzzy[1]
    return None


def select_passages(claim_text: str, source_text: str, top_k: int = RELATION_CHECK_TOP_K) -> list[str]:
    """Pick the source sentences most likely to bear on a claim.

    Cheap lexical similarity (no embeddings dependency): scores every
    sentence against the claim and returns the top K. This narrows what
    the relation-check model reads, which is the main cost lever on the
    only LLM rung.
    """
    sentences = split_sentences(source_text)
    if not sentences:
        return []
    claim_normalised = normalise(claim_text)
    scored = [
        (fuzz.token_set_ratio(claim_normalised, normalise(sentence)), sentence)
        for sentence in sentences
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [sentence for _, sentence in scored[:top_k]]


# ---------------------------------------------------------------------------
# Path 2: the LLM relation-check (injected, so this module stays testable
# offline and free of any provider knowledge)
# ---------------------------------------------------------------------------

# A relation checker takes (claim_text, passages) and returns the span the
# model says covers the assertion, or None. The CALLER builds this from the
# ModelRouter; tests inject a stub.
RelationChecker = Callable[[str, list[str]], Optional[str]]


def verify_claim(
    claim: Claim,
    sources: list[SourceRef],
    relation_checker: RelationChecker | None = None,
) -> Claim:
    """Verify one claim against scraped sources. Returns the updated claim.

    Order of attack:
      1. Restoration against every scraped source (free). A full-claim
         match covers the assertion (the claim itself was found), so it
         verifies immediately.
      2. If the claim's values appear somewhere but the assertion doesn't,
         or nothing restores, run the relation-check (when available).
         The returned span must be VERBATIM in the source or the claim
         stays unverified.
      3. If all candidate sources were unreachable, say so — that is a
         fetch failure, not evidence against the claim.
    """
    scraped = [s for s in sources if s.scrape_status == "scraped" and s.full_text]

    if not scraped:
        attempted = [s for s in sources if s.scrape_status == "unreachable"]
        note = "source unreachable" if attempted else "no scraped sources"
        return claim.model_copy(update={"verdict": "unverified", "note": note})

    # --- Path 1: restoration ---------------------------------------------
    for source in scraped:
        restored = restore(claim.text, source.full_text)
        if restored is not None:
            method, span = restored
            return claim.model_copy(
                update={
                    "verdict": "verified",
                    "supporting_span": span,
                    "source_url": source.url,
                    "method": method,
                    "note": "",
                }
            )

    # --- Path 2: relation-check ------------------------------------------
    if relation_checker is not None:
        claim_numbers = extract_numbers(claim.text)
        for source in scraped:
            passages = select_passages(claim.text, source.full_text)
            if not passages:
                continue
            span = relation_checker(claim.text, passages)
            if span and span.strip() and span.strip() in source.full_text:
                # The model's span is verbatim in this source — accept.
                return claim.model_copy(
                    update={
                        "verdict": "verified",
                        "supporting_span": span.strip(),
                        "source_url": source.url,
                        "method": "llm_extract",
                        "note": "",
                    }
                )
            # A span that is NOT verbatim is treated as no support at all.

        # Explain the near-miss when we can see one: values present but
        # assertion unsupported is the classic unsupported-relation case.
        if claim_numbers and any(
            claim_numbers & extract_numbers(source.full_text) for source in scraped
        ):
            return claim.model_copy(
                update={"verdict": "unverified", "note": "value found but assertion not supported"}
            )

    return claim.model_copy(update={"verdict": "unverified", "note": claim.note or "no supporting span found"})

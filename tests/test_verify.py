"""Tests for the verification core — the accuracy guarantees.

All offline and deterministic. The LLM relation-check is a stub, which is
the point: these tests prove the SHAPE of the guarantee (verbatim spans or
nothing), independent of any model.
"""

from scanner.models import Claim, SourceRef
from scanner.verify import (
    extract_numbers,
    normalise,
    restore,
    select_passages,
    split_sentences,
    verify_claim,
)

PAGE = (
    "The Acme Youth Enterprise Fund was founded in 2015 by a coalition of banks. "
    "It operates in 14 counties across Kenya, focusing on rural entrepreneurs. "
    "A separate organisation, the Beta Trust, reported revenues of $1,000,000 in 2020. "
    "Annual reports are published every March."
)


def scraped_source(text: str = PAGE, url: str = "https://example.org/about") -> SourceRef:
    return SourceRef(url=url, scrape_status="scraped", full_text=text)


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def test_normalise_handles_numbers_quotes_whitespace():
    assert normalise("Revenue of  $1,000,000") == "revenue of $1000000"
    assert normalise("It’s “quoted”") == "it's \"quoted\""


def test_extract_numbers_canonicalises():
    assert extract_numbers("$1,000,000 in 2020") == {"1000000", "2020"}


def test_split_sentences_drops_fragments():
    spans = split_sentences(PAGE)
    assert any("founded in 2015" in s for s in spans)
    assert all(len(s) >= 20 for s in spans)


# ---------------------------------------------------------------------------
# Restoration (path 1)
# ---------------------------------------------------------------------------

def test_exact_restoration_returns_verbatim_sentence():
    result = restore("founded in 2015 by a coalition of banks", PAGE)
    assert result is not None
    method, span = result
    assert method == "restore_exact"
    assert span in PAGE  # the span is quotable, verbatim source text


def test_fuzzy_restoration_tolerates_small_wobble():
    result = restore("It operates in 14 counties across Kenya focusing on rural entrepreneurs", PAGE)
    assert result is not None
    method, span = result
    assert method in ("restore_exact", "restore_fuzzy")
    assert span in PAGE


def test_unrelated_claim_does_not_restore():
    assert restore("The fund won a Nobel prize", PAGE) is None


def test_value_only_presence_does_not_restore():
    """The classic failure mode: $1,000,000 belongs to the Beta Trust, not
    Acme. A claim attributing it to Acme must NOT restore."""
    assert restore("Acme reported revenues of $1,000,000", PAGE) is None


# ---------------------------------------------------------------------------
# verify_claim end-to-end (with stubbed relation checker)
# ---------------------------------------------------------------------------

def test_verify_claim_grounds_by_restoration_without_llm():
    claim = Claim(label="Founded", text="founded in 2015 by a coalition of banks")
    verified = verify_claim(claim, [scraped_source()], relation_checker=None)
    assert verified.verdict == "verified"
    assert verified.method == "restore_exact"
    assert verified.supporting_span in PAGE
    assert verified.source_url == "https://example.org/about"


def test_unreachable_sources_noted_not_blamed():
    """All sources unreachable -> unverified with the honest note, distinct
    from 'no source supports this'."""
    claim = Claim(text="founded in 2015")
    unreachable = SourceRef(url="https://dead.example.org", scrape_status="unreachable")
    result = verify_claim(claim, [unreachable])
    assert result.verdict == "unverified"
    assert result.note == "source unreachable"


def test_llm_span_accepted_only_if_verbatim():
    """A relation-check span that is NOT verbatim in the source must be
    rejected — this is the rule that stops model fabrication."""
    claim = Claim(text="Acme focuses on supporting rural business owners")

    def lying_checker(claim_text, passages):
        return "Acme is the leading rural finance provider"  # not in the page

    result = verify_claim(claim, [scraped_source()], relation_checker=lying_checker)
    assert result.verdict == "unverified"


def test_llm_span_accepted_when_verbatim():
    claim = Claim(text="Acme focuses on supporting rural business owners")
    real_span = "It operates in 14 counties across Kenya, focusing on rural entrepreneurs."

    def honest_checker(claim_text, passages):
        return real_span

    result = verify_claim(claim, [scraped_source()], relation_checker=honest_checker)
    assert result.verdict == "verified"
    assert result.method == "llm_extract"
    assert result.supporting_span == real_span


def test_value_found_but_assertion_unsupported_is_explained():
    """Value-only match + relation-check finding nothing -> the note names
    the failure mode so the report can show it."""
    claim = Claim(text="Acme reported revenues of $1,000,000")

    def finds_nothing(claim_text, passages):
        return None

    result = verify_claim(claim, [scraped_source()], relation_checker=finds_nothing)
    assert result.verdict == "unverified"
    assert result.note == "value found but assertion not supported"


def test_select_passages_ranks_relevant_sentences_first():
    passages = select_passages("founded in 2015", PAGE, top_k=2)
    assert len(passages) == 2
    assert any("founded in 2015" in p for p in passages)

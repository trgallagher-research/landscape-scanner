"""Tests for the core data models: defaults, serialisation, and the
verification contract encoded in the schema."""

from scanner.models import (
    Claim,
    Confidence,
    EntityCard,
    Report,
    RunConfig,
    SourceRef,
)


def test_claim_defaults_to_unverified():
    """A freshly created claim must start unverified — verification is
    something that happens TO a claim, never a default."""
    claim = Claim(text="Founded in 2015")
    assert claim.verdict == "unverified"
    assert claim.supporting_span is None


def test_verified_claim_carries_span_and_source():
    """The shape of a properly verified claim."""
    claim = Claim(
        label="Founded",
        text="Founded in 2015",
        verdict="verified",
        supporting_span="was founded in 2015 by",
        source_url="https://example.org/about",
        method="restore_exact",
    )
    assert claim.verdict == "verified"
    assert claim.supporting_span


def test_source_without_full_text_strips_page_text_only():
    """Reports keep the source URL/title but drop megabytes of page text."""
    source = SourceRef(url="https://example.org", title="Example", full_text="x" * 10000)
    compact = source.without_full_text()
    assert compact.full_text is None
    assert compact.url == source.url
    assert source.full_text is not None  # original untouched


def test_entity_card_defaults_are_honest():
    """New entities start at low confidence and are not quarantined."""
    card = EntityCard(name="Some Programme")
    assert card.confidence.level == "low"
    assert card.quarantined is False
    assert card.deep_profiled is True


def test_run_config_defaults_match_cost_targets():
    """The default funnel shape should reflect the agreed cost design."""
    config = RunConfig(question="entrepreneurship programmes in Kenya")
    assert config.budget_usd == 2.0
    assert config.shortlist_size == 25
    assert config.scrape_pages_per_entity == 3
    assert config.model_profile == "economy"
    assert config.demo_mode is False


def test_report_round_trips_through_json():
    """The full report must serialise and reload losslessly (resume + export)."""
    report = Report(
        run_config=RunConfig(question="test"),
        entities=[
            EntityCard(
                name="Entity A",
                features=[Claim(label="Founded", text="Founded in 2015")],
                confidence=Confidence(level="medium", basis="single source"),
            )
        ],
    )
    reloaded = Report.model_validate_json(report.model_dump_json())
    assert reloaded.entities[0].name == "Entity A"
    assert reloaded.entities[0].features[0].verdict == "unverified"

"""Tests for the HTML export: self-containment, honesty markers, and
escaping of source-derived text."""

from scanner.models import (
    Claim,
    Confidence,
    CostSummary,
    EntityCard,
    Overview,
    ProviderManifest,
    Report,
    RunConfig,
    SourceRef,
)
from scanner.report_html import render_html


def sample_report(**overrides) -> Report:
    verified = Claim(
        label="Founded",
        text="founded in 2015",
        verdict="verified",
        supporting_span="was founded in 2015 by a coalition",
        source_url="https://example.org/about",
        method="restore_exact",
    )
    unverified = Claim(label="Award", text="won a 2019 award", note="no supporting span found")
    base = dict(
        run_config=RunConfig(question="entrepreneurship programmes in Kenya"),
        overview=Overview(headline="A finance-heavy landscape.", confidence_counts={"high": 1}),
        entities=[
            EntityCard(
                name="Acme Fund",
                form="programme",
                segment="Finance",
                one_liner="Bank-backed fund.",
                features=[verified, unverified],
                confidence=Confidence(level="high", basis="2 sources"),
                sources=[SourceRef(url="https://example.org/about", title="About", scrape_status="scraped")],
            ),
            EntityCard(
                name="Ghost Initiative",
                quarantined=True,
                confidence=Confidence(level="low", basis="no source could be fetched"),
            ),
        ],
        manifest=ProviderManifest(search_providers={"serper": True, "brave": True}),
        cost=CostSummary(total_usd=0.42, llm_calls=37),
    )
    base.update(overrides)
    return Report(**base)


def test_html_is_self_contained():
    """No external scripts/stylesheets — the file must work offline."""
    html_text = render_html(sample_report())
    assert "<script src=" not in html_text
    assert "<link " not in html_text
    assert "<style>" in html_text


def test_verified_claim_shows_quote_and_unverified_is_marked():
    html_text = render_html(sample_report())
    assert "was founded in 2015 by a coalition" in html_text  # the verbatim quote
    assert "unverified" in html_text                          # the honest marker


def test_quarantined_entity_is_flagged():
    html_text = render_html(sample_report())
    assert "QUARANTINED" in html_text


def test_demo_mode_banner_is_unmissable():
    report = sample_report(manifest=ProviderManifest(demo_mode=True))
    assert "DEMO MODE" in render_html(report)
    # And absent on a live report.
    assert "DEMO MODE" not in render_html(sample_report())


def test_cost_and_manifest_in_footer():
    html_text = render_html(sample_report())
    assert "$0.42" in html_text
    assert "serper (live)" in html_text


def test_source_text_is_escaped():
    """Hostile text from a scraped page must never become live HTML."""
    nasty = Claim(
        label="XSS",
        text='<script>alert("x")</script>',
        verdict="verified",
        supporting_span='<img src=x onerror=alert(1)>',
        source_url="https://example.org",
        method="llm_extract",
    )
    report = sample_report()
    report.entities[0].features.append(nasty)
    html_text = render_html(report)
    assert "<script>alert" not in html_text
    assert "<img src=x" not in html_text

"""Self-contained HTML report — the shareable artifact.

One file, no external assets, no JavaScript dependencies: drill-down uses
native <details>/<summary> elements, styling is inline CSS. The file opens
in any browser, attaches to any email, and can be committed to a GitHub
Pages repo when the content is fine being public.

Honesty requirements carried into the rendering:
* Verified claims show their verbatim source quote and a link; unverified
  claims are visibly marked, never dressed as facts.
* Quarantined entities sit in their own clearly-labelled section.
* The provider manifest and measured cost are printed in the footer; a
  demo-mode run carries an unmissable banner.
"""

from __future__ import annotations

import html

from .models import Claim, EntityCard, Report

# Colour per confidence band (used for the card badges).
_BADGE_COLOURS = {"high": "#1a7f37", "medium": "#9a6700", "low": "#6e7781"}

_CSS = """
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
       background: #f6f8fa; color: #1f2328; line-height: 1.5; }
.wrap { max-width: 980px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 1.5rem; margin-bottom: 4px; }
h2 { font-size: 1.15rem; margin-top: 32px; border-bottom: 1px solid #d1d9e0;
     padding-bottom: 4px; }
.meta { color: #59636e; font-size: 0.85rem; }
.banner-demo { background: #cf222e; color: #fff; padding: 10px 16px;
               font-weight: 700; text-align: center; }
.overview { background: #fff; border: 1px solid #d1d9e0; border-radius: 8px;
            padding: 16px 20px; margin-top: 16px; }
.headline { font-size: 1.1rem; font-weight: 600; }
.chips span { display: inline-block; background: #eaeef2; border-radius: 12px;
              padding: 2px 10px; margin: 2px 4px 2px 0; font-size: 0.8rem; }
.card { background: #fff; border: 1px solid #d1d9e0; border-radius: 8px;
        padding: 14px 18px; margin: 10px 0; }
.card.quarantined { border-left: 4px solid #cf222e; }
.badge { color: #fff; border-radius: 10px; padding: 1px 9px; font-size: 0.75rem;
         font-weight: 600; vertical-align: middle; }
.entity-name { font-weight: 700; font-size: 1.02rem; }
.tag { color: #59636e; font-size: 0.8rem; margin-left: 6px; }
.oneliner { margin: 4px 0 8px; }
ul.features { margin: 4px 0; padding-left: 18px; }
li.feature { margin: 3px 0; }
.verdict-ok { color: #1a7f37; font-weight: 700; }
.verdict-no { color: #9a6700; font-weight: 700; }
details { margin: 2px 0 2px 8px; }
summary { cursor: pointer; color: #0969da; font-size: 0.85rem; }
blockquote { border-left: 3px solid #d1d9e0; margin: 6px 0 6px 4px;
             padding: 4px 10px; color: #424a53; font-size: 0.9rem;
             background: #f6f8fa; }
.limitations { color: #9a6700; font-size: 0.85rem; margin-top: 6px; }
.longtail li { margin: 2px 0; font-size: 0.92rem; }
.footer { margin-top: 40px; padding-top: 12px; border-top: 1px solid #d1d9e0;
          font-size: 0.82rem; color: #59636e; }
.alert { color: #cf222e; font-weight: 600; }
a { color: #0969da; text-decoration: none; }
"""


def _esc(text: str) -> str:
    """HTML-escape user/model/source text before it enters the page."""
    return html.escape(text or "", quote=True)


def _render_feature(feature: Claim) -> str:
    """One feature line with its verdict and (when verified) the quote."""
    if feature.verdict == "verified":
        mark = '<span class="verdict-ok">&#10003;</span>'
        source_link = (
            f'<a href="{_esc(feature.source_url)}" target="_blank" rel="noopener">source</a>'
            if feature.source_url else "source unknown"
        )
        detail = (
            f"<details><summary>verbatim quote &amp; source</summary>"
            f"<blockquote>&ldquo;{_esc(feature.supporting_span or '')}&rdquo;</blockquote>"
            f"<div>{source_link} &middot; method: {_esc(feature.method or '')}</div></details>"
        )
    else:
        mark = '<span class="verdict-no">&#9675;</span>'
        note = f" <em>({_esc(feature.note)})</em>" if feature.note else ""
        detail = f'<span class="tag">unverified{note}</span>'
    label = f"<strong>{_esc(feature.label)}:</strong> " if feature.label else ""
    return f'<li class="feature">{mark} {label}{_esc(feature.text)} {detail}</li>'


def _render_card(card: EntityCard) -> str:
    """One deep-profiled entity card with full drill-down."""
    badge_colour = _BADGE_COLOURS.get(card.confidence.level, "#6e7781")
    quarantine_class = " quarantined" if card.quarantined else ""
    quarantine_note = (
        '<div class="alert">&#9888; QUARANTINED &mdash; existence could not be verified '
        "in any readable source. Treat with caution.</div>"
        if card.quarantined else ""
    )
    features_html = "".join(_render_feature(f) for f in card.features)
    sources_html = " &middot; ".join(
        f'<a href="{_esc(s.url)}" target="_blank" rel="noopener">{_esc(s.title or s.url)}</a>'
        f" <span class='tag'>[{_esc(s.scrape_status)}]</span>"
        for s in card.sources
    )
    limitations = (
        f'<div class="limitations">{_esc(card.limitations)}</div>' if card.limitations else ""
    )
    agreement = (
        ' &middot; <span title="found by 2+ independent search indexes">2+ indexes</span>'
        if card.cross_index_agreement else ""
    )
    return f"""
<div class="card{quarantine_class}">
  <div>
    <span class="entity-name">{_esc(card.name)}</span>
    <span class="badge" style="background:{badge_colour}"
          title="{_esc(card.confidence.basis)}">{card.confidence.level}</span>
    <span class="tag">{_esc(card.form)}{' &middot; ' + _esc(card.segment) if card.segment else ''}{agreement}</span>
  </div>
  {quarantine_note}
  <div class="oneliner">{_esc(card.one_liner)}</div>
  <ul class="features">{features_html}</ul>
  <details><summary>confidence basis &amp; sources</summary>
    <div class="tag">Basis: {_esc(card.confidence.basis)}</div>
    <div class="tag">Sources: {sources_html}</div>
  </details>
  {limitations}
</div>"""


def render_html(report: Report) -> str:
    """Render the full report to one self-contained HTML document."""
    overview = report.overview
    config = report.run_config

    demo_banner = (
        '<div class="banner-demo">DEMO MODE &mdash; this report was generated from '
        "canned fixture data, not live search. Do not use its content.</div>"
        if report.manifest.demo_mode else ""
    )

    chips = "".join(
        f"<span>{_esc(level)}: {count}</span>"
        for level, count in sorted(overview.confidence_counts.items())
    )
    segments_html = "".join(
        f"<li><strong>{_esc(s.label)}</strong> ({len(s.entity_names)}): "
        f"{_esc(', '.join(s.entity_names) or '— empty —')}"
        f"<br><span class='tag'>{_esc(s.description)}</span></li>"
        for s in overview.segments
    )
    gaps_html = "".join(f"<li>{_esc(g)}</li>" for g in overview.gaps)
    crowded_html = "".join(f"<li>{_esc(c)}</li>" for c in overview.crowded_areas)

    verified_cards = [c for c in report.entities if not c.quarantined]
    quarantined_cards = [c for c in report.entities if c.quarantined]
    cards_html = "".join(_render_card(c) for c in verified_cards)
    quarantined_html = (
        "<h2>Quarantined (existence unverified)</h2>"
        + "".join(_render_card(c) for c in quarantined_cards)
        if quarantined_cards else ""
    )

    long_tail_html = ""
    if report.long_tail:
        items = "".join(
            f"<li><strong>{_esc(c.name)}</strong> &mdash; {_esc(c.one_liner)} "
            + " ".join(
                f'<a href="{_esc(s.url)}" target="_blank" rel="noopener">[source]</a>'
                for s in c.sources
            )
            + "</li>"
            for c in report.long_tail
        )
        long_tail_html = (
            f"<h2>Also found ({len(report.long_tail)} more, not deep-profiled)</h2>"
            f'<ul class="longtail">{items}</ul>'
        )

    providers = ", ".join(
        f"{name} ({'live' if ok else 'FAILED'})"
        for name, ok in report.manifest.search_providers.items()
    )
    models = "; ".join(
        f"{task}: {model}" for task, model in sorted(report.manifest.models_by_task.items())
    )
    manifest_notes = "".join(
        f'<div class="alert">{_esc(n)}</div>' for n in report.manifest.notes
    )
    coverage = "".join(f"<li>{_esc(n)}</li>" for n in report.coverage_notes)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Landscape: {_esc(config.question)}</title>
<style>{_CSS}</style>
</head>
<body>
{demo_banner}
<div class="wrap">
  <h1>Landscape scan</h1>
  <div class="meta">{_esc(config.question)}
    {(' &middot; ' + _esc(', '.join(config.geography))) if config.geography else ''}
    &middot; generated {_esc(report.created_at)}</div>

  <div class="overview">
    <div class="headline">{_esc(overview.headline)}</div>
    <div class="chips">{chips}</div>
    <h2>Segments</h2><ul>{segments_html}</ul>
    {f'<h2>Key players</h2><p>{_esc(", ".join(overview.key_players))}</p>' if overview.key_players else ''}
    {f'<h2>Crowded areas</h2><ul>{crowded_html}</ul>' if overview.crowded_areas else ''}
    {f'<h2>Gaps</h2><ul>{gaps_html}</ul>' if overview.gaps else ''}
  </div>

  <h2>Entity profiles ({len(verified_cards)})</h2>
  {cards_html}
  {quarantined_html}
  {long_tail_html}

  <div class="footer">
    <div><strong>Provider manifest:</strong> search &mdash; {_esc(providers)}</div>
    <div>models &mdash; {_esc(models)}</div>
    {manifest_notes}
    <div><strong>Cost:</strong> ${report.cost.total_usd:.2f} across {report.cost.llm_calls}
      model calls ({report.cost.input_tokens:,} in / {report.cost.output_tokens:,} out tokens)</div>
    {f'<ul>{coverage}</ul>' if coverage else ''}
    <div>Verification: every &#10003; claim links to a verbatim quote found in the cited
      source. &#9675; claims could not be grounded and are shown as unverified.
      Generated by <a href="https://github.com/trgallagher-research/landscape-scanner">landscape-scanner</a>.</div>
  </div>
</div>
</body>
</html>"""

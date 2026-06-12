# Landscape Scanner

Ask a landscape question in plain English — *"entrepreneurship programmes,
initiatives and interventions, current and past, in Kenya"* — and get back a
**verification-grounded landscape report**: an executive overview you can read
in a minute, entity cards with key features at a glance, and a full audit
trail (every claim → the verbatim quote it came from → the source link) one
click down.

Built for researchers who want a fast, honest field scan without the cost of a
consultancy or the hallucination risk of asking a chatbot.

## What makes it trustworthy

- **Anchor-constrained extraction.** The model never writes what it "knows"
  about an entity. It can only *select* from text spans actually present in
  scraped sources, so facts can't be imported from the model's imagination.
- **Verbatim verification.** Every decision-bearing claim must trace to an
  exact quote found in a real source, checked by deterministic string
  matching. Claims that can't be grounded are shown as *unverified* — never
  hidden, never dressed up as fact.
- **Quarantine, not deletion.** Entities whose existence can't be confirmed
  are kept and clearly flagged, so you see the engine's uncertainty instead
  of a silently cleaned-up list.
- **No silent fakery.** A live run refuses to start if a required API key is
  missing. Every report carries a provider manifest stating exactly which
  search engines and models were used.

## What makes it cheap (~$0.50–$2 per run)

A funnel: broad discovery across multiple search indexes, a cheap triage pass
over snippets, then deep profiling (scrape + verify) of only the top ~20–25
entities. The long tail stays in the report as one-line entries. Inexpensive
models handle high-volume extraction; verification stays on a trusted model.
A live cost meter (real token usage, not estimates) enforces the budget you
set — runs halt resumably, never overspend.

## Quickstart

```bash
pip install -e ".[app,pdf]"      # engine + local web UI + PDF scraping
scanner ui                       # open http://127.0.0.1:8000
```

In the browser: add your API keys on the **Keys** screen (saved to a local,
gitignored `.env`), type a question on the **Run** screen, watch the live
cost on **Progress**, then browse **Results** and download a shareable
single-file HTML report.

Prefer the command line?

```bash
scanner keys                                   # show which keys are set
scanner run --question "entrepreneurship programmes, current and past, in Kenya" \
            --geography Kenya --budget 2.0 --out report.html
```

## Status

Core engine, local web UI, and shareable HTML reports are built and tested
(offline test suite). Calibration against a hand-built gold set is the
remaining step before fully trusting numbers on a new domain.

## Keys

API keys live in a local `.env` file (gitignored) or environment variables —
never in code, never committed. Search: `SERPER_API_KEY`, `BRAVE_API_KEY`.
Models: `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`.

## License

MIT

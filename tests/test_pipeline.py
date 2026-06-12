"""End-to-end pipeline test, fully offline.

Stubs stand in for search, scraping, and every LLM task — clearly labelled
test doubles injected by the test, never silent fallbacks inside the
engine. The test proves the funnel shape: discover -> triage -> profile
(with real verification logic running against the stub page text) -> read,
plus resume behaviour and the honesty guarantees in the output.
"""

from pydantic import BaseModel

from scanner import prompts
from scanner.budget import BudgetMeter
from scanner.models import RunConfig, SearchHit
from scanner.pipeline import Pipeline
from scanner.search.base import MultiSearch, SearchProvider
from scanner.state import RunStore

# One fake page of source text. The verification core runs FOR REAL against
# this text — claims that appear here verify, claims that don't, don't.
ACME_PAGE = (
    "The Acme Youth Enterprise Fund was founded in 2015 by a coalition of banks. "
    "It operates in 14 counties across Kenya, focusing on rural entrepreneurs. "
    "The programme closed to new applicants in 2022."
)


class StubSearchProvider(SearchProvider):
    def __init__(self, name: str):
        self.name = name

    def search(self, query, max_results=10):
        return [
            SearchHit(url="https://acme.example.org/about", title="Acme Fund", snippet="Acme Youth Enterprise Fund — rural Kenya", index=self.name),
            SearchHit(url="https://ghost.example.org", title="Ghost Initiative", snippet="mentioned once", index=self.name),
        ]


class StubRouter:
    """Returns canned, schema-correct outputs per task and counts calls."""

    def __init__(self):
        self.meter = BudgetMeter(budget_usd=100.0)
        self.models_used = {"all": "stub-model"}
        self.calls_by_task: dict[str, int] = {}

    def call_json(self, task, system, prompt, schema: type[BaseModel], max_tokens=8192):
        self.calls_by_task[task] = self.calls_by_task.get(task, 0) + 1
        if task == "frame":
            return prompts.FrameOutput(
                search_queries=["q1", "q2", "q3", "q4", "q5"],
                segments=[
                    prompts.ProposedSegment(label="Finance", description="funding-focused"),
                    prompts.ProposedSegment(label="Training", description="skills-focused"),
                ],
            )
        if task == "extract_entities":
            return prompts.ExtractOutput(
                entities=[
                    prompts.ExtractedEntity(name="Acme Youth Enterprise Fund", source_urls=["https://acme.example.org/about"]),
                    prompts.ExtractedEntity(name="Ghost Initiative", source_urls=["https://ghost.example.org"]),
                ]
            )
        if task == "triage":
            return prompts.TriageOutput(
                scores=[
                    prompts.TriageScore(name="Acme Youth Enterprise Fund", relevance=9),
                    prompts.TriageScore(name="Ghost Initiative", relevance=6),
                ]
            )
        if task == "span_discovery":
            # Mix of real verbatim spans and one fabricated span — the
            # pipeline must discard the fabricated one.
            return prompts.SpanOutput(
                spans=[
                    prompts.Span(text="founded in 2015 by a coalition of banks", kind="fact"),
                    prompts.Span(text="operates in 14 counties across Kenya", kind="fact"),
                    prompts.Span(text="won the 2019 Global Impact Award", kind="fact"),  # NOT in page
                ]
            )
        if task == "attribute_population":
            return prompts.PopulateOutput(
                one_liner="Bank-backed fund for rural Kenyan entrepreneurs.",
                form="programme",
                segment="Finance",
                features=[
                    prompts.PopulatedFeature(label="Founded", text="founded in 2015 by a coalition of banks"),
                    prompts.PopulatedFeature(label="Award", text="won the 2019 Global Impact Award"),
                ],
            )
        if task == "relation_check":
            return prompts.RelationOutput(supported=False, span="")
        if task == "read":
            return prompts.ReadOutput(
                headline="A small, finance-heavy landscape.",
                key_players=["Acme Youth Enterprise Fund"],
                gaps=["No training-segment entities verified"],
            )
        raise AssertionError(f"unexpected task {task}")


def make_pipeline(tmp_path, shortlist_size=1):
    config = RunConfig(
        question="entrepreneurship programmes in Kenya",
        shortlist_size=shortlist_size,
        scrape_pages_per_entity=2,
    )
    router = StubRouter()
    search = MultiSearch([StubSearchProvider("serper"), StubSearchProvider("brave")])
    store = RunStore(tmp_path / "runs", "test-run")
    pipeline = Pipeline(config, router, search, store, router.meter)

    # Stub the scrape ladder: Acme's page resolves, Ghost's does not.
    def fake_fetch(url):
        if "acme" in url:
            return "scraped", ACME_PAGE
        return "unreachable", None

    pipeline.scraper._fetch_uncached = fake_fetch
    return pipeline, router


def test_full_run_produces_grounded_report(tmp_path):
    pipeline, router = make_pipeline(tmp_path)
    report = pipeline.run()

    # Funnel shape: 1 deep-profiled, 1 long tail.
    assert len(report.entities) == 1
    assert len(report.long_tail) == 1
    acme = report.entities[0]
    assert acme.name == "Acme Youth Enterprise Fund"
    assert acme.quarantined is False
    assert acme.cross_index_agreement is True  # both stub indexes returned it

    # The verbatim-grounded feature verified by restoration; the fabricated
    # award claim stayed unverified (its span was never in the page).
    by_label = {f.label: f for f in acme.features}
    assert by_label["Founded"].verdict == "verified"
    assert by_label["Founded"].supporting_span in ACME_PAGE
    assert by_label["Award"].verdict == "unverified"

    # Manifest honesty: providers recorded, no demo flag.
    assert report.manifest.search_providers == {"serper": True, "brave": True}
    assert report.manifest.demo_mode is False

    # Long-tail entry is marked as not deep-profiled.
    assert report.long_tail[0].deep_profiled is False


def test_fabricated_spans_are_discarded_at_inventory(tmp_path):
    """The span the stub model invented must not survive into the cached
    inventory — the anchor constraint filters at the source."""
    pipeline, router = make_pipeline(tmp_path)
    pipeline.run()
    # Find the cached span inventories and check none contain the fake.
    span_files = list((pipeline.store.spans_cache_dir).glob("*.json"))
    assert span_files
    for file in span_files:
        assert "Global Impact Award" not in file.read_text(encoding="utf-8")


def test_resume_skips_completed_stages(tmp_path):
    """Running the same pipeline twice must not repeat any LLM work."""
    pipeline, router = make_pipeline(tmp_path)
    pipeline.run()
    first_run_calls = dict(router.calls_by_task)

    # New pipeline over the SAME store: everything should come from disk.
    pipeline2, router2 = make_pipeline(tmp_path)
    pipeline2.store = pipeline.store  # same run directory
    pipeline2.run()
    # Only the read stage re-runs (the report is assembled fresh), nothing
    # upstream of it does.
    assert "frame" not in router2.calls_by_task
    assert "span_discovery" not in router2.calls_by_task
    assert "attribute_population" not in router2.calls_by_task
    assert first_run_calls["frame"] == 1


def test_unreachable_only_entity_is_quarantined(tmp_path):
    """An entity whose sources all fail to fetch is quarantined with an
    honest basis — not dropped, not faked."""
    pipeline, router = make_pipeline(tmp_path, shortlist_size=2)
    report = pipeline.run()
    ghost = next(card for card in report.entities if card.name == "Ghost Initiative")
    assert ghost.quarantined is True
    assert "unreachable" in ghost.limitations or "fetched" in ghost.confidence.basis

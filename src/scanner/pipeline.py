"""The pipeline funnel: discover broadly, profile deeply, report honestly.

Stage order (each persists to disk before the next starts, so any halt —
budget reached, crash, Ctrl-C — resumes without repeating paid work):

    frame     one cheap call: search queries + proposed segments
    discover  run queries across all live search indexes; extract entity
              names from the results (anchor-constrained: only names that
              appear in result text)
    triage    rank ALL candidates from snippets (batched, cheap); the top
              ``shortlist_size`` go forward, the rest become the long tail
    profile   per shortlisted entity: scrape a few pages, extract the span
              inventory, assemble features FROM spans, verify every claim
              (restoration first, LLM relation-check last), compute
              confidence, quarantine what can't be confirmed
    read      one synthesis call over the verified material only

Cost shape: triage is one-to-two batched calls over snippets; profiling is
where money goes, and it is capped by the shortlist size; verification is
mostly free (restoration). The budget meter can halt the run at any call —
resumably.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, Field

from . import prompts
from .budget import BudgetExceeded, BudgetMeter
from .llm.router import ModelRouter
from .models import (
    Claim,
    Confidence,
    EntityCard,
    Overview,
    ProviderManifest,
    Report,
    RunConfig,
    SegmentSummary,
    SourceRef,
)
from .scrape import Scraper
from .search.base import MultiSearch
from .state import RunStore
from .verify import normalise, verify_claim


class Candidate(BaseModel):
    """One discovered entity, pre-profiling."""

    name: str
    sources: list[SourceRef] = Field(default_factory=list)
    indexes: list[str] = Field(default_factory=list)  # which search indexes surfaced it
    relevance: int = 0  # triage score 0-10


class RunHalted(Exception):
    """The run stopped resumably (budget). State is on disk; re-run with a
    higher budget to continue from where it stopped."""


class Pipeline:
    """Orchestrates one scan end to end, with disk-first resume."""

    def __init__(
        self,
        config: RunConfig,
        router: ModelRouter,
        search: MultiSearch,
        store: RunStore,
        meter: BudgetMeter,
    ):
        self.config = config
        self.router = router
        self.search = search
        self.store = store
        self.meter = meter
        self.scraper = Scraper(
            cache_dir=store.scrape_cache_dir,
            max_chars=config.max_page_chars,
        )
        self.coverage_notes: list[str] = []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> Report:
        """Run (or resume) the scan. Raises RunHalted on budget stop."""
        try:
            plan = self._stage_frame()
            candidates = self._stage_discover(plan)
            shortlist, long_tail = self._stage_triage(candidates)
            profiled = self._stage_profile(shortlist, plan)
            return self._stage_read(plan, profiled, long_tail)
        except BudgetExceeded as stop:
            # Everything completed so far is already on disk; the same
            # command with a higher budget continues from the halt point.
            raise RunHalted(str(stop)) from stop

    # ------------------------------------------------------------------
    # Stage 1: frame
    # ------------------------------------------------------------------

    def _stage_frame(self) -> prompts.FrameOutput:
        if self.store.has_stage("frame"):
            return prompts.FrameOutput.model_validate(self.store.load_stage("frame"))
        plan = self.router.call_json(
            "frame",
            prompts.FRAME_SYSTEM,
            prompts.frame_prompt(
                self.config.question, self.config.geography, self.config.max_search_queries
            ),
            prompts.FrameOutput,
        )
        # Respect the query budget regardless of what the model proposed.
        plan.search_queries = plan.search_queries[: self.config.max_search_queries]
        self.store.save_stage("frame", plan.model_dump())
        return plan

    # ------------------------------------------------------------------
    # Stage 2: discover
    # ------------------------------------------------------------------

    def _stage_discover(self, plan: prompts.FrameOutput) -> list[Candidate]:
        if self.store.has_stage("discover"):
            return [Candidate.model_validate(c) for c in self.store.load_stage("discover")]

        # Run every query across all live indexes; collect deduped hits.
        all_hits = []
        for query in plan.search_queries:
            all_hits.extend(self.search.search(query, max_results_per_provider=10))
        for provider, error in self.search.failures.items():
            self.coverage_notes.append(f"Search provider degraded: {provider} ({error})")

        # Extract entity names from the hits, in batches the model can read.
        candidates: dict[str, Candidate] = {}
        hit_by_url = {hit.url: hit for hit in all_hits}
        batch_size = 25
        hits_list = list(hit_by_url.values())
        for start in range(0, len(hits_list), batch_size):
            batch = hits_list[start : start + batch_size]
            block = "\n".join(
                f"{i + 1}. {hit.title} — {hit.snippet} — {hit.url}"
                for i, hit in enumerate(batch)
            )
            extraction = self.router.call_json(
                "extract_entities",
                prompts.EXTRACT_SYSTEM,
                prompts.extract_prompt(self.config.question, block),
                prompts.ExtractOutput,
            )
            for entity in extraction.entities:
                key = normalise(entity.name)
                if not key:
                    continue
                candidate = candidates.setdefault(key, Candidate(name=entity.name))
                for url in entity.source_urls:
                    hit = hit_by_url.get(url)
                    if hit and all(s.url != url for s in candidate.sources):
                        candidate.sources.append(
                            SourceRef(url=url, title=hit.title, snippet=hit.snippet, index=hit.index)
                        )

        # Cross-index agreement: which independent indexes surfaced each candidate.
        for candidate in candidates.values():
            seen = set()
            for source in candidate.sources:
                seen.update(
                    self.search.indexes_by_url.get(source.url, {source.index})
                )
            candidate.indexes = sorted(seen)

        result = list(candidates.values())
        self.store.save_stage("discover", [c.model_dump() for c in result])
        return result

    # ------------------------------------------------------------------
    # Stage 3: triage (the funnel gate)
    # ------------------------------------------------------------------

    def _stage_triage(self, candidates: list[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
        if self.store.has_stage("triage"):
            data = self.store.load_stage("triage")
            return (
                [Candidate.model_validate(c) for c in data["shortlist"]],
                [Candidate.model_validate(c) for c in data["long_tail"]],
            )

        # Score every candidate from its snippets, in batches.
        scores: dict[str, int] = {}
        batch_size = 40
        for start in range(0, len(candidates), batch_size):
            batch = candidates[start : start + batch_size]
            block = "\n".join(
                f"- {c.name} — " + " | ".join(s.snippet for s in c.sources[:3] if s.snippet)
                for c in batch
            )
            triage = self.router.call_json(
                "triage",
                prompts.TRIAGE_SYSTEM,
                prompts.triage_prompt(self.config.question, block),
                prompts.TriageOutput,
            )
            for score in triage.scores:
                scores[normalise(score.name)] = score.relevance

        for candidate in candidates:
            candidate.relevance = scores.get(normalise(candidate.name), 0)

        # Rank: relevance first, then cross-index agreement, then source count.
        ranked = sorted(
            candidates,
            key=lambda c: (c.relevance, len(c.indexes), len(c.sources)),
            reverse=True,
        )
        relevant = [c for c in ranked if c.relevance >= 4]  # drop clear noise
        dropped = len(ranked) - len(relevant)
        if dropped:
            self.coverage_notes.append(
                f"{dropped} low-relevance candidates (score < 4) excluded at triage."
            )
        shortlist = relevant[: self.config.shortlist_size]
        long_tail = relevant[self.config.shortlist_size :]

        self.store.save_stage(
            "triage",
            {
                "shortlist": [c.model_dump() for c in shortlist],
                "long_tail": [c.model_dump() for c in long_tail],
            },
        )
        return shortlist, long_tail

    # ------------------------------------------------------------------
    # Stage 4: profile (scrape -> spans -> features -> verify)
    # ------------------------------------------------------------------

    def _stage_profile(
        self, shortlist: list[Candidate], plan: prompts.FrameOutput
    ) -> list[EntityCard]:
        # Incremental resume: cards already profiled are loaded and skipped.
        done: list[EntityCard] = []
        if self.store.has_stage("profile"):
            done = [EntityCard.model_validate(c) for c in self.store.load_stage("profile")]
        done_names = {normalise(card.name) for card in done}
        segment_labels = [segment.label for segment in plan.segments]

        for candidate in shortlist:
            if normalise(candidate.name) in done_names:
                continue
            card = self._profile_one(candidate, segment_labels)
            done.append(card)
            # Persist after EVERY entity so a budget halt loses nothing.
            self.store.save_stage("profile", [c.model_dump() for c in done])
        return done

    def _profile_one(self, candidate: Candidate, segment_labels: list[str]) -> EntityCard:
        """Profile one entity: scrape, extract spans, populate, verify."""
        # -- scrape the top sources ----------------------------------------
        scraped_sources: list[SourceRef] = []
        for source in candidate.sources[: self.config.scrape_pages_per_entity]:
            status, text = self.scraper.fetch(source.url)
            scraped_sources.append(
                source.model_copy(update={"scrape_status": status, "full_text": text})
            )
        usable = [s for s in scraped_sources if s.scrape_status == "scraped"]

        # An entity with no readable source can't be verified: quarantine.
        if not usable:
            return EntityCard(
                name=candidate.name,
                quarantined=True,
                cross_index_agreement=len(candidate.indexes) >= 2,
                confidence=Confidence(level="low", basis="no source could be fetched"),
                sources=[s.without_full_text() for s in scraped_sources],
                limitations="All candidate sources were unreachable; nothing could be verified.",
            )

        # -- span inventory (anchor-constrained vocabulary) ----------------
        spans: list[str] = []
        for source in usable:
            spans.extend(self._spans_for(candidate.name, source))
        spans = list(dict.fromkeys(spans))  # dedupe, keep order

        # -- populate the profile by SELECTING from spans ------------------
        spans_block = "\n".join(f"- {span}" for span in spans[:120])
        populated = self.router.call_json(
            "attribute_population",
            prompts.POPULATE_SYSTEM,
            prompts.populate_prompt(
                candidate.name, self.config.question, segment_labels, spans_block
            ),
            prompts.PopulateOutput,
        )

        # -- verify every feature claim -------------------------------------
        relation_checker = self._make_relation_checker()
        features: list[Claim] = []
        for feature in populated.features:
            claim = Claim(label=feature.label, text=feature.text)
            features.append(verify_claim(claim, usable, relation_checker))

        # -- existence + confidence -----------------------------------------
        exists = any(
            normalise(candidate.name) in normalise(source.full_text or "")
            for source in usable
        )
        verified_count = sum(1 for f in features if f.verdict == "verified")
        cross_index = len(candidate.indexes) >= 2
        confidence = self._confidence(exists, verified_count, len(features), cross_index)

        unreachable_count = sum(1 for s in scraped_sources if s.scrape_status == "unreachable")
        limitations_parts = []
        if verified_count < len(features):
            limitations_parts.append(
                f"{len(features) - verified_count} of {len(features)} key features could not be verified."
            )
        if unreachable_count:
            limitations_parts.append(f"{unreachable_count} source(s) unreachable.")

        return EntityCard(
            name=candidate.name,
            form=populated.form,
            segment=populated.segment if populated.segment in segment_labels else "",
            one_liner=populated.one_liner,
            features=features,
            confidence=confidence,
            quarantined=not exists,
            cross_index_agreement=cross_index,
            sources=[s.without_full_text() for s in scraped_sources],
            limitations=" ".join(limitations_parts),
        )

    def _spans_for(self, entity_name: str, source: SourceRef) -> list[str]:
        """Span inventory for one (entity, page) — cached on disk.

        Spans the model returns are kept ONLY if they are verbatim
        substrings of the page; anything else is discarded on the spot.
        That filter is the anchor constraint: the downstream profile can
        only be assembled from text that provably exists in a source.
        """
        cache_key = hashlib.sha256(
            f"{source.url}::{normalise(entity_name)}".encode("utf-8")
        ).hexdigest()[:24]
        cached = self.store.load_spans(cache_key)
        if cached is not None:
            return cached

        output = self.router.call_json(
            "span_discovery",
            prompts.SPAN_SYSTEM,
            prompts.span_prompt(entity_name, source.full_text or ""),
            prompts.SpanOutput,
        )
        verbatim = [
            span.text.strip()
            for span in output.spans
            if span.text.strip() and span.text.strip() in (source.full_text or "")
        ]
        self.store.save_spans(cache_key, verbatim)
        return verbatim

    def _make_relation_checker(self):
        """Bridge verify.py's injected checker to the router (always Claude)."""

        def check(claim_text: str, passages: list[str]) -> str | None:
            result = self.router.call_json(
                "relation_check",
                prompts.RELATION_SYSTEM,
                prompts.relation_prompt(claim_text, passages),
                prompts.RelationOutput,
                max_tokens=2048,
            )
            return result.span if result.supported else None

        return check

    @staticmethod
    def _confidence(exists: bool, verified: int, total: int, cross_index: bool) -> Confidence:
        """Transparent confidence rules (the basis string IS the audit trail)."""
        basis_parts = []
        basis_parts.append("existence verified" if exists else "existence NOT verified")
        basis_parts.append(f"{verified} of {total} key features grounded")
        if cross_index:
            basis_parts.append("found by 2+ independent search indexes")
        basis = "; ".join(basis_parts)

        if not exists:
            return Confidence(level="low", basis=basis)
        if verified >= 2 and cross_index:
            return Confidence(level="high", basis=basis)
        if verified >= 1:
            return Confidence(level="medium", basis=basis)
        return Confidence(level="low", basis=basis)

    # ------------------------------------------------------------------
    # Stage 5: read (executive overview) + report assembly
    # ------------------------------------------------------------------

    def _stage_read(
        self,
        plan: prompts.FrameOutput,
        profiled: list[EntityCard],
        long_tail_candidates: list[Candidate],
    ) -> Report:
        # The read sees VERIFIED features only — the overview can't be
        # built on unverified material.
        lines = []
        for card in profiled:
            if card.quarantined:
                continue
            verified = "; ".join(
                f"{f.label}: {f.text}" for f in card.features if f.verdict == "verified"
            )
            lines.append(
                f"- {card.name} [{card.segment or 'unsegmented'}, "
                f"confidence {card.confidence.level}] {verified}"
            )
        read = self.router.call_json(
            "read",
            prompts.READ_SYSTEM,
            prompts.read_prompt(self.config.question, "\n".join(lines) or "(none verified)"),
            prompts.ReadOutput,
        )

        # Segment summaries from the actual profiled population.
        segments = []
        for segment in plan.segments:
            names = [c.name for c in profiled if c.segment == segment.label and not c.quarantined]
            segments.append(
                SegmentSummary(
                    label=segment.label,
                    description=read.segment_descriptions.get(segment.label, segment.description),
                    entity_names=names,
                )
            )

        confidence_counts: dict[str, int] = {}
        for card in profiled:
            key = "quarantined" if card.quarantined else card.confidence.level
            confidence_counts[key] = confidence_counts.get(key, 0) + 1

        # Long-tail cards: found, triaged relevant, not deep-profiled.
        long_tail_cards = [
            EntityCard(
                name=c.name,
                deep_profiled=False,
                cross_index_agreement=len(c.indexes) >= 2,
                one_liner=(c.sources[0].snippet if c.sources else ""),
                sources=[s.without_full_text() for s in c.sources[:2]],
                confidence=Confidence(level="low", basis="not deep-profiled (long tail)"),
            )
            for c in long_tail_candidates
        ]
        if long_tail_cards:
            self.coverage_notes.append(
                f"{len(long_tail_cards)} relevant entities beyond the shortlist were not "
                f"deep-profiled (cost control); they appear in the long tail."
            )

        manifest = ProviderManifest(
            search_providers={
                name: name not in self.search.failures
                for name in self.search.provider_names()
            },
            models_by_task=dict(self.router.models_used),
            demo_mode=self.config.demo_mode,
            notes=list(self.search.failures and [f"search failures: {sorted(self.search.failures)}"] or []),
        )
        if self.meter.reasoning_alarm:
            manifest.notes.append(
                "ALERT: a model emitted hidden reasoning tokens despite non-thinking "
                "configuration; their cost is included in the total."
            )

        report = Report(
            run_config=self.config,
            overview=Overview(
                headline=read.headline,
                segments=segments,
                key_players=read.key_players,
                crowded_areas=read.crowded_areas,
                gaps=read.gaps,
                confidence_counts=confidence_counts,
            ),
            entities=profiled,
            long_tail=long_tail_cards,
            manifest=manifest,
            cost=self.meter.summary(),
            coverage_notes=self.coverage_notes,
        )
        self.store.save_stage("report", report.model_dump())
        return report


def build_pipeline(config: RunConfig, keys, runs_dir: Path) -> Pipeline:
    """Wire a live pipeline from config + keys. Refuses to run without the
    required keys — there is no silent fake fallback, by design."""
    from .keys import ProviderKeys  # local import to avoid cycles in tests
    from .llm.anthropic_client import AnthropicClient
    from .llm.openrouter import OpenRouterClient
    from .search.brave import BraveProvider
    from .search.serper import SerperProvider
    from .state import make_run_id

    assert isinstance(keys, ProviderKeys)
    missing = keys.missing_for_live_run()
    if missing and not config.demo_mode:
        raise RuntimeError(
            f"Cannot start a live run: missing API key(s) for {', '.join(missing)}. "
            f"Set them in .env or the Keys screen. (No silent fake fallback — by design.)"
        )

    search_providers = [SerperProvider(keys.get("serper"))]
    if keys.has("brave"):
        search_providers.append(BraveProvider(keys.get("brave")))

    clients = {"anthropic": AnthropicClient(keys.get("anthropic"))}
    if keys.has("openrouter"):
        clients["openrouter"] = OpenRouterClient(keys.get("openrouter"))

    meter = BudgetMeter(budget_usd=config.budget_usd)
    router = ModelRouter(clients, meter, profile=config.model_profile)
    store = RunStore(runs_dir, make_run_id(config.question))
    return Pipeline(config, router, MultiSearch(search_providers), store, meter)

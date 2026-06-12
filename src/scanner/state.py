"""Disk-first run state: every stage persists before the next one starts.

A run lives under ``runs/<run_id>/``:

    runs/<run_id>/
        stage_frame.json       # the run plan
        stage_discover.json    # candidates found
        stage_triage.json      # shortlist + long tail
        stage_profile.json     # profiled entity cards (appended incrementally)
        report.json            # the final deliverable
        cache/scrape/          # per-URL fetched text (success AND failure)
        cache/spans/           # per URL+entity span inventories

Because state is on disk, a halted run (budget reached, crash, Ctrl-C)
resumes from the last completed stage, and profiling resumes from the last
completed ENTITY — nothing already paid for is paid for twice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def make_run_id(question: str) -> str:
    """A readable, filesystem-safe run id derived from the question."""
    slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")[:48]
    return slug or "run"


class RunStore:
    """Reads and writes one run's state directory."""

    def __init__(self, runs_dir: Path, run_id: str):
        """runs_dir: the top-level runs directory (e.g. ./runs)."""
        self.run_dir = Path(runs_dir) / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.scrape_cache_dir = self.run_dir / "cache" / "scrape"
        self.spans_cache_dir = self.run_dir / "cache" / "spans"
        self.spans_cache_dir.mkdir(parents=True, exist_ok=True)

    def _stage_path(self, stage: str) -> Path:
        return self.run_dir / f"stage_{stage}.json"

    def has_stage(self, stage: str) -> bool:
        """True if this stage already completed in a previous attempt."""
        return self._stage_path(stage).is_file()

    def save_stage(self, stage: str, data: Any) -> None:
        """Persist a stage's output atomically (write temp, then replace)."""
        path = self._stage_path(stage)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        temp_path.replace(path)

    def load_stage(self, stage: str) -> Any:
        """Load a previously persisted stage's output."""
        return json.loads(self._stage_path(stage).read_text(encoding="utf-8"))

    # -- span inventory cache (per URL + entity) ---------------------------

    def spans_cache_path(self, cache_key: str) -> Path:
        return self.spans_cache_dir / f"{cache_key}.json"

    def load_spans(self, cache_key: str) -> list[str] | None:
        """Cached span texts for one URL+entity, or None when not cached."""
        path = self.spans_cache_path(cache_key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def save_spans(self, cache_key: str, spans: list[str]) -> None:
        self.spans_cache_path(cache_key).write_text(
            json.dumps(spans, ensure_ascii=False), encoding="utf-8"
        )

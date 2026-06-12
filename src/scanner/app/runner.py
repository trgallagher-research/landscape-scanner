"""Background run manager: executes a scan off the web request thread.

The pipeline is synchronous and can take minutes, so the web app starts it
in a daemon thread and polls a shared status object. The pipeline reports
progress through a callback (stage changes, per-entity ticks, live cost);
the manager stores the latest snapshot for the Progress screen to read.

Only one run executes at a time per process — this is a single-user local
tool, and serialising keeps the budget meter and cost readout unambiguous.
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Report, RunConfig


@dataclass
class RunStatus:
    """A snapshot of an in-flight or finished run, read by the Progress UI."""

    run_id: str
    question: str
    state: str = "starting"          # starting | running | done | halted | error
    stage: str = "frame"             # current pipeline stage
    detail: str = ""                 # human-readable progress line
    profiled: int = 0                # entities deep-profiled so far
    shortlist_total: int = 0         # how many will be profiled in total
    spent_usd: float = 0.0
    budget_usd: float = 0.0
    error: str = ""                  # populated when state == "error"
    report: Report | None = None     # populated when state == "done"


# Friendly labels for each pipeline stage, shown in the tracker.
STAGE_LABELS = {
    "frame": "Planning searches",
    "discover": "Searching & finding entities",
    "triage": "Ranking candidates",
    "profile": "Profiling & verifying entities",
    "read": "Writing the overview",
}
STAGE_ORDER = ["frame", "discover", "triage", "profile", "read"]


class RunManager:
    """Owns the single active run and its status snapshot."""

    def __init__(self, runs_dir: Path):
        self.runs_dir = runs_dir
        self._status: RunStatus | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def status(self) -> RunStatus | None:
        """The current run's status snapshot (or None if none has started)."""
        return self._status

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, config: RunConfig, keys) -> RunStatus:
        """Begin a scan in the background. Raises if one is already running."""
        if self.is_running():
            raise RuntimeError("A scan is already running; wait for it to finish.")

        from ..state import make_run_id

        status = RunStatus(
            run_id=make_run_id(config.question),
            question=config.question,
            budget_usd=config.budget_usd,
        )
        self._status = status
        self._thread = threading.Thread(
            target=self._run, args=(config, keys, status), daemon=True
        )
        self._thread.start()
        return status

    def _run(self, config: RunConfig, keys, status: RunStatus) -> None:
        """Thread body: build the pipeline, wire progress, run it."""
        from ..pipeline import RunHalted, build_pipeline

        try:
            pipeline = build_pipeline(config, keys, runs_dir=self.runs_dir)
        except RuntimeError as error:
            # Missing keys / misconfig — surfaced verbatim to the UI.
            status.state = "error"
            status.error = str(error)
            return

        # Wire the pipeline's progress hook to our status snapshot.
        pipeline.on_progress = lambda **kw: self._update(status, **kw)
        status.state = "running"

        try:
            report = pipeline.run()
            status.report = report
            status.spent_usd = report.cost.total_usd
            status.state = "done"
            status.detail = "Scan complete."
        except RunHalted as halt:
            status.state = "halted"
            status.error = str(halt)
            status.spent_usd = pipeline.meter.spent_usd
        except Exception as error:  # noqa: BLE001 - last-resort guard for the UI
            status.state = "error"
            status.error = f"{type(error).__name__}: {error}"
            status.detail = traceback.format_exc(limit=3)

    def _update(self, status: RunStatus, **fields) -> None:
        """Apply a progress update from the pipeline (thread-safe enough for
        single-writer/single-reader snapshots)."""
        with self._lock:
            for key, value in fields.items():
                setattr(status, key, value)
            # Always refresh the live cost from the meter via the detail hook.

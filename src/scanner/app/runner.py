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


def _drive_pipeline(status, config, keys, runs_dir, run_id, update) -> None:
    """Thread body shared by both managers: build the pipeline, wire progress,
    run it, and record the outcome on the status snapshot. ``run_id`` names the
    on-disk run directory; ``update`` is a thread-safe field setter."""
    from ..pipeline import RunHalted, build_pipeline

    try:
        pipeline = build_pipeline(config, keys, runs_dir=runs_dir, run_id=run_id)
    except RuntimeError as error:
        # Missing keys / misconfig — surfaced verbatim.
        status.state = "error"
        status.error = str(error)
        return

    pipeline.on_progress = lambda **kw: update(status, **kw)
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
    except Exception as error:  # noqa: BLE001 - last-resort guard
        status.state = "error"
        status.error = f"{type(error).__name__}: {error}"
        status.detail = traceback.format_exc(limit=3)


class RunManager:
    """Owns the single active run and its status snapshot (local UI / CLI)."""

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
            target=_drive_pipeline,
            args=(status, config, keys, self.runs_dir, status.run_id, self._update),
            daemon=True,
        )
        self._thread.start()
        return status

    def _update(self, status: RunStatus, **fields) -> None:
        """Apply a progress update from the pipeline (thread-safe enough for
        single-writer/single-reader snapshots)."""
        with self._lock:
            for key, value in fields.items():
                setattr(status, key, value)
            # Always refresh the live cost from the meter via the detail hook.


def make_unique_run_id(question: str) -> str:
    """A readable run id that is unique per run, so concurrent scans of the same
    question never share a directory. The question slug stays for legibility; a
    short random suffix guarantees uniqueness."""
    import secrets

    from ..state import make_run_id

    return f"{make_run_id(question)}-{secrets.token_hex(3)}"


class MultiRunManager:
    """Owns many concurrent runs, keyed by run id — the hosted JSON API model.

    Unlike RunManager (one global run, for the local single-user UI), this keeps a
    registry so a multi-user website can run several scans at once and poll each by
    id. Runs still persist to ``runs/<run_id>/`` on disk, so a finished report can be
    reloaded after a process restart even if it's no longer in the registry."""

    def __init__(self, runs_dir: Path):
        self.runs_dir = runs_dir
        self._runs: dict[str, RunStatus] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def start(self, config: RunConfig, keys) -> RunStatus:
        """Begin a scan in the background and return its status (with the run id)."""
        run_id = make_unique_run_id(config.question)
        status = RunStatus(
            run_id=run_id,
            question=config.question,
            budget_usd=config.budget_usd,
        )
        with self._lock:
            self._runs[run_id] = status
        thread = threading.Thread(
            target=_drive_pipeline,
            args=(status, config, keys, self.runs_dir, run_id, self._update),
            daemon=True,
        )
        self._threads[run_id] = thread
        thread.start()
        return status

    def get(self, run_id: str) -> RunStatus | None:
        """The live status for a run, or None if this process never started it."""
        return self._runs.get(run_id)

    def recover_status(self, run_id: str) -> RunStatus | None:
        """Best-effort status for a run this process doesn't hold in memory.

        After a restart the in-memory registry is empty, but runs persist to
        ``runs/<run_id>/``. If a finished report is on disk, report it as ``done``;
        if the directory exists but never produced a report, the run was
        interrupted (e.g. the service restarted mid-scan) — report it as ``error``
        so a polling client stops instead of waiting forever. None if truly unknown."""
        if not (self.runs_dir / run_id).is_dir():
            return None
        report = self.load_report(run_id)
        if report is not None:
            return RunStatus(
                run_id=run_id,
                question=report.run_config.question,
                state="done",
                stage="read",
                detail="Scan complete.",
                spent_usd=report.cost.total_usd,
                budget_usd=report.run_config.budget_usd,
                report=report,
            )
        return RunStatus(
            run_id=run_id,
            question="",
            state="error",
            error="The scan was interrupted by a service restart and did not finish.",
        )

    def load_report(self, run_id: str):
        """Reload a finished report from disk for a run not held in memory (e.g.
        after a process restart). Returns a Report, or None if absent."""
        from ..models import Report
        from ..state import RunStore

        # Don't materialise a directory for an unknown id (RunStore would mkdir it).
        if not (self.runs_dir / run_id).is_dir():
            return None
        store = RunStore(self.runs_dir, run_id)
        if not store.has_stage("report"):
            return None
        return Report.model_validate(store.load_stage("report"))

    def _update(self, status: RunStatus, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(status, key, value)

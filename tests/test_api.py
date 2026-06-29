"""Tests for the hosted JSON API (scanner.app.api) and the MultiRunManager.

Route tests use a stub manager so no real run executes. Manager tests use a fake
build_pipeline (no network, no keys) and wait on the background thread. Skipped
cleanly if FastAPI isn't installed (the app is an optional extra).
"""

import types

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from scanner.app import api  # noqa: E402
from scanner.app.runner import MultiRunManager, RunStatus  # noqa: E402
from scanner.models import (  # noqa: E402
    CostSummary,
    Overview,
    Report,
    RunConfig,
)


def _report(question: str = "q") -> Report:
    return Report(
        run_config=RunConfig(question=question),
        overview=Overview(headline="A test landscape."),
        cost=CostSummary(total_usd=0.42, llm_calls=3),
    )


# --- Route tests (stub manager) --------------------------------------------


class StubManager:
    def __init__(self):
        self.started = []

    def start(self, config, keys):
        self.started.append((config, keys))
        return RunStatus(
            run_id="rid-1",
            question=config.question,
            state="starting",
            budget_usd=config.budget_usd,
        )

    def get(self, run_id):
        if run_id == "rid-1":
            return RunStatus(
                run_id="rid-1", question="q", state="done",
                report=_report(), spent_usd=0.42, budget_usd=2.0,
            )
        if run_id == "running-1":
            return RunStatus(run_id="running-1", question="q", state="running")
        return None

    def load_report(self, run_id):
        return _report() if run_id == "ondisk-1" else None


@pytest.fixture
def client(monkeypatch):
    # Keys present so the start handler proceeds to the (stub) manager.
    monkeypatch.setenv("SERPER_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(api, "manager", StubManager())
    return TestClient(api.app)


def test_start_scan_returns_run_id(client):
    r = client.post("/scans", json={"question": "programmes in Kenya", "budget_usd": 1.5})
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == "rid-1"
    assert body["state"] == "starting"


def test_start_scan_requires_question(client):
    r = client.post("/scans", json={"question": "   "})
    assert r.status_code == 400


def test_start_scan_rejects_missing_keys(client, monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # No .env in the test cwd, so required keys are now missing.
    r = client.post("/scans", json={"question": "q"})
    assert r.status_code == 400
    assert "serper" in r.json()["detail"].lower()


def test_status_404_for_unknown_run(client):
    assert client.get("/scans/nope").status_code == 404


def test_status_snapshot_shape(client):
    r = client.get("/scans/rid-1")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "done"
    assert set(["run_id", "stage", "detail", "spent_usd", "budget_usd"]) <= body.keys()


def test_report_returns_when_done(client):
    r = client.get("/scans/rid-1/report")
    assert r.status_code == 200
    assert r.json()["overview"]["headline"] == "A test landscape."


def test_report_409_while_running(client):
    assert client.get("/scans/running-1/report").status_code == 409


def test_report_reloads_from_disk_when_not_in_memory(client):
    r = client.get("/scans/ondisk-1/report")
    assert r.status_code == 200
    assert r.json()["overview"]["headline"] == "A test landscape."


# --- Auth -------------------------------------------------------------------


def test_token_required_when_configured(client, monkeypatch):
    monkeypatch.setenv("SCANNER_SERVICE_TOKEN", "sekret")
    assert client.get("/scans/rid-1").status_code == 401
    ok = client.get("/scans/rid-1", headers={"Authorization": "Bearer sekret"})
    assert ok.status_code == 200


def test_no_token_needed_when_unset(client, monkeypatch):
    monkeypatch.delenv("SCANNER_SERVICE_TOKEN", raising=False)
    assert client.get("/scans/rid-1").status_code == 200


# --- MultiRunManager (real threads, fake pipeline) -------------------------


def _install_fake_pipeline(monkeypatch):
    class FakePipeline:
        def __init__(self, config, runs_dir, run_id):
            self.config = config
            self.on_progress = lambda **k: None
            self.meter = types.SimpleNamespace(spent_usd=0.42)
            self._runs_dir = runs_dir
            self._run_id = run_id

        def run(self):
            from scanner.state import RunStore

            report = _report(self.config.question)
            RunStore(self._runs_dir, self._run_id).save_stage(
                "report", report.model_dump()
            )
            return report

    def fake_build_pipeline(config, keys, runs_dir, run_id=None):
        return FakePipeline(config, runs_dir, run_id or "x")

    import scanner.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "build_pipeline", fake_build_pipeline)


def test_manager_runs_concurrently_with_distinct_ids(monkeypatch, tmp_path):
    _install_fake_pipeline(monkeypatch)
    mgr = MultiRunManager(tmp_path)

    s1 = mgr.start(RunConfig(question="same question"), keys=None)
    s2 = mgr.start(RunConfig(question="same question"), keys=None)
    assert s1.run_id != s2.run_id  # unique despite identical question

    for run_id in (s1.run_id, s2.run_id):
        mgr._threads[run_id].join(timeout=5)
        assert mgr.get(run_id).state == "done"
        assert mgr.get(run_id).report is not None


def test_manager_reloads_report_from_disk(monkeypatch, tmp_path):
    _install_fake_pipeline(monkeypatch)
    mgr = MultiRunManager(tmp_path)
    status = mgr.start(RunConfig(question="reload me"), keys=None)
    mgr._threads[status.run_id].join(timeout=5)

    # A fresh manager (simulating a process restart) has no memory of the run,
    # but the report is on disk.
    fresh = MultiRunManager(tmp_path)
    assert fresh.get(status.run_id) is None
    assert fresh.load_report(status.run_id) is not None
    assert fresh.load_report("never-ran") is None

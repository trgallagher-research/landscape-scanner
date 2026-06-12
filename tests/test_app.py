"""Tests for the web app layer using FastAPI's TestClient.

No real run executes here — the RunManager is exercised with a stubbed
pipeline so the progress/status plumbing is tested without network or keys.
Skipped cleanly if FastAPI isn't installed (the app is an optional extra).
"""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from scanner.app import server  # noqa: E402
from scanner.models import (  # noqa: E402
    Confidence,
    CostSummary,
    EntityCard,
    Overview,
    ProviderManifest,
    Report,
    RunConfig,
)
from scanner.app.runner import RunStatus  # noqa: E402


@pytest.fixture
def client():
    return TestClient(server.app)


def test_run_page_loads(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Scan a landscape" in response.text


def test_keys_page_shows_status_not_values(client):
    response = client.get("/keys")
    assert response.status_code == 200
    assert "API keys" in response.text
    # The page lists providers and a status column.
    assert "serper" in response.text
    assert "Status" in response.text


def test_status_api_reports_none_before_any_run(client, monkeypatch):
    monkeypatch.setattr(server.manager, "_status", None)
    response = client.get("/api/status")
    assert response.json() == {"state": "none"}


def test_results_empty_state(client, monkeypatch):
    monkeypatch.setattr(server.manager, "_status", None)
    response = client.get("/results")
    assert "No results yet" in response.text


def test_results_renders_finished_report(client, monkeypatch):
    """When a report is present, the Results screen renders it and offers
    the downloadable file."""
    report = Report(
        run_config=RunConfig(question="test question"),
        overview=Overview(headline="A test landscape."),
        entities=[
            EntityCard(name="Acme", one_liner="A thing.", confidence=Confidence(level="high"))
        ],
        manifest=ProviderManifest(search_providers={"serper": True}),
        cost=CostSummary(total_usd=0.5, llm_calls=10),
    )
    status = RunStatus(run_id="test", question="test question", state="done", report=report)
    monkeypatch.setattr(server.manager, "_status", status)

    page = client.get("/results")
    assert "A test landscape." in page.text
    assert "Download the shareable HTML report" in page.text

    download = client.get("/results.html")
    assert download.status_code == 200
    assert "attachment" in download.headers.get("content-disposition", "")
    assert "<!DOCTYPE html>" in download.text


def test_run_rejected_when_required_keys_missing(client, monkeypatch):
    """Starting a run without keys must redirect back with an error, never
    silently proceed on fakes."""
    from scanner.keys import ProviderKeys

    # Force a no-keys environment.
    monkeypatch.setattr(
        ProviderKeys, "missing_for_live_run", lambda self: ["serper", "anthropic"]
    )
    monkeypatch.setattr(server.manager, "is_running", lambda: False)

    response = client.post(
        "/run",
        data={"question": "q", "budget": "1.0", "profile": "economy", "shortlist": "5"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # The manager raises on missing keys; we redirect home with the error.
    assert response.headers["location"].startswith("/?error=")

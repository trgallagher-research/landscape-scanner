"""FastAPI server: the four screens plus a small JSON progress API.

Kept deliberately simple — server-rendered HTML with a sprinkle of vanilla
JavaScript for the live progress poll. No templating engine, no frontend
build step. The whole UI is in this file and ``pages.py``.

Security posture: binds to 127.0.0.1 only; key values are never sent back
to the browser (only set/missing status); the Keys form writes to the
local .env via the gitignore-guarded ProviderKeys.save_key.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..keys import KNOWN_PROVIDERS, ProviderKeys
from ..models import RunConfig
from . import pages
from .runner import RunManager

# Where runs are stored (cwd/runs), shared with the CLI.
RUNS_DIR = Path("runs")

app = FastAPI(title="Landscape Scanner")
manager = RunManager(RUNS_DIR)


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> str:
    """The Run screen — the default landing page."""
    keys = ProviderKeys()
    missing = keys.missing_for_live_run()
    error = request.query_params.get("error", "")
    return pages.run_page(
        missing_keys=missing, running=manager.is_running(), error=error
    )


@app.post("/run")
def start_run(
    question: str = Form(...),
    geography: str = Form(""),
    budget: float = Form(2.0),
    profile: str = Form("economy"),
    shortlist: int = Form(25),
) -> RedirectResponse:
    """Kick off a scan and redirect to the Progress screen."""
    keys = ProviderKeys()
    # Refuse a live run up front if required keys are missing — keep the user
    # on the Run screen with a clear message rather than starting a doomed run
    # (and never fall back to fake data).
    missing = keys.missing_for_live_run()
    if missing:
        message = f"Cannot start: missing required key(s): {', '.join(missing)}."
        return RedirectResponse(url=f"/?error={message}", status_code=303)

    config = RunConfig(
        question=question.strip(),
        geography=[g.strip() for g in geography.split(",") if g.strip()],
        budget_usd=budget,
        model_profile=profile if profile in ("economy", "quality") else "economy",
        shortlist_size=shortlist,
    )
    try:
        manager.start(config, keys)
    except RuntimeError as error:
        return RedirectResponse(url=f"/?error={error}", status_code=303)
    return RedirectResponse(url="/progress", status_code=303)


@app.get("/progress", response_class=HTMLResponse)
def progress_page() -> str:
    """The live Progress screen (polls /api/status)."""
    return pages.progress_page(manager.status)


@app.get("/api/status", response_class=JSONResponse)
def api_status() -> dict:
    """JSON snapshot of the active run, polled by the Progress screen."""
    status = manager.status
    if status is None:
        return {"state": "none"}
    return {
        "state": status.state,
        "stage": status.stage,
        "detail": status.detail,
        "profiled": status.profiled,
        "shortlist_total": status.shortlist_total,
        "spent_usd": round(status.spent_usd, 4),
        "budget_usd": status.budget_usd,
        "error": status.error,
    }


@app.get("/results", response_class=HTMLResponse)
def results_page() -> str:
    """The Results screen — renders the finished report inline."""
    status = manager.status
    if status is None or status.report is None:
        return pages.simple_message(
            "No results yet",
            "Run a scan first, then come back here.",
            link="/",
            link_label="Start a scan",
        )
    return pages.results_page(status.report)


@app.get("/results.html")
def download_report() -> HTMLResponse:
    """Download the standalone, shareable HTML report (single file)."""
    from ..report_html import render_html

    status = manager.status
    if status is None or status.report is None:
        return HTMLResponse("<p>No report available.</p>", status_code=404)
    html = render_html(status.report)
    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": 'attachment; filename="landscape-report.html"'},
    )


@app.get("/keys", response_class=HTMLResponse)
def keys_page(request: Request) -> str:
    """The Keys screen — shows set/missing status, accepts new keys."""
    keys = ProviderKeys()
    saved = request.query_params.get("saved", "")
    error = request.query_params.get("error", "")
    return pages.keys_page(keys.statuses(), saved=saved, error=error)


@app.post("/keys")
def save_key(provider: str = Form(...), value: str = Form(...)) -> RedirectResponse:
    """Save one provider key to the local .env (gitignore-guarded)."""
    keys = ProviderKeys()
    try:
        keys.save_key(provider, value)
    except (ValueError, RuntimeError) as error:
        return RedirectResponse(url=f"/keys?error={error}", status_code=303)
    return RedirectResponse(url=f"/keys?saved={provider}", status_code=303)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Launch the app with uvicorn (used by ``scanner ui``)."""
    import uvicorn

    print(f"Landscape Scanner UI: http://{host}:{port}")
    print("Keys stay on this machine. Press Ctrl+C to stop.")
    uvicorn.run(app, host=host, port=port, log_level="warning")

"""Hosted JSON API: the surface a multi-user website calls.

This is deliberately separate from the local UI in ``server.py``. The local UI
is single-run, no-auth, and binds 127.0.0.1 — safe for one person on their own
machine, unsafe to expose. This API is what runs behind a Cloudflare Tunnel: it
is multi-run (keyed by run id), token-authenticated, and renders no HTML. The
no-auth local UI is intentionally NOT mounted here, so it never reaches a tunnel.

Contract: see ``docs/scanner-service-contract.md`` in the website repo
(claude-interviewer). Endpoints:

    POST /scans                 -> { run_id, state }      (starts a background run)
    GET  /scans/{run_id}        -> RunStatus snapshot      (polled)
    GET  /scans/{run_id}/report -> Report JSON             (when state == "done")

Security posture: every route requires a bearer token matching
``SCANNER_SERVICE_TOKEN`` when that env var is set (Cloudflare Access is the
primary gate in front; the bearer is defence-in-depth). If the var is unset the
API fails open — same convention as the rest of the project — so local testing
needs no token. Set it in any real deployment.
"""

from __future__ import annotations

import math
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from ..keys import KNOWN_PROVIDERS, ProviderKeys
from ..models import RunConfig
from .runner import MultiRunManager, TooManyConcurrentRuns

# Where runs are stored (cwd/runs), shared with the CLI and local UI.
RUNS_DIR = Path("runs")

app = FastAPI(title="Landscape Scanner API")
manager = MultiRunManager(RUNS_DIR)


# --- Auth -------------------------------------------------------------------


def require_token(authorization: str = Header(default="")) -> None:
    """Require ``Authorization: Bearer <SCANNER_SERVICE_TOKEN>`` when the token is
    configured. No-op when the env var is unset (fails open for local use)."""
    expected = os.environ.get("SCANNER_SERVICE_TOKEN")
    if not expected:
        return
    prefix = "Bearer "
    presented = authorization[len(prefix):] if authorization.startswith(prefix) else ""
    if not (presented and secrets.compare_digest(presented, expected)):
        raise HTTPException(status_code=401, detail="Unauthorized")


# --- Request models ---------------------------------------------------------


class ScanKeys(BaseModel):
    """Optional per-request provider keys (bring-your-own-key). When present, the
    run uses only these — not the engine's own env keys."""

    serper: Optional[str] = None
    anthropic: Optional[str] = None
    brave: Optional[str] = None
    openrouter: Optional[str] = None


class StartScanRequest(BaseModel):
    question: str
    budget_usd: float = 2.0
    model_profile: str = "default"
    keys: Optional[ScanKeys] = None


def _resolve_keys(keys: Optional[ScanKeys]) -> ProviderKeys:
    """BYOK: build keys from the request when provided (isolated from any local
    .env), else fall back to the engine's own environment."""
    if keys is None:
        return ProviderKeys()
    env = {
        KNOWN_PROVIDERS[name]["env"]: value
        for name, value in keys.model_dump().items()
        if value
    }
    # A non-existent env_file keeps request keys isolated from the NUC's local .env.
    return ProviderKeys(environ=env, env_file=Path("/nonexistent-byok.env"))


def _profile(value: str) -> str:
    """Map the contract's model_profile onto the engine's two profiles.

    Anything outside the two known profiles (including the contract's documented
    default, ``"default"``) deliberately resolves to the cheaper ``economy``."""
    return value if value in ("economy", "quality") else "economy"


# Server-side per-scan spend ceiling. The website clamps too, but the engine must
# not trust the caller: the bearer is only defence-in-depth (Cloudflare Access is
# the primary gate), so a request that slips past both must still not be able to
# drain a key with an arbitrary budget. Configurable via SCANNER_MAX_BUDGET_USD.
DEFAULT_BUDGET_USD = 2.0


def _clamp_budget(value: float) -> float:
    """Clamp a requested budget to a sane, server-enforced range."""
    cap = float(os.environ.get("SCANNER_MAX_BUDGET_USD", "5"))
    if not math.isfinite(value) or value <= 0:
        value = DEFAULT_BUDGET_USD
    return min(value, cap)


# --- Routes -----------------------------------------------------------------


@app.post("/scans", dependencies=[Depends(require_token)])
def start_scan(req: StartScanRequest) -> dict:
    """Start a scan in the background and return its run id immediately."""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="A question is required")

    keys = _resolve_keys(req.keys)
    missing = keys.missing_for_live_run()
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required key(s): {', '.join(missing)}",
        )

    config = RunConfig(
        question=question,
        budget_usd=_clamp_budget(req.budget_usd),
        model_profile=_profile(req.model_profile),
    )
    try:
        status = manager.start(config, keys)
    except TooManyConcurrentRuns as full:
        raise HTTPException(status_code=429, detail=str(full))
    return {"run_id": status.run_id, "state": status.state}


@app.get("/scans/{run_id}", dependencies=[Depends(require_token)])
def scan_status(run_id: str) -> dict:
    """The live status snapshot for a run (mirrors RunStatus)."""
    status = manager.get(run_id)
    if status is None:
        # Not held in memory. It may be a run that finished before a restart, or
        # one a restart interrupted mid-flight. Resolve it from disk so a polling
        # client reaches a terminal state instead of seeing a 404 forever.
        status = manager.recover_status(run_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Unknown run")
    return {
        "run_id": status.run_id,
        "question": status.question,
        "state": status.state,
        "stage": status.stage,
        "detail": status.detail,
        "profiled": status.profiled,
        "shortlist_total": status.shortlist_total,
        "spent_usd": round(status.spent_usd, 4),
        "budget_usd": status.budget_usd,
        "error": status.error,
    }


@app.get("/scans/{run_id}/report", dependencies=[Depends(require_token)])
def scan_report(run_id: str) -> dict:
    """The finished report as JSON. 409 while still running; 404 if unknown.

    Reloads from disk when the run isn't held in memory (e.g. after a restart)."""
    status = manager.get(run_id)
    if status is not None:
        if status.report is not None:
            return status.report.model_dump()
        if status.state in ("starting", "running"):
            raise HTTPException(status_code=409, detail="Scan is not finished yet")
        # done/halted/error but no in-memory report — fall through to disk.

    report = manager.load_report(run_id)
    if report is not None:
        return report.model_dump()
    if status is None:
        raise HTTPException(status_code=404, detail="Unknown run")
    raise HTTPException(status_code=409, detail=f"No report available ({status.state})")


def _serve_preflight(host: str) -> Optional[str]:
    """Validate the serving configuration before launch (pure, so it's testable).

    Raises SystemExit on an unsafe config: serving on a non-local interface with
    no ``SCANNER_SERVICE_TOKEN`` set would leave the API with no app-level auth
    (Cloudflare Access is optional in the contract), so anyone who reached it
    could spend the owner's keys. Returns a warning string for safe-but-notable
    configs (no token, but bound to localhost), or None when all is well."""
    token = os.environ.get("SCANNER_SERVICE_TOKEN")
    is_local = host in ("127.0.0.1", "localhost", "::1")
    if not token and not is_local:
        raise SystemExit(
            f"Refusing to serve on '{host}' without SCANNER_SERVICE_TOKEN set: "
            "the API would have no app-level auth. Set the token (defence-in-depth "
            "behind Cloudflare Access), or bind 127.0.0.1 and reach it via the tunnel."
        )
    if not token:
        return (
            "SCANNER_SERVICE_TOKEN is not set — the API has no app-level auth. "
            "Fine for localhost; set it before exposing this via a tunnel."
        )
    return None


def serve_api(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Launch the hosted JSON API with uvicorn (used by ``scanner serve-api``).

    Default binds localhost — a Cloudflare Tunnel connects to the local port, so
    there is no need to bind a public interface."""
    import uvicorn

    warning = _serve_preflight(host)
    if warning:
        print(f"WARNING: {warning}")
    print(f"Landscape Scanner API: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")

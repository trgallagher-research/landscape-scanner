"""HTML for the four screens.

Plain server-rendered pages sharing one stylesheet and a top nav. The
Results screen reuses the standalone report renderer (report_html) for the
body so the on-screen view and the downloadable file look identical.
"""

from __future__ import annotations

import html

from ..keys import KNOWN_PROVIDERS
from ..models import Report
from ..report_html import render_html
from .runner import STAGE_LABELS, STAGE_ORDER, RunStatus

_NAV = """
<nav class="nav">
  <a href="/">Run</a>
  <a href="/progress">Progress</a>
  <a href="/results">Results</a>
  <a href="/keys">Keys</a>
</nav>
"""

_STYLE = """
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
       background: #f6f8fa; color: #1f2328; }
.nav { background: #1f2328; padding: 10px 20px; }
.nav a { color: #d1d9e0; margin-right: 18px; text-decoration: none; font-size: 0.95rem; }
.nav a:hover { color: #fff; }
.wrap { max-width: 860px; margin: 0 auto; padding: 24px 18px 60px; }
h1 { font-size: 1.5rem; }
label { display: block; margin: 14px 0 4px; font-weight: 600; font-size: 0.92rem; }
input[type=text], input[type=number], select, textarea {
  width: 100%; box-sizing: border-box; padding: 9px 11px; border: 1px solid #d1d9e0;
  border-radius: 6px; font-size: 1rem; font-family: inherit; }
textarea { min-height: 70px; resize: vertical; }
.hint { color: #59636e; font-size: 0.82rem; margin-top: 3px; }
.row { display: flex; gap: 16px; }
.row > div { flex: 1; }
button { background: #1f7a36; color: #fff; border: 0; border-radius: 6px;
         padding: 11px 22px; font-size: 1rem; font-weight: 600; cursor: pointer;
         margin-top: 20px; }
button:hover { background: #196b2e; }
button:disabled { background: #9aa7b1; cursor: not-allowed; }
.card { background: #fff; border: 1px solid #d1d9e0; border-radius: 8px;
        padding: 16px 20px; margin: 14px 0; }
.warn { background: #fff8c5; border: 1px solid #d4a72c; border-radius: 6px;
        padding: 10px 14px; margin: 14px 0; font-size: 0.9rem; }
.err { background: #ffebe9; border: 1px solid #cf222e; border-radius: 6px;
       padding: 10px 14px; margin: 14px 0; font-size: 0.9rem; }
.ok { background: #dafbe1; border: 1px solid #1a7f37; border-radius: 6px;
      padding: 10px 14px; margin: 14px 0; font-size: 0.9rem; }
table { width: 100%; border-collapse: collapse; margin-top: 10px; }
td, th { text-align: left; padding: 8px 6px; border-bottom: 1px solid #eaeef2; font-size: 0.92rem; }
.status-set { color: #1a7f37; font-weight: 600; }
.status-missing { color: #cf222e; font-weight: 600; }
.stage { padding: 8px 12px; margin: 6px 0; border-radius: 6px; background: #eaeef2; }
.stage.active { background: #ddf4ff; border-left: 4px solid #0969da; font-weight: 600; }
.stage.done { background: #dafbe1; color: #1a7f37; }
.bigcost { font-size: 1.6rem; font-weight: 700; }
.bar { height: 10px; background: #eaeef2; border-radius: 5px; overflow: hidden; margin-top: 6px; }
.bar > div { height: 100%; background: #1f7a36; width: 0%; transition: width .3s; }
"""


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def _shell(title: str, body: str) -> str:
    """Wrap page body in the common HTML shell with nav and styles."""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} — Landscape Scanner</title>
<style>{_STYLE}</style>
</head><body>{_NAV}<div class="wrap">{body}</div></body></html>"""


# ---------------------------------------------------------------------------
# Run screen
# ---------------------------------------------------------------------------

def run_page(missing_keys: list[str], running: bool, error: str = "") -> str:
    """The form for starting a scan."""
    warning = f'<div class="err">{_esc(error)}</div>' if error else ""
    if missing_keys:
        names = ", ".join(missing_keys)
        warning += (
            f'<div class="warn">Missing required key(s): <strong>{_esc(names)}</strong>. '
            f'A live run needs these — add them on the <a href="/keys">Keys</a> screen. '
            f'(The tool will not silently fake results.)</div>'
        )
    running_note = (
        '<div class="warn">A scan is already running — see '
        '<a href="/progress">Progress</a>.</div>'
        if running else ""
    )
    disabled = "disabled" if (missing_keys or running) else ""

    return _shell(
        "Run",
        f"""
<h1>Scan a landscape</h1>
<p class="hint">Ask a field-scan question in plain English. The tool finds the
entities, profiles the most relevant ones, and verifies every claim against
real sources.</p>
{warning}{running_note}
<form method="post" action="/run">
  <label for="question">Research question</label>
  <textarea id="question" name="question" required
    placeholder="e.g. entrepreneurship programmes, initiatives and interventions, current and past, in Kenya"></textarea>
  <div class="hint">Be specific about scope. Include "past" or "current" if it matters.</div>

  <label for="geography">Geography (optional, comma-separated)</label>
  <input type="text" id="geography" name="geography" placeholder="Kenya">

  <div class="row">
    <div>
      <label for="budget">Budget cap (USD)</label>
      <input type="number" id="budget" name="budget" value="2.0" min="0.25" step="0.25">
      <div class="hint">Hard stop. The run halts (resumably) if it's reached.</div>
    </div>
    <div>
      <label for="shortlist">Entities to deep-profile</label>
      <input type="number" id="shortlist" name="shortlist" value="25" min="5" max="60">
      <div class="hint">More = deeper coverage, higher cost.</div>
    </div>
  </div>

  <label for="profile">Model profile</label>
  <select id="profile" name="profile">
    <option value="economy">Economy — cheap extraction + Claude verification (recommended)</option>
    <option value="quality">Quality — Claude throughout (more expensive)</option>
  </select>

  <button type="submit" {disabled}>Start scan</button>
</form>
""",
    )


# ---------------------------------------------------------------------------
# Progress screen
# ---------------------------------------------------------------------------

def progress_page(status: RunStatus | None) -> str:
    """Live progress, refreshed by polling /api/status."""
    if status is None:
        return _shell(
            "Progress",
            '<h1>Progress</h1><p>No scan has started yet. '
            '<a href="/">Start one.</a></p>',
        )

    stages_html = "".join(
        f'<div class="stage" id="stage-{stage}">{_esc(STAGE_LABELS[stage])}</div>'
        for stage in STAGE_ORDER
    )

    # The poller updates stage highlighting, cost, the profile bar, and
    # redirects to results when done. Vanilla JS, no dependencies.
    script = """
<script>
const STAGES = %s;
async function poll() {
  let r = await fetch('/api/status');
  let s = await r.json();
  if (s.state === 'none') return;
  document.getElementById('detail').textContent = s.detail || '';
  document.getElementById('cost').textContent = '$' + (s.spent_usd||0).toFixed(2)
      + ' / $' + (s.budget_usd||0).toFixed(2);
  // Stage highlighting
  let idx = STAGES.indexOf(s.stage);
  STAGES.forEach((st, i) => {
    let el = document.getElementById('stage-' + st);
    el.className = 'stage' + (i < idx ? ' done' : (i === idx ? ' active' : ''));
  });
  // Profile progress bar
  if (s.shortlist_total > 0) {
    let pct = Math.round(100 * s.profiled / s.shortlist_total);
    document.getElementById('barfill').style.width = pct + '%%';
    document.getElementById('barlabel').textContent =
        s.profiled + ' / ' + s.shortlist_total + ' entities profiled';
  }
  if (s.state === 'done') { window.location = '/results'; return; }
  if (s.state === 'error') {
    document.getElementById('detail').innerHTML =
        '<div class="err">Error: ' + (s.error||'') + '</div>'; return;
  }
  if (s.state === 'halted') {
    document.getElementById('detail').innerHTML =
        '<div class="warn">Halted (budget reached): ' + (s.error||'') +
        ' — raise the budget and re-run the same question to resume.</div>'; return;
  }
  setTimeout(poll, 1500);
}
poll();
</script>
""" % str(STAGE_ORDER)

    return _shell(
        "Progress",
        f"""
<h1>Scanning…</h1>
<p class="hint">{_esc(status.question)}</p>
<div class="card">
  {stages_html}
  <div class="bar"><div id="barfill"></div></div>
  <div class="hint" id="barlabel"></div>
</div>
<div class="card">
  <div>Live cost: <span class="bigcost" id="cost">$0.00</span></div>
  <div class="hint" id="detail"></div>
</div>
{script}
""",
    )


# ---------------------------------------------------------------------------
# Results screen
# ---------------------------------------------------------------------------

def results_page(report: Report) -> str:
    """Render the finished report, reusing the standalone renderer's body."""
    # render_html returns a full document; we lift its <body> contents so it
    # sits inside the app shell with the nav and a download button.
    full = render_html(report)
    start = full.find("<body>") + len("<body>")
    end = full.find("</body>")
    body_inner = full[start:end]
    # Drop the report's own .wrap div constraints by leaving its markup as-is;
    # it is self-styled inline, which coexists with the app shell fine.
    download = (
        '<div class="ok">Scan complete. '
        '<a href="/results.html"><strong>Download the shareable HTML report</strong></a> '
        '— one self-contained file you can email or publish.</div>'
    )
    return _shell("Results", download + body_inner)


# ---------------------------------------------------------------------------
# Keys screen
# ---------------------------------------------------------------------------

def keys_page(statuses: dict[str, bool], saved: str = "", error: str = "") -> str:
    """Show key status and a form to save one key at a time."""
    rows = ""
    for provider, ok in statuses.items():
        role = KNOWN_PROVIDERS[provider]["role"]
        env = KNOWN_PROVIDERS[provider]["env"]
        badge = (
            '<span class="status-set">set</span>'
            if ok else '<span class="status-missing">missing</span>'
        )
        rows += (
            f"<tr><td><strong>{_esc(provider)}</strong></td><td>{badge}</td>"
            f"<td>{_esc(role)}<br><span class='hint'>{_esc(env)}</span></td></tr>"
        )

    options = "".join(
        f'<option value="{_esc(p)}">{_esc(p)}</option>' for p in KNOWN_PROVIDERS
    )
    notice = ""
    if saved:
        notice = f'<div class="ok">Saved key for <strong>{_esc(saved)}</strong> to local .env.</div>'
    if error:
        notice = f'<div class="err">{_esc(error)}</div>'

    return _shell(
        "Keys",
        f"""
<h1>API keys</h1>
<p class="hint">Keys are read from your environment or a local <code>.env</code>
file and never leave this machine. Values are never displayed back to you.</p>
{notice}
<div class="card">
  <table>
    <tr><th>Provider</th><th>Status</th><th>Role</th></tr>
    {rows}
  </table>
</div>
<div class="card">
  <h3>Add or update a key</h3>
  <form method="post" action="/keys">
    <label for="provider">Provider</label>
    <select id="provider" name="provider">{options}</select>
    <label for="value">Key value</label>
    <input type="text" id="value" name="value" required
       placeholder="paste the key — saved locally, never shown again">
    <div class="hint">Written to <code>.env</code>, which is gitignored. The
    save is refused if <code>.env</code> isn't protected by .gitignore.</div>
    <button type="submit">Save key</button>
  </form>
</div>
""",
    )


def simple_message(title: str, message: str, link: str = "/", link_label: str = "Home") -> str:
    """A minimal one-message page (used for empty states)."""
    return _shell(
        title,
        f'<h1>{_esc(title)}</h1><p>{_esc(message)}</p>'
        f'<p><a href="{_esc(link)}">{_esc(link_label)}</a></p>',
    )

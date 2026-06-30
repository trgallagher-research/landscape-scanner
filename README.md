# Landscape Scanner

Ask a landscape question in plain English — *"entrepreneurship programmes,
initiatives and interventions, current and past, in Kenya"* — and get back a
**verification-grounded landscape report**: an executive overview you can read
in a minute, entity cards with key features at a glance, and a full audit
trail (every claim → the verbatim quote it came from → the source link) one
click down.

Built for researchers who want a fast, honest field scan without the cost of a
consultancy or the hallucination risk of asking a chatbot.

## What makes it trustworthy

- **Anchor-constrained extraction.** The model never writes what it "knows"
  about an entity. It can only *select* from text spans actually present in
  scraped sources, so facts can't be imported from the model's imagination.
- **Verbatim verification.** Every decision-bearing claim must trace to an
  exact quote found in a real source, checked by deterministic string
  matching. Claims that can't be grounded are shown as *unverified* — never
  hidden, never dressed up as fact.
- **Quarantine, not deletion.** Entities whose existence can't be confirmed
  are kept and clearly flagged, so you see the engine's uncertainty instead
  of a silently cleaned-up list.
- **No silent fakery.** A live run refuses to start if a required API key is
  missing. Every report carries a provider manifest stating exactly which
  search engines and models were used.

## What makes it cheap (~$0.50–$2 per run)

A funnel: broad discovery across multiple search indexes, a cheap triage pass
over snippets, then deep profiling (scrape + verify) of only the top ~20–25
entities. The long tail stays in the report as one-line entries. Inexpensive
models handle high-volume extraction; verification stays on a trusted model.
A live cost meter (real token usage, not estimates) enforces the budget you
set — runs halt resumably, never overspend.

## Quickstart

```bash
pip install -e ".[app,pdf]"      # engine + local web UI + PDF scraping
scanner ui                       # open http://127.0.0.1:8000
```

In the browser: add your API keys on the **Keys** screen (saved to a local,
gitignored `.env`), type a question on the **Run** screen, watch the live
cost on **Progress**, then browse **Results** and download a shareable
single-file HTML report.

Prefer the command line?

```bash
scanner keys                                   # show which keys are set
scanner run --question "entrepreneurship programmes, current and past, in Kenya" \
            --geography Kenya --budget 2.0 --out report.html
```

## Security & sharing

This repository is public, but **it contains no secrets**. Your API keys live
only in a local `.env` file (gitignored) or environment variables on the
machine that runs the tool. The web UI shows each key as *set* or *missing* —
it never displays or transmits the key value.

Two things are worth keeping straight:

- **Exposed** (the key value leaks) — protected. The UI never reveals keys,
  and they are never committed.
- **Usable** (someone makes the app *spend* your keys without seeing them) —
  this is the risk to manage. The app has **no login**, so anyone who can
  reach a running instance can run scans that spend your credits, even though
  they never see the key itself.

The design therefore keeps the app **local-first**:

- `scanner ui` binds to `127.0.0.1` (localhost) only. It is not on the
  network and not on the internet; only the machine running it can reach it.
  The `http://127.0.0.1:8000` address always means "this computer" — it is
  not a shareable link.
- **Share results, not access.** Hand colleagues the downloaded single-file
  HTML report (open in any browser, email-able). It carries the findings and
  the full audit trail, and spends none of your credits when they open it.
- **If a colleague wants to run their own scans**, they clone this public
  repo and use *their own* keys on *their own* machine. Nobody spends anyone
  else's credits.

Do **not** bind `scanner ui` to `0.0.0.0` or port-forward it to the internet
unless you add authentication first — that would let anyone who finds it
spend your keys.

**Two run modes, two trust models.** Everything above describes `scanner ui` —
the single-user local interface, which has no login and therefore stays bound to
localhost. There is also `scanner serve-api`: a **token-authenticated** JSON API
built to be exposed safely to a web front-end. It validates a bearer token on
every request, renders no HTML, and never mounts the no-auth UI — and `serve-api`
refuses to bind a non-local host unless `SCANNER_SERVICE_TOKEN` is set. See
*Running as a hosted API* below.

## Running the local UI on a home server (e.g. an Intel NUC) over SSH

You can run the scanner on an always-on box you SSH into, and reach the UI
from your laptop **without exposing it to the network** — by keeping it bound
to localhost on the server and tunnelling over SSH. The trust boundary stays
exactly "who can SSH into the box".

**1. One-time setup on the NUC** (assumes Linux; see `deploy/setup-nuc.sh`):

```bash
ssh you@nuc
git clone https://github.com/trgallagher-research/landscape-scanner.git
cd landscape-scanner
bash deploy/setup-nuc.sh          # makes a venv and installs the app
```

**2. Add your keys on the NUC** (kept in its local, gitignored `.env`):

```bash
nano .env        # add SERPER_API_KEY=... etc. (see the Keys section)
```

**3. Run it, bound to localhost only:**

```bash
.venv/bin/scanner ui --host 127.0.0.1 --port 8000
```

To keep it running after you log out, install it as a service (auto-starts on
boot, restarts on crash) — see `deploy/landscape-scanner.service`:

```bash
# edit the User= and WorkingDirectory= lines first, then:
sudo cp deploy/landscape-scanner.service /etc/systemd/system/
sudo systemctl enable --now landscape-scanner
```

**4. From your laptop, open an SSH tunnel and browse it:**

```bash
ssh -N -L 8000:127.0.0.1:8000 you@nuc
# leave that running, then open http://localhost:8000 in your laptop's browser
```

The UI now appears in your laptop's browser, but the only way in is through
your authenticated SSH connection — nobody else on the network or the
internet can reach it or spend your keys. (On Windows, the same
`ssh -N -L ...` command works in PowerShell.)

## Running as a hosted API (web front-end)

To drive the scanner from a web app — multiple users, runs you start and poll
remotely — run the authenticated JSON API instead of the local UI. This is how
the engine is deployed in production today: it sits on the always-on box and a
separate, login-gated website calls it as a proxy.

**1. Run the API** (`scanner serve-api`) with a bearer token. It is multi-run
(keyed by run id), binds `127.0.0.1`, and renders no HTML:

```bash
SCANNER_SERVICE_TOKEN=$(openssl rand -hex 32) \
  .venv/bin/scanner serve-api --host 127.0.0.1 --port 8000
```

As a reboot-safe service, use `deploy/scanner-api.service` (the token is supplied
via an `EnvironmentFile`, kept out of git):

```bash
sudo cp deploy/scanner-api.service /etc/systemd/system/
sudo systemctl enable --now scanner-api
```

**2. Expose it without opening a port** — put it behind a **Cloudflare Tunnel**
(`cloudflared` connects out from the box, so nothing is bound to the internet),
fronted by **Cloudflare Access** with a **service token**, so only your web app
can reach it. Each request then carries *both* the Access service-token headers
and the app bearer — two independent layers.

**3. The web app** is a thin, login-gated front end that calls the API, stores
reports privately per user, and never exposes the scanner directly. The optional
per-request `keys` field lets each user bring their own provider keys; without it
the engine uses its own `.env`. See `docs/how-it-works.md` for the full
end-to-end architecture and reliability model.

## Status

Core engine, local web UI, the hosted JSON API (`serve-api`), and shareable HTML
reports are built and tested (offline test suite). The engine is deployed in
production behind a Cloudflare Tunnel + Access, driven by a login-gated web front
end. Calibration against a hand-built gold set is the remaining step before fully
trusting numbers on a new domain.

## Keys

API keys live in a local `.env` file (gitignored) or environment variables —
never in code, never committed. Search: `SERPER_API_KEY`, `BRAVE_API_KEY`.
Models: `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`.

## License

MIT

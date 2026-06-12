"""The local web app: a friendly front door to the scanner.

Four screens, served by FastAPI:
  * Run    — type a question, set geography/budget/profile, start a scan
  * Progress — live stage tracker and running cost while a scan executes
  * Results  — executive overview + entity cards with claim-level drill-down
  * Keys     — see which provider keys are set; paste/save new ones locally

Run it with:  scanner ui   (or: uvicorn scanner.app.server:app)
The app binds to localhost only — keys never leave the machine.
"""

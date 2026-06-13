#!/usr/bin/env bash
#
# One-time setup for running Landscape Scanner on a Linux home server
# (e.g. an Intel NUC) that you SSH into.
#
# Run it from the repository root:
#     bash deploy/setup-nuc.sh
#
# It creates a virtual environment and installs the app (web UI + PDF
# scraping). It does NOT touch your keys — add those to a local .env file
# afterwards (see the README "Keys" section). It does NOT open any network
# port; you run the app bound to localhost and reach it over SSH.

set -euo pipefail

# Move to the repository root (the directory above this script's folder),
# so the script works regardless of where it is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

echo "==> Setting up Landscape Scanner in: ${REPO_ROOT}"

# Pick a Python interpreter (prefer python3).
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "ERROR: no python3/python found on PATH. Install Python 3.10+ first." >&2
    exit 1
fi

echo "==> Using interpreter: $(${PYTHON} --version 2>&1)"

# Create the virtual environment if it does not already exist.
if [ ! -d ".venv" ]; then
    echo "==> Creating virtual environment (.venv)"
    "${PYTHON}" -m venv .venv
else
    echo "==> Reusing existing virtual environment (.venv)"
fi

# Install the package with the web UI and PDF extras.
echo "==> Installing the app and dependencies (this can take a minute)"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[app,pdf]"

# Confirm the console script is available.
if [ -x ".venv/bin/scanner" ]; then
    echo "==> Installed. The 'scanner' command is at .venv/bin/scanner"
else
    echo "WARNING: .venv/bin/scanner not found; you can still run 'python -m scanner.cli'." >&2
fi

cat <<'NEXT'

Setup complete. Next steps:

  1. Add your API keys to a local .env file in this directory, e.g.:
        SERPER_API_KEY=...
        BRAVE_API_KEY=...
        ANTHROPIC_API_KEY=...
        OPENROUTER_API_KEY=...
     (.env is gitignored and never leaves this machine.)

  2. Run the UI, bound to localhost only:
        .venv/bin/scanner ui --host 127.0.0.1 --port 8000

  3. From your laptop, tunnel in over SSH and open the browser:
        ssh -N -L 8000:127.0.0.1:8000 you@this-server
        # then visit http://localhost:8000 on your laptop

  To run it permanently as a service, see deploy/landscape-scanner.service.
NEXT

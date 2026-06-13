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

# Pick a Python interpreter. The app requires Python 3.10+ (it uses
# `str | None` unions and builtin generics). Prefer the newest available,
# and fail clearly if only an older one is present.
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1; then
        version="$("${candidate}" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
        major="${version%.*}"
        minor="${version#*.}"
        if [ "${major}" = "3" ] && [ "${minor}" -ge 10 ] 2>/dev/null; then
            PYTHON="${candidate}"
            break
        fi
    fi
done

if [ -z "${PYTHON}" ]; then
    echo "ERROR: need Python 3.10 or newer, but none was found on PATH." >&2
    echo "" >&2
    echo "On Ubuntu 20.04 (which ships Python 3.8), install a newer Python:" >&2
    echo "    sudo apt update && sudo apt install -y software-properties-common" >&2
    echo "    sudo add-apt-repository -y ppa:deadsnakes/ppa" >&2
    echo "    sudo apt update && sudo apt install -y python3.11 python3.11-venv" >&2
    echo "Then re-run this script." >&2
    exit 1
fi

echo "==> Using interpreter: $(${PYTHON} --version 2>&1)  (${PYTHON})"

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

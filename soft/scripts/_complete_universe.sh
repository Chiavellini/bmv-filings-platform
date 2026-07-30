#!/bin/bash
# Resolve soft/ from this script's own location so the checkout can move.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 1
echo "[chain] waiting for the cached-funds sweep to finish..."
while pgrep -f "build_all.py --cached-funds" >/dev/null 2>&1; do sleep 30; done
echo "[chain] cached sweep done. Fetching filings for uncached companies (network, slow)..."
.venv/bin/python -u scripts/fetch_xbrl_batch.py 2>&1
echo "[chain] fetch done. Building the full universe with the newly-cached filings..."
.venv/bin/python -u scripts/build_all.py --cached-funds 2>&1
echo "[chain] COMPLETE."

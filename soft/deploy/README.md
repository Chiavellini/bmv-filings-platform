# Live daily refresh — operating model & install

The workbook has two data clocks:

| clock | data | source | refresh |
|---|---|---|---|
| **daily** | price → market cap, EV, all multiples (live Excel formulas) | Yahoo Finance | `scripts/refresh_daily.py` |
| **quarterly** | revenue, book value, shares, EBITDA, … | cached BMV-XBRL | `scripts/fetch_xbrl_batch.py` + `scripts/build_all.py` when a new filing lands |

Re-running the whole pipeline daily is wasted work — fundamentals only change ~4×/year. The daily
refresh reuses the cached XBRL and re-pulls **only** market data, then re-emits each workbook through
the same math gate.

## The two commands

```bash
cd soft

# DAILY — fast; cached fundamentals + fresh prices. Idempotent.
python3 scripts/refresh_daily.py
#   -> re-prices every built workbook; writes outputs/_scorecard/refresh_<date>.md

# QUARTERLY — only when new filings are published (run by hand).
python3 scripts/fetch_xbrl_batch.py        # pull any new quarter into data/reports/*/xbrl
python3 scripts/build_all.py --cached-funds # full rebuild + scorecard
```

`refresh_daily.py` forces the market-data cache to re-fetch by setting `SOFT_MARKET_TTL_HOURS=0`.
Outside the refresh, a normal build reuses a cached quote while it is younger than
`SOFT_MARKET_TTL_HOURS` (default **18h**), so ad-hoc rebuilds during the day don't hammer Yahoo but
still pick up a fresh quote each morning.

## Install the scheduler (macOS launchd)

The agent runs the daily refresh every weekday at 16:00 local (after the BMV close).

1. **Edit every `/ABSOLUTE/PATH/TO/SOFT` placeholder** in
   `deploy/com.soft.refresh.plist` after creating `soft/.venv`. The checked-in
   file is deliberately a template and cannot accidentally start from a new
   checkout.

2. **Install & load:**
   ```bash
   cp deploy/com.soft.refresh.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.soft.refresh.plist
   ```

3. **Verify / run once now:**
   ```bash
   launchctl list | grep com.soft.refresh
   launchctl start com.soft.refresh          # trigger immediately
   tail -f outputs/_scorecard/_refresh_launchd.log
   ```

4. **Uninstall:**
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.soft.refresh.plist
   rm ~/Library/LaunchAgents/com.soft.refresh.plist
   ```

Notes
- launchd runs a missed job at the next wake if the Mac was asleep at 16:00.
- Weekends fire too but simply show "price unchanged" (market closed) — harmless and idempotent.
- Keep the checkout and output directory on a disk that is mounted whenever
  the job fires.

The template targets the repo-local `soft/.venv`; it must not reuse a personal
environment from another project. If the checkout or output directory is on an
external volume, verify that the volume is mounted before the scheduled time.
For the exported platform, the root native-Airflow DAG is the preferred
quarterly document-estate scheduler; this launchd template remains an optional
macOS-only daily price refresh.

## Auto-publish to GitHub Pages

A headless job **cannot** update a claude.ai Artifact (only a live Claude agent can). So the
auto-updateable public deliverable is served from GitHub Pages. `scripts/publish_pages.py` wraps the
freshest **dense** + **core** matrices into standalone pages, writes a landing `index.html`, and
commits and, only when explicitly enabled, pushes them to a separately
configured Pages repository.

- **Site working tree:** `deploy/site/` — its OWN git repo pointing at the Pages repo (ignored by the
  project repo). Set `SOFT_PAGES_REPO=owner/repository`; `SOFT_SITE_DIR` is optional.
- **Gate:** the checked-in plist sets `SOFT_PUBLISH=0`. Publishing is a separate
  opt-in deployment decision and must never be enabled merely by cloning this
  repository.

Publish manually:
```bash
python3 scripts/publish_pages.py            # assemble + commit + push
python3 scripts/publish_pages.py --no-push  # assemble + commit only (dry, no network)
```

One-time setup on a fresh machine, after selecting a separate Pages repository:
```bash
export SOFT_PAGES_REPO="OWNER/REPOSITORY"
gh repo create "$SOFT_PAGES_REPO" --public -d "Soft coverage — daily BMV valuation matrix"
git init deploy/site
git -C deploy/site remote add origin "https://github.com/${SOFT_PAGES_REPO}.git"
python3 scripts/publish_pages.py --no-push          # populate deploy/site
git -C deploy/site add -A && git -C deploy/site commit -m "initial" && git -C deploy/site push -u origin main
gh api -X POST "repos/${SOFT_PAGES_REPO}/pages" -f 'source[branch]=main' -f 'source[path]=/'
```

# earnings/ — BMV quarterly-report event study

Tests whether BMV quarterly reports predict next-day stock moves, and whether
prices adjust *before* the filing (leakage). Phase A: 17 configured issuers,
XBRL quarters 2021-2T..2026-1T. Expanding the universe = editing
`configs/study.yaml` only.

Everything is offline against frozen local data except one ^MXX index fetch.

## Environment

The certified interpreter is Python **3.11.13**, recorded in `.python-version`.
Create an environment inside this subproject so it cannot accidentally inherit
the root package's Python 3.13 environment or another installation named `src`:

```bash
cd earnings
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pytest -m "not network and not model"
```

## Data lineage

Two modes, both rooted in `earnlib/bootstrap.py`:

- **Default / `EARNINGS_V2=1`** — self-contained when the separately
  transferred frozen data bundle is attached: every runtime input then lives
  under `earnings/`. The large `data/` inputs are deliberately not stored in
  Git. This mode reproduces the certified v2 record.
- **`EARNINGS_V3=1` (current baseline)** — shared-infra mode: XBRL facts come
  from the monorepo document estate view
  (`data/document_estate/views/reports/<slug>/xbrl/`, read-only, filtered to
  soft-project symlink targets and pinned to `configs/facts_vintage.csv`), and
  the `src` package resolves to the ROOT extraction engine instead of the
  vendored alpha-go copy. Certified equivalent by `scripts/parity_check.py`
  (the separately transferred research bundle contains the dated parity
  record). v3 additionally applies the W2 timing correction and writes
  `*_v3` artifacts / `results_v3/` into that external research bundle so v2
  outputs stay frozen.

| input | location inside earnings/ |
|---|---|
| filing timestamps (minute-level) | external bundle materialized at `vendor/alpha-go/data/bmv/archive_index.html` for the frozen v2 path |
| quarterly metrics | `data/soft/data/reports/<slug>/xbrl/*_facts.json` via vendored `extract_from_xbrl` |
| MD&A narrative (sentiment refinement) | `data/soft/data/reports/<slug>/xbrl/*_mdna.html` |
| historical pdf-md extraction | `data/legacy_reports/`, `data/legacy_outputs/`, `data/legacy_configs/` |
| daily OHLCV (6y, 200 symbols) | frozen snapshot in `data/snapshots/` (source cache mirrored at `data/soft/.cache/market_data/`) |
| market index | ^MXX daily bars (one-time fetch, frozen in the snapshot dir) |
| alpha-go `src` modules | `vendor/alpha-go/src/` (committed frozen fork) |
| alpha-go catalog and raw snapshots | separately transferred data bundle under `vendor/alpha-go/data/` |

Desk-model estimate sources, evidentiary records, generated results, and the
frozen heavy-data bundle are transferred separately from the code repository
with their own checksum/timestamp manifest. Their built estimate artifact is
`outputs/estimates_pit.parquet` inside that external bundle.

## Run

```bash
cd earnings
PY=.venv/bin/python
$PY scripts/build_prices.py --snapshot-only   # freeze soft's price cache FIRST
$PY scripts/build_events.py                   # filings -> events.parquet
$PY scripts/build_metrics.py                  # XBRL facts -> metrics.parquet
$PY scripts/build_prices.py --fetch-index     # ^MXX (once) + prices.parquet
$PY scripts/compute_surprises.py              # SUE + composite S
$PY scripts/run_event_study.py                # development-sample tables
$PY scripts/run_validation.py                 # quant-audit gates 1-4
$PY scripts/run_event_study.py --holdout      # gate 5: ONE-SHOT, run last
$PY scripts/make_report.py                    # outputs/REPORT.md
```

For v3, first configure the root project's `estate.json` (or export
`PDFS_DOCUMENT_ESTATE`) to point at the separately transferred estate, then run:

```bash
EARNINGS_V3=1 .venv/bin/python scripts/build_metrics.py
```

`configs/study.yaml` pre-commits every parameter and the success criteria;
changes after the first analysis run are researcher degrees of freedom and must
be logged in the report. Holdout quarters are sacred: excluded from every
development run, evaluated once.

Key design notes: t0 = first session opening after the filing timestamp;
Q4 XBRL filings lagging > 60 days are annual-report artifacts, excluded as
events; SUE priors come from the same filing (point-in-time restatements);
trailing sigma is availability-masked by filing timestamps.

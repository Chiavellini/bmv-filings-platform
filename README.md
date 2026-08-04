# BMV filings platform

A Mexican public-company document pipeline with two root modules and three
application consumers.

```text
external scheduler
       |
       v
1. quarterly acquisition engine  src/acquisition/
               |
               v
2. shared document estate        data/document_estate/
               +------> durable outbox receipts  src/consumers/
               |                    |
               |                    +--> verified PDF/HTML/text -> Markdown
               |                    +--> batched Alpha Go projection
               |
               v
3. quarterly extraction engine   src/parse/ + src/extract/ + src/excel/
               |
               +------> Alpha Go   document search & intelligence
               +------> Soft       coverage matrix & pricing
               +------> Earnings   earnings event study
```

Issuer identity, source configuration, and durable originals belong to the root
modules. Application-specific search, valuation, event-study, and interface
behavior stay inside the three consumers.

## One command per module

Each module has its own declared interpreter. Use the explicit version shown
below instead of assuming that another computer's default `python3` is
compatible.

| Module | Environment | Verified entry command |
|---|---|---|
| Acquisition | `.venv` | `.venv/bin/refresh-quarterly-estate audit --json` |
| Estate | `.venv` | `.venv/bin/python scripts/check_estate_connection.py` |
| Outbox delivery | `.venv` | `.venv/bin/process-estate-outbox status --database data/document_estate/catalog.db --json` |
| Extractor | `.venv` | `.venv/bin/python scripts/build_segments.py inputs/<company>.md --analyst-metrics <request.xlsx>` |
| Alpha Go | `alpha-go/.venv312` | `cd alpha-go && .venv312/bin/python -m streamlit run app/streamlit_app.py` |
| Soft | `soft/.venv` (Python 3.11.13) | `cd soft && .venv/bin/python -m pytest -q -m "not network and not model"` |
| Earnings | `earnings/.venv` (Python 3.11.13) | `cd earnings && .venv/bin/python -m pytest -q -m "not network and not model"` |

`audit`, `plan`, `status`, and `check_estate_connection.py` are strictly
read-only: no source discovery, no network, no estate writes.

Install the root package once so the console scripts exist:

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
```

## Connect a local document estate

[`estate.json`](estate.json) is the single user-facing bridge between the code
and a document-estate bundle. Root, Alpha Go, Soft, and Earnings all resolve the
catalog and compatibility view through it. A portable bundle looks like:

```text
estate-v1/
  catalog.db
  blobs/
  indexes/alpha_go.db
  views/reports/
  manifest.json
```

For a transferred USB, keep the checked-in bridge relative and create an
untracked `.env` from `.env.example`:

```bash
cp .env.example .env
# Set the destination computer's actual values in .env:
# PDFS_ESTATE_MOUNT_ROOT=<mounted USB root>
# PDFS_DOCUMENT_ESTATE=<mounted USB root>/bmv-estate-v1
# PDFS_ESTATE_ID=<ID from .bmv-estate-volume.json>
set -a
. ./.env
set +a
```

Every other key has a portable default and is containment-checked against
`estate_root`. `PDFS_ESTATE_BRIDGE` selects a different bridge file and
`PDFS_DOCUMENT_ESTATE` overrides the relative development root. Mount-aware
automation must set all three Estate variables; it refuses a missing mount,
missing sentinel, or wrong Estate ID before opening SQLite.

Verify a connection, including a relocated bundle, without writing anything:

```bash
.venv/bin/python scripts/check_estate_connection.py
.venv/bin/python scripts/check_portable_estate.py \
  --estate-root "$PDFS_DOCUMENT_ESTATE" \
  --mount-root "$PDFS_ESTATE_MOUNT_ROOT" \
  --estate-id "$PDFS_ESTATE_ID"
```

That gate is sufficient for acquisition, extraction, onboarding, and workbook
generation. After Alpha's semantic index has been built and certified, repeat
it with `--require-alpha-index` before enabling Alpha consumers.

The repository works with **no estate attached** — the bridge resolves paths,
reports what is missing, and estate-dependent tests skip rather than fail.

## Automated quarterly refresh

The portable native Airflow 3 bundle is in `deploy/airflow/`. Docker is not
required. Airflow runs the same fleet-level `refresh-quarterly-estate` CLI used
locally; it does not create a different downloader or DAG for every company.
Airflow keeps scheduling state in its own local database (or PostgreSQL for an
always-on installation), while the document catalog and downloaded objects
remain on the user's external disk. New documents flow through the durable
outbox into verified Markdown and the estate-wide Alpha Go search index.

On the second computer, create a Python 3.13 Airflow virtual environment, then
connect its external estate:

```bash
.airflow-venv/bin/python scripts/configure_airflow.py \
  --estate /absolute/path/on/the/external/disk/document-estate
set -a
. deploy/airflow/native/.env
set +a
.airflow-venv/bin/airflow db migrate
.airflow-venv/bin/airflow pools set \
  estate_writer 1 "Single SQLite estate mutation slot"
# Disposable import/UI trial only; always-on deployment uses the documented
# api-server, scheduler, and dag-processor services.
.airflow-venv/bin/airflow standalone
```

There are four DAGs: weekday read-only acquisition audit; six-hour strict
acquisition followed by Alpha drain; 15-minute outbox drain; and daily full
Alpha reconcile/hash audit. The three writers are paused on creation and refuse
to run until acquisition/Alpha gates and absolute Alpha target paths are
configured. Start with a scoped disposable-estate canary; do not unpause the
fleet schedule merely because the DAG files exist. Installation, PostgreSQL,
native-service, launchd, health, and migration details are in
[`docs/deployment/AIRFLOW_PORTABILITY.md`](docs/deployment/AIRFLOW_PORTABILITY.md).

## Tests

```bash
.venv/bin/python -m pytest -q -m "not network"
```

A fresh clone has no `data/reports/` corpus (15 GB, gitignored), so tests that
read filings skip. That is intended: a skip says "this needs data I do not have";
a failure would falsely say "this code is broken".

To prove the repository is actually deliverable — clone, isolated venv, declared
dependencies only, no estate present:

```bash
.venv/bin/python scripts/certify_clean_clone.py
```

The repository release workflow repeats the no-estate suite independently
under Python 3.13.9 for root, 3.12.13 for Alpha Go, and 3.11.13 for Soft and
Earnings. Native Airflow has a separate focused workflow; neither workflow uses
Docker or attaches the document estate.

## Status

The root acquisition module supports BMV XBRL, issuer-IR PDFs, official BMV
issuer-page PDFs, and explicit Wayback backfills, writing through the root estate
writer to a SQLite catalog and local content-addressed storage. It does **not**
implement root adapters for SEC EDGAR or uploads.

This boundary is not yet the only production path: Alpha Go, Soft, and Earnings
still contain transitional download, upload, parsing, indexing, and data-writing
entry points. Treat their estate-facing writers as legacy until consumer cutover
completes.

Fleet readiness is limited: **25 of 179** active issuers have an enabled primary
PDF source in `configs/issuers.yaml` (24 issuer-IR sources and one official BMV
issuer-page source), and five bindings have explicit root live-canary
evidence. BMV quarterly XBRL is configured for 177 issuers, but only 122 mappings
are locally verified. Listing dates, delisting dates, fiscal-year ends, and
filing-grace settings are not issuer-verified. The complete read-only primary-PDF
matrix is documented in
[the acquisition coverage runbook](docs/acquisition/PRIMARY_PDF_ACQUISITION.md).

A bounded per-consumer outbox dispatcher, a PDF/HTML/text-to-Markdown derivative
consumer, and an optional estate-wide Alpha Go projection consumer exist, with triggered
receipts, append-only attempt history, fenced heartbeat leases, bounded batches,
dead-letter replay, fail-closed draining, and broad idempotent reconciliation.
**Code availability is not proof that the live catalog has been migrated or the
worker schedule enabled.** Soft and Earnings have no equivalent consumers. See
[the Alpha automatic-sync contract](docs/ALPHA_GO_ESTATE_SYNC.md).

Do not schedule `sync --apply` for production. Deployment is gated on
primary-source and issuer-lifecycle enrichment, consumer writer cutover, a
controlled live outbox migration and worker rollout, portable estate paths, and a
tested database/object-storage backend contract. See
[the deployment guide](docs/DEPLOYMENT.md).

## Documentation

- [Quarterly acquisition engine](docs/ACQUISITION_ENGINE.md)
- [Primary-PDF coverage and source certification](docs/acquisition/PRIMARY_PDF_ACQUISITION.md)
- [Airflow and second-computer portability contract](docs/deployment/AIRFLOW_PORTABILITY.md)
- [Document estate](docs/DOCUMENT_ESTATE.md) — including the table-ownership map
- [Deployment and worker safety](docs/DEPLOYMENT.md)
- [Extraction pipeline deep-dive and documentation index](docs/README.md)
- [Known issues and deferred work](docs/KNOWN_ISSUES.md)
- [Sweep baseline, 2026-07-29](docs/sweep/BASELINE_2026-07-29.md)

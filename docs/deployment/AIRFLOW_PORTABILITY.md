# Native Airflow portability

Status: deployment bundle and offline contracts are defined. Clean-checkout
installation, DAG import with the pinned Airflow runtime, service activation,
and a disposable-estate canary on the destination computer remain mandatory.
Repository files alone do not mean that Airflow is installed or running.

Docker is **not required** for this deployment. The supported first path is a
normal Python 3.13 virtual environment on the other computer, with Airflow
running as native processes and the user-owned document estate mounted from
that computer's external disk.

The repository contains:

- `deploy/airflow/dags/quarterly_estate.py`: Airflow 3 DAG definitions;
- `src/deployment/airflow_task.py`: validated process boundary to the public
  acquisition and estate-consumer CLIs;
- `scripts/configure_airflow.py`: local environment and secret generator;
- `deploy/airflow/native/.env.example`: documented native settings;
- `deploy/airflow/native/systemd/bmv-airflow@.service`: optional Linux service
  template;
- `deploy/airflow/native/launchd/`: macOS launchd template (concrete plists are
  rendered by the configurator without embedding secrets);
- `scripts/run_airflow_native.sh`: path-validated native service launcher;
- `scripts/airflow_native_status.py`: read-only static/runtime health gate;
- `requirements/airflow-native.txt`: optional PostgreSQL driver pin; and
- `.github/workflows/airflow-native.yml`: GitHub validation for downloader and
  DAG changes.

## One fleet downloader, not one downloader per company

Airflow does not encode issuer-specific download logic. It executes one strict
fleet-level public CLI, then drains the durable outbox into Alpha Go:

```text
airflow-quarterly-estate sync
        |
        v
refresh-quarterly-estate sync --apply --json
        |
        +--> configs/issuers.yaml
        +--> shared BMV XBRL adapter
        +--> shared issuer-IR/BMV PDF adapters
        +--> mounted document estate
        |
        v
process-estate-outbox run --apply --enable-alpha-go --require-drained
        |
        +--> verified Markdown derivative
        +--> verified XBRL facts derivative
        +--> derivative publication verification/receipts
        +--> estate-wide Alpha manifest and index
```

Issuer differences remain declarative in `configs/issuers.yaml`. New adapter
work can continue after GitHub export without changing the Airflow topology.
There is one applied fleet task and one SQLite-writer slot, not 179 parallel
company tasks.

## Code, Airflow state, and estate are separate

| Store | Example location | Owner |
|---|---|---|
| Git checkout | `/opt/bmv-filings` or a user checkout | GitHub/release |
| Airflow metadata | local `AIRFLOW_HOME/airflow.db` for a trial; PostgreSQL for always-on use | Airflow |
| Document estate | `/Volumes/.../document-estate` or `/mnt/.../document-estate` | This project/user |

Airflow's metadata database stores schedules, DAG runs, and task status. It
must never be pointed at the estate's `catalog.db`. The acquisition task reaches
`catalog.db` through `PDFS_DOCUMENT_ESTATE`; it does not need an Airflow
Connection for the local SQLite file.

The current estate backend is SQLite plus a local filesystem. Therefore all
estate mutations must run on the one computer to which the external disk is
physically attached or locally mounted. Do not run several remote Airflow
workers against `catalog.db` over NFS, SMB, cloud sync, or an eventually
consistent filesystem.

## DAG policy

| DAG | Default schedule | Initial state | Mutation |
|---|---|---|---|
| `quarterly_estate_audit` | Weekdays, 07:00 Mexico City | enabled | none |
| `quarterly_estate_sync` | Every six hours at minute 17 | paused plus acquisition and Alpha gates | estate, then Alpha |
| `quarterly_estate_consume` | Every 15 minutes | paused plus Alpha gate | outbox/derivatives/Alpha |
| `quarterly_estate_alpha_reconcile` | Daily, 03:43 Mexico City | paused plus Alpha gate | full Alpha reconcile, then read-only hash/status gates |

All DAGs have `catchup=False` and `max_active_runs=1`. Every catalog/index
writer uses the one-slot `estate_writer` pool. Tasks default to two bounded,
exponentially delayed retries and retain execution timeouts. Acquisition also
has its own fenced singleton lease, so an Airflow retry cannot create two live
estate writers.

The sync task refuses to start unless `PDFS_AIRFLOW_SYNC_ENABLED=true`; every
Alpha task refuses unless `PDFS_AIRFLOW_ALPHA_ENABLED=true` and all Alpha root,
corpus, index, config, interpreter, and target-id settings are explicit,
absolute, and exist. Unpausing is not enough. The sync DAG is
`sync_fleet >> drain_alpha_outbox`, while the independent 15-minute consumer
DAG catches documents written by partial/legacy runs.

The drain deliberately uses the PR #1 public `run` command, so enabling Alpha
does not replace the root derivative consumers: PDF-to-Markdown, XBRL-to-facts,
and derivative-publication verification run in the same bounded delivery
contract. `PDFS_AIRFLOW_PDF_PARSER_VERSION` and
`PDFS_AIRFLOW_XBRL_PROCESSOR_VERSION` select their durable receipt generations;
`PDFS_AIRFLOW_ALPHA_TIMEOUT_SECONDS` bounds each isolated Alpha invocation.
Change a version only when its corresponding derivative contract changes.

Scheduled sync is strict by default. `PDFS_AIRFLOW_ALLOW_COVERAGE_GAPS=false`
omits the diagnostic opt-out, so readiness gaps, discovery/download failures,
or estate-write failures propagate a nonzero exit code to Airflow. A deliberate
gap-tolerant run cannot prove publication freshness.

Set `PDFS_AIRFLOW_ALERT_WEBHOOK` to an HTTPS endpoint before unattended use.
The final task failure callback sends only DAG/task/run/attempt identifiers;
secrets and document contents are excluded. With the setting blank, Airflow
still records failures, but there is no external alert and the native health
command reports not ready.

## Install on the second computer

### 1. Clone and create the native environment

Use Python 3.13. The checked-in installer creates the venv and uses Airflow's
official 3.3.0/Python 3.13 constraints:

```bash
git clone <github-repository-url> bmv-filings
cd bmv-filings
scripts/install_airflow_native.sh
```

Install Chromium only if a configured source needs the Playwright fallback:

```bash
scripts/install_airflow_native.sh --with-playwright
```

On Linux, the administrator may also need Playwright's documented operating
system dependencies. That is a host package installation, not a Docker
requirement.

### 2. Connect the external estate and render host services

The selected directory must already exist:

```bash
.airflow-venv/bin/python scripts/configure_airflow.py \
  --estate /absolute/path/on/external/disk/document-estate
```

This writes the untracked `deploy/airflow/native/.env`, creates local Airflow
state under `~/.local/state/bmv-filings-airflow`, generates Fernet/JWT secrets,
and leaves applied sync disabled.

On macOS, render the three concrete launchd agents at the same time. Rendering
does not load or start them:

```bash
.airflow-venv/bin/python scripts/configure_airflow.py \
  --estate /absolute/path/on/external/disk/document-estate \
  --launchd-output-dir /Users/your-name/Library/LaunchAgents
```

Load the environment in each service shell:

```bash
set -a
. deploy/airflow/native/.env
set +a
```

The generated Alpha settings are deliberately blank. Choose them explicitly;
the corpus path is a consequential choice. Pointing an existing 3,695-document
index at its current Alpha corpus should reconcile unchanged, whereas pointing
it at a new portable projection directory intentionally performs a full
reindex. Do not infer one from the other. A current-layout example is:

```text
PDFS_AIRFLOW_ALPHA_ENABLED="false"
PDFS_ALPHA_ROOT="/absolute/path/to/repository/alpha-go"
PDFS_ALPHA_CORPUS="/absolute/path/to/repository/alpha-go/data/corpus"
PDFS_ALPHA_INDEX="/absolute/path/to/estate/indexes/alpha_go.db"
PDFS_ALPHA_CONFIG="/absolute/path/to/repository/alpha-go/configs/alpha_go.yaml"
PDFS_ALPHA_PYTHON="/absolute/path/to/repository/alpha-go/.venv/bin/python"
PDFS_ALPHA_TARGET_ID="portable-estate-v1"
```

For a new portable corpus, select a dedicated directory such as
`<estate>/projections/alpha-go`, seed/certify it with a full reconcile, and
then retain that exact path. Do not use the estate root itself as the corpus.

### 3. Initialize Airflow

For a disposable first trial, the generated configuration uses Airflow's own
local SQLite metadata and `LocalExecutor`. Airflow 3.3 supports this combination
using SQLite WAL, but the official documentation still classifies SQLite as a
development backend. It is completely separate from the estate catalog and is
not the always-on configuration.

```bash
.airflow-venv/bin/airflow db migrate
.airflow-venv/bin/airflow pools set \
  estate_writer 1 "Single SQLite estate mutation slot"
.airflow-venv/bin/airflow dags list --local
.airflow-venv/bin/airflow dags list-import-errors --local
```

For only the disposable import/UI trial, start standalone:

```bash
.airflow-venv/bin/airflow standalone
```

Airflow prints or stores the generated admin password under `AIRFLOW_HOME`.
Open `http://127.0.0.1:8080`, inspect the enabled audit DAG, and trigger it.

### 4. Audit Alpha, then run the applied canary

Before enabling either write gate, run the read-only whole-estate Alpha audit
with the explicit paths selected above. It exits nonzero on missing families,
manifest drift, index drift, or hash mismatch:

```bash
.airflow-venv/bin/process-estate-outbox audit-alpha \
  --estate-root "${PDFS_DOCUMENT_ESTATE}" \
  --database "${PDFS_DOCUMENT_ESTATE}/catalog.db" \
  --alpha-root "${PDFS_ALPHA_ROOT}" \
  --alpha-corpus "${PDFS_ALPHA_CORPUS}" \
  --alpha-index "${PDFS_ALPHA_INDEX}" \
  --alpha-config "${PDFS_ALPHA_CONFIG}" \
  --alpha-target-id "${PDFS_ALPHA_TARGET_ID}" \
  --alpha-python "${PDFS_ALPHA_PYTHON}" --json
```

Stop Airflow, edit the local `.env`, and set:

```text
PDFS_AIRFLOW_ONLY="femsa"
PDFS_AIRFLOW_SYNC_ENABLED="true"
PDFS_AIRFLOW_ALPHA_ENABLED="true"
```

Reload the environment, start Airflow again, leave the sync DAG paused, and
manually trigger it. The run must perform strict acquisition and fully drain
the resulting derivative/Alpha receipts. Repeat the same canary and verify
idempotency. Before clearing `PDFS_AIRFLOW_ONLY`, manually drain any historical
backlog until `--require-drained` exits zero and run `audit-alpha` again.

After the canary is clean, clear the issuer scope and explicitly unpause all
four schedules:

```bash
.airflow-venv/bin/airflow dags unpause quarterly_estate_audit
.airflow-venv/bin/airflow dags unpause quarterly_estate_sync
.airflow-venv/bin/airflow dags unpause quarterly_estate_consume
.airflow-venv/bin/airflow dags unpause quarterly_estate_alpha_reconcile
```

## Always-on native service

For a durable installation, use PostgreSQL for **Airflow metadata**, while the
estate itself remains `/external/path/catalog.db` plus `/external/path/blobs`.
Regenerate the environment with the PostgreSQL URL:

```bash
.airflow-venv/bin/python scripts/configure_airflow.py \
  --estate /absolute/path/on/external/disk/document-estate \
  --metadata-url \
  'postgresql+psycopg2://airflow:replace-me@127.0.0.1/airflow' \
  --force

.airflow-venv/bin/python -m pip install \
  -r requirements/airflow-native.txt
```

When replacing a generated environment with `--force`, the configurator
preserves the existing Fernet and JWT secrets and refuses replacement if it
cannot parse them. It never rotates encryption material implicitly.

After loading the environment, migrate Airflow's PostgreSQL metadata and
recreate the one-slot pool. Run these native Airflow services under the target
operating system's supervisor:

```text
airflow api-server
airflow scheduler
airflow dag-processor
```

The checked-in systemd template documents the Linux shape. Its service user and
environment-file path are administrator-selected, while checkout and venv
locations come from `PDFS_PROJECT_ROOT` and `PDFS_AIRFLOW_VENV` in that file.

On macOS, bootstrap the three rendered agents only after database migration,
pool creation, Alpha audit, and the manual canary:

```bash
launchctl bootstrap "gui/$(id -u)" /Users/your-name/Library/LaunchAgents/com.bmv.airflow.api-server.plist
launchctl bootstrap "gui/$(id -u)" /Users/your-name/Library/LaunchAgents/com.bmv.airflow.scheduler.plist
launchctl bootstrap "gui/$(id -u)" /Users/your-name/Library/LaunchAgents/com.bmv.airflow.dag-processor.plist
```

Then use the read-only health gate. It verifies owner-only secrets, strict/write
gates, every Alpha path, the one-slot pool, DAG imports, scheduler job, native
services, all four DAGs present and unpaused, outbox status, and (on macOS) that the legacy `com.bmv.watch`
scheduler is no longer loaded:

```bash
.airflow-venv/bin/python scripts/airflow_native_status.py --json
```

Never leave the legacy watcher and the Airflow writer enabled together. Inspect
and deliberately retire the old launchd job only after the scoped Airflow
canary is certified; this repository does not unload it automatically.

The configurator never calls `launchctl`, `systemctl`, or an Airflow service.
Rendering files is not activation. The release gate still needs to certify the
pinned runtime, Playwright browser/host dependencies, PostgreSQL metadata,
external alert endpoint, and supervisor behavior on the destination host.

## New estate versus legacy estate

A new or disposable estate directory can be tested now. The first applied sync
initializes the owning schemas and writes new originals with bundle-relative
`blobs/...` keys.

The existing live legacy estate is a separate portability task. It currently
contains host-absolute compatibility paths and incomplete
`content_objects.object_key` coverage. Copying its live SQLite file directly is
not a safe export. A complete legacy export must:

1. fence writers and create a consistent SQLite snapshot;
2. migrate originals to bundle-relative object keys;
3. include every referenced blob and required compatibility artifact;
4. generate and verify a checksum manifest; and
5. pass SQLite integrity and relocated-path checks.

That legacy migration blocks calling the old data bundle portable; it does not
block moving the code to GitHub or testing Airflow against a new/disposable
estate on the other computer.

## Minimum export milestone

Before the first other-computer canary:

- GitHub CI passes the focused downloader/DAG tests;
- the native Python 3.13 environment installs from the documented pins;
- all four DAGs load and `airflow dags list-import-errors` is empty;
- the `estate_writer` pool has exactly one slot;
- the audit DAG succeeds against the selected external disk;
- the selected estate is backed up or explicitly disposable;
- the read-only Alpha audit has zero missing/stale/hash-drift families;
- one scoped strict sync, its post-sync drain, and its repeat succeed; and
- the native runtime health command reports `ok=true` with an HTTPS failure
  alert configured.

Full source coverage is a prerequisite for unpausing the strict fleet sync.
Legacy estate portability and destination-host service activation remain
separate acceptance work; neither is implied by the checked-in templates.

## Official references

- [Airflow 3.3.0 documentation](https://airflow.apache.org/docs/apache-airflow/stable/)
- [Airflow Task SDK](https://airflow.apache.org/docs/task-sdk/stable/)
- [Airflow production deployment](https://airflow.apache.org/docs/apache-airflow/stable/administration-and-deployment/production-deployment.html)
- [Airflow database backend setup](https://airflow.apache.org/docs/apache-airflow/stable/howto/set-up-database.html)
- [Airflow configuration reference](https://airflow.apache.org/docs/apache-airflow/stable/configurations-ref.html)

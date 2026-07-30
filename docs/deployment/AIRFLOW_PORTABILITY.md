# Native Airflow portability

Status: native release candidate validated locally on a disposable estate,
2026-07-30. Clean-checkout installation and service operation on the second
computer remain pending.

Docker is **not required** for this deployment. The supported first path is a
normal Python 3.13 virtual environment on the other computer, with Airflow
running as native processes and the user-owned document estate mounted from
that computer's external disk.

The repository contains:

- `deploy/airflow/dags/quarterly_estate.py`: Airflow 3 DAG definitions;
- `src/deployment/airflow_task.py`: validated process boundary to the public
  acquisition CLI;
- `scripts/configure_airflow.py`: local environment and secret generator;
- `deploy/airflow/native/.env.example`: documented native settings;
- `deploy/airflow/native/systemd/bmv-airflow@.service`: optional Linux service
  template;
- `requirements/airflow-native.txt`: optional PostgreSQL driver pin; and
- `.github/workflows/airflow-native.yml`: GitHub validation for downloader and
  DAG changes.

## One fleet downloader, not one downloader per company

Airflow does not encode issuer-specific download logic. It executes one
fleet-level public CLI:

```text
airflow-quarterly-estate sync
        |
        v
refresh-quarterly-estate sync --apply --allow-coverage-gaps --json
        |
        +--> configs/issuers.yaml
        +--> shared BMV XBRL adapter
        +--> shared issuer-IR/BMV PDF adapters
        +--> mounted document estate
        |
        v
process_estate_outbox.py run --apply
        |
        +--> root parsed/facts derivatives
        +--> optional Alpha Go shared-estate projection
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
| `quarterly_estate_sync` | Every six hours at minute 17 | paused plus environment write gate | estate plus downstream derivatives |

Both DAGs have `catchup=False` and `max_active_runs=1`. The applied task uses
the one-slot `estate_writer` pool, has no automatic retries, and times out
before the acquisition lease expires.

The sync task refuses to start unless
`PDFS_AIRFLOW_SYNC_ENABLED=true`. Unpausing it is not enough. This double gate
allows the repository and DAG to be exported safely while the first canary is
still pending on the other computer. The local disposable-estate canary and its
idempotent repeat have passed.

After a successful sync, a second task in the same one-slot `estate_writer`
pool drains the durable estate outbox in bounded batches. It runs the root
derivative consumers and fails visibly if a delivery is retryable/dead, returns
invalid machine output, or remains nonterminal at its configured batch ceiling.
After the final applied batch it runs the read-only status check, so delayed
retries and unpublished events cannot produce a false-green task. The downstream
task also requires `PDFS_AIRFLOW_SYNC_ENABLED=true`; it cannot become an
independent ungated writer. `PDFS_AIRFLOW_POST_SYNC_ENABLED=false` is an
emergency kill switch. The generated configuration leaves this post-sync step
enabled so an intentionally authorized sync refreshes both originals and their
root derivatives. `PDFS_AIRFLOW_PDF_PARSER_VERSION` and
`PDFS_AIRFLOW_XBRL_PROCESSOR_VERSION` are durable contract generations: bump
the corresponding value whenever parser behavior changes materially so the
outbox creates a new, auditable delivery generation.

Alpha Go projection remains independently disabled. When enabled, it uses the
Alpha-owned Python 3.12 environment while its corpus and SQLite index live under
the shared estate. This keeps Airflow native and avoids importing Alpha's
application package into the root/Airflow interpreter. Turning the Airflow
switch off explicitly disables prior Alpha subscription generations; it does
not leave undrainable receipts behind.

The initial sync includes `--allow-coverage-gaps`. Missing primary-PDF
configuration remains visible in the JSON report, but it does not prevent
configured XBRL/PDF sources from remaining fresh. Actual discovery, download,
validation, or estate-write failures still return nonzero and fail the Airflow
task.

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

### 2. Connect the external estate

The selected directory must already exist:

```bash
.airflow-venv/bin/python scripts/configure_airflow.py \
  --estate /absolute/path/on/external/disk/document-estate
```

This writes the untracked `deploy/airflow/native/.env`, creates local Airflow
state under `~/.local/state/bmv-filings-airflow`, generates Fernet/JWT secrets,
and leaves applied sync disabled.

Load the environment in each service shell:

```bash
set -a
. deploy/airflow/native/.env
set +a
```

### 3. Initialize Airflow

For the first exported-code trial, the generated configuration uses Airflow's
own local SQLite metadata and `LocalExecutor`. This database is completely
separate from the estate catalog.

```bash
.airflow-venv/bin/airflow db migrate
.airflow-venv/bin/airflow pools set \
  estate_writer 1 "Single SQLite estate mutation slot"
.airflow-venv/bin/airflow dags list --local
.airflow-venv/bin/airflow dags list-import-errors --local
```

Start the native trial:

```bash
.airflow-venv/bin/airflow standalone
```

Airflow prints or stores the generated admin password under `AIRFLOW_HOME`.
Open `http://127.0.0.1:8080`, inspect the enabled audit DAG, and trigger it.

### 4. Run the applied canary

Stop Airflow, edit the local `.env`, and set:

```text
PDFS_AIRFLOW_ONLY="femsa"
PDFS_AIRFLOW_SYNC_ENABLED="true"
```

Reload the environment, start Airflow again, leave the sync DAG paused, and
manually trigger it. The run must finish both `sync_fleet` and
`process_estate_outbox`. Repeat the same canary and verify that acquisition and
delivery claim no new work. Only then clear `PDFS_AIRFLOW_ONLY` and decide
whether to unpause the schedule.

### 5. Opt in to Alpha Go projection

Install Alpha's separately declared Python 3.12 environment:

```bash
python3.12 -m venv alpha-go/.venv312
alpha-go/.venv312/bin/pip install -r alpha-go/requirements.txt
```

Confirm or edit the generated `PDFS_AIRFLOW_ALPHA_ROOT`,
`PDFS_AIRFLOW_ALPHA_PYTHON`, `PDFS_AIRFLOW_ALPHA_CONFIG`,
`PDFS_AIRFLOW_ALPHA_CORPUS`, and `PDFS_AIRFLOW_ALPHA_INDEX` paths. The default
index path is `<estate>/indexes/alpha_go.db`, which is also the shared-estate
dashboard target. Then set:

```text
PDFS_AIRFLOW_ALPHA_ENABLED="true"
```

Leave `PDFS_AIRFLOW_ALPHA_TARGET_ID` stable for the life of that target. Change
it deliberately only when replaying all projection receipts for a replacement
generation.

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

After loading the environment, migrate Airflow's PostgreSQL metadata and
recreate the one-slot pool. Run these native Airflow services under the target
operating system's supervisor:

```text
airflow api-server
airflow scheduler
airflow dag-processor
```

The checked-in systemd template documents the Linux shape. Its user, checkout,
venv, and environment-file paths are intentional placeholders for the target
administrator to install under `/opt/bmv-filings` and `/etc/bmv-filings`.
For macOS, the same three commands can be supervised by `launchd`; generate
machine-specific plists only after the destination checkout and external-disk
mount paths are known.

The release gate has not yet certified unattended scheduler/standalone
operation, Playwright browser and host dependencies, PostgreSQL metadata, or
the systemd template on the second computer. Those are destination-host
acceptance checks, not claims made by the local DAG/task canary.

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
- `airflow dags list-import-errors` is empty;
- the audit DAG succeeds against the selected external disk;
- the selected estate is backed up or explicitly disposable; and
- one scoped sync plus its repeat succeeds.

Full source coverage, extraction refinement, consumer rollout, and legacy
estate migration remain follow-on work rather than prerequisites for this
export milestone.

## Official references

- [Airflow 3.3.0 documentation](https://airflow.apache.org/docs/apache-airflow/stable/)
- [Airflow Task SDK](https://airflow.apache.org/docs/task-sdk/stable/)
- [Airflow production deployment](https://airflow.apache.org/docs/apache-airflow/stable/administration-and-deployment/production-deployment.html)
- [Airflow database backend setup](https://airflow.apache.org/docs/apache-airflow/stable/howto/set-up-database.html)
- [Airflow configuration reference](https://airflow.apache.org/docs/apache-airflow/stable/configurations-ref.html)

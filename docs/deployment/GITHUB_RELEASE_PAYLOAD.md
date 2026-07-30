# GitHub source-release payload

This is the authoritative allowlist for the sanitized, single-root-commit
GitHub release. It is intentionally narrower than the current worktree and its
historical Git payload. The document estate and research data are separate
export artifacts.

## Root platform

Exact root files:

```text
.env.example
.gitignore
.python-version
HANDOFF.md
README.md
estate.json
estate_bridge.py
pyproject.toml
```

Source and configuration:

- `.github/workflows/airflow-native.yml`
- `.github/workflows/release-gate.yml`
- all `configs/*.yaml` except obsolete `configs/her.yaml`;
- `deploy/airflow/dags/quarterly_estate.py`;
- `deploy/airflow/native/.env.example`;
- `deploy/airflow/native/systemd/bmv-airflow@.service`;
- all `inputs/*.md`;
- `requirements/acquisition-worker.txt` and
  `requirements/airflow-native.txt`;
- all `src/**/*.py`;
- all `tests/**/*.py`, plus the small HTML/JSON fixtures under `tests/data/`
  and `tests/fixtures/`; and
- the operational root scripts below.

```text
scripts/_regen_baseline.py
scripts/audit_git_payload.py
scripts/audit_primary_pdf_sources.py
scripts/audit_soft_quarterly_coverage.py
scripts/build_document_estate.py
scripts/build_segments.py
scripts/certify_clean_clone.py
scripts/check_estate_connection.py
scripts/check_metric.py
scripts/configure_airflow.py
scripts/consolidate_document_estate.py
scripts/deployment_preflight.py
scripts/expand_soft_quarterlies.py
scripts/fetch_company_reports.py
scripts/fetch_regional_reports.py
scripts/install_airflow_native.sh
scripts/measure_semantic_search.py
scripts/parse_reports_for_search.py
scripts/pipeline_scorecard.py
scripts/process_estate_outbox.py
scripts/refresh_quarterly_estate.py
scripts/sync_xbrl_facts.py
scripts/verify_extraction.py
scripts/zero_shot_eval.py
```

Canonical root documentation:

```text
docs/README.md
docs/ACQUISITION_ENGINE.md
docs/COMPANY_ONBOARDING.md
docs/DEPLOYMENT.md
docs/DOCUMENT_ESTATE.md
docs/KNOWN_ISSUES.md
docs/PHASES.md
docs/STRESS_TEST.md
docs/VERIFICATION.md
docs/ZERO_SHOT.md
docs/architecture/pipeline_phases.yaml
docs/acquisition/PRIMARY_PDF_ACQUISITION.md
docs/acquisition/PRIMARY_PDF_SOURCE_MATRIX_2026-07-30.csv
docs/deployment/AIRFLOW_PORTABILITY.md
docs/deployment/GITHUB_RELEASE_PAYLOAD.md
docs/deployment/RELEASE_GATE_2026-07-30.md
```

One-off migration scripts, tailored model scripts, development probes,
historical sweep logs, obsolete setup notes, and old handoffs are not included.

## Alpha Go

Included:

- `.env.example`, `.gitignore`, `.python-version`, `README.md`,
  `pyproject.toml`, and `requirements.txt`;
- `.streamlit/config.toml`;
- all source under `app/`, `src/`, and `scripts/`;
- configuration under `configs/`;
- tests and their synthetic `tests/fixtures/sample_corpus/`;
- canonical documentation under `docs/`, excluding `docs/handoffs/`; and
- source-like evaluation specifications
  `eval/analog_queries.yaml`, `eval/queries.yaml`, and
  `eval/summary_gold.yaml`.

Corpora, raw documents, indexes, caches, models, handoffs, generated candidate
sets, and corpus-derived relevance/sentiment labels remain external. The two
sentiment performance-floor tests explicitly skip when that evaluation bundle
is not attached; direct and synthetic sentiment tests still run.

## Soft

Included:

- `.gitignore`, `.python-version`, `README.md`, `pyproject.toml`, and
  `requirements.txt`;
- all `configs/*.yaml` and `inputs/*.md`;
- source, tests, and operational scripts, excluding the stale duplicate files
  `scripts/book_scorecard 2.py` and `scripts/reconcile_master 2.py`;
- `eval/reconcile_golden.yaml` and `eval/reconcile_waivers.yaml`;
- canonical `docs/*.md`, excluding the historical
  `docs/STATUS_AND_NEXT_STEPS.md`;
- the opt-in launchd/Pages templates in `deploy/`; and
- `data/bloomberg/README.md` plus the documented synthetic
  `data/bloomberg/walmex.csv` fixture.

Generated `configs/residual_na.json`, blank `inputs/*.bloomberg.csv` templates,
real Bloomberg exports, filing/CNBV caches, outputs, and the nested Pages
working tree remain external or regenerable.

## Earnings

Included:

- `.gitignore`, `.python-version`, `README.md`, `pyproject.toml`, and
  `requirements.txt`;
- configuration under `configs/`;
- source under `earnlib/` and `scripts/`;
- tests and their synthetic fixtures; and
- the frozen Alpha dependency under `vendor/alpha-go/src/`, its required
  `configs/xbrl_concepts.yaml`, and `vendor/alpha-go/VENDORING.md`.

Analyst workbooks, research inputs, audit inventories, study outputs, vendored
databases/raw snapshots, and binary package snapshots remain in the separately
transferred data bundle. `VENDORING.md` records the frozen-fork boundary and
known source divergences.

## Global exclusions

The source release must not contain:

- the prior repository history or any blob reachable only through it;
- Docker files or Docker-based deployment workflows;
- `.env` files, credentials, tokens, private keys, or personal contacts;
- PDFs, SQLite databases, vector indexes, reports, corpora, or blob stores;
- root ground truth and manually verified research data;
- corpus-derived Alpha evaluation labels/candidate sets;
- non-synthetic Bloomberg data;
- Earnings analyst, audit, output, or vendored-data directories;
- build products, virtual environments, caches, logs, backups, and run state;
- host-absolute personal paths; or
- files above the payload size threshold.

## Private-repository boundary

The project has a frozen-vendor boundary note but no repository `LICENSE`.
The first GitHub repository must therefore remain private. Public publication
is blocked until the owner selects a license and confirms ownership/licensing
for the top-level source, the frozen Earnings fork, and any optional research
data released separately.

## Required verification

The isolated repository must contain exactly one root commit before its first
push. From a clean checkout of that exact commit:

```bash
python scripts/audit_git_payload.py
python scripts/certify_clean_clone.py --scope all
```

The native Airflow installer must then be run from the same checkout, followed
by `pip check`, local DAG listing, import-error listing, and the read-only audit
task against a disposable estate. Full-fleet applied sync remains disabled.

# Release-gate handoff — 2026-07-30

## Scope note

This document inventories the complete current worktree delta from `HEAD`, not
authorship. The repository was already substantially dirty when the release
gate began, so some listed changes predate this gate. Nothing has been staged
or committed in the original repository, and it still has no remote. A
source-only allowlist was committed separately as an isolated, one-root-commit
private-GitHub candidate; nothing has been pushed.

## Initial project state

- The root platform, Alpha Go, Soft, and Earnings were present but did not have
  one clean-checkout certification covering their separate Python versions and
  declared dependency files.
- Downloader automation existed as application code, but the native Airflow
  install, DAG import, disabled-by-default sync policy, and portable task
  invocation had not been proven together.
- New estate artifacts could retain host-absolute paths, preventing a copied
  external-disk bundle from being safely relocated.
- The live legacy estate lacked the acquisition, outbox, consumer, and portable
  object-key schema required for unattended read-write use.
- The branch contained or historically referenced databases, generated
  research outputs, non-synthetic market-data exports, local paths, and other
  material that must not be copied directly to GitHub.
- Source coverage was uneven. The registry covered 179 active issuers, with
  broad BMV XBRL coverage but only 25 current primary-PDF bindings.

## Work completed

1. Added a native, non-Docker Airflow deployment with separate audit and sync
   DAGs. The sync DAG is paused and also requires an explicit environment
   enable flag.
2. Installed Airflow 3.3.0 in a disposable Python 3.13 environment, verified
   its dependencies, parsed both DAGs locally with zero import errors, and ran
   the read-only audit task.
3. Ran a scoped live canary for GNP, FEMSA, Sports World, and Tiendas 3B against
   a disposable estate. It exercised BMV XBRL and four PDF adapter patterns.
   The second run stored no duplicate objects.
4. Made newly written original and derivative artifact paths bundle-relative,
   while retaining read compatibility with legacy absolute rows.
5. Verified one acquisition-to-derivative delivery, SQLite integrity, safe
   object keys, and relocation of the disposable estate.
6. Added a clean-snapshot certifier that builds an ephemeral candidate commit,
   clones it again, detaches local estates/corpora/credentials, and runs all
   four declared environments.
7. Added a read-only Git payload/history scanner that redacts credential-like
   matches and rejects databases, private/generated data, local paths, and
   stale-HEAD certification.
8. Removed the embedded personal SEC EDGAR contact and made
   `SEC_EDGAR_USER_AGENT` an explicit local setting.
9. Made Soft and Earnings tests runnable without an attached local corpus and
   replaced several machine-specific runtime paths with repo-local or
   configurable values.
10. Added GitHub Actions workflows for the four offline suites, payload audit,
    and native Airflow import checks.
11. Replaced the last hidden private-data test dependencies with optional
    integration skips or synthetic fixtures: root ground truth, Alpha
    sentiment evaluation labels, and the Earnings analyst workbook.
12. Built a 1,229-file isolated source candidate, scanned its complete
    one-commit history with zero findings, and passed every module from a
    genuinely clean no-estate checkout.
13. Reinstalled native Airflow from that candidate, executed the read-only DAG
    task, ran the four-issuer applied canary, and proved the repeat stored zero
    duplicates.
14. Initialized one bounded derivative-consumer receipt and verified relative
    paths, object keys, blobs, SQLite integrity, and relocated-estate access.

## Files changed

The paths below are tracked files modified relative to `HEAD`. Each group
states the shared purpose; file-specific behavior is covered where material.

### Root configuration and documentation

- `.env.example` — documents the required local EDGAR identity setting.
- `.gitignore` — excludes databases, Airflow state, caches, generated outputs,
  private analyst data, and non-synthetic Bloomberg exports.
- `HANDOFF.md` — replaces the obsolete machine-specific handoff with this
  sanitized release-gate handoff.
- `README.md` — documents the module runtimes, external-estate boundary,
  native Airflow entry points, and disabled-by-default full sync.
- `pyproject.toml` — aligns declared root extras with the acquisition/browser
  worker used by native Airflow.
- `configs/issuers.yaml` — expands and normalizes issuer source bindings used by
  the fleet audit and scoped downloader.

### Existing architecture and operating documents

- `docs/ACQUISITION_ENGINE.md`
- `docs/COMPANY_ONBOARDING.md`
- `docs/DEPLOYMENT.md`
- `docs/DEV_SETUP.md`
- `docs/EXTRACTION_ROADMAP.md`
- `docs/KNOWN_ISSUES.md`
- `docs/PHASES.md`
- `docs/README.md`
- `docs/architecture/pipeline_phases.yaml`
- `docs/sweep/BASELINE_2026-07-29.md`

These files now describe the shared registry, acquisition ledger, portable
estate, downstream boundary, current source coverage, native scheduling, and
the fact that the legacy estate is not yet approved for read-write relocation.

### Alpha Go

- `alpha-go/README.md` — separates the portable prebuilt-index runtime from the
  still-legacy corpus rebuild path.
- `alpha-go/docs/CONFIG.md` — aligns model, dimensions, weights, and environment
  configuration with the certified runtime.
- `alpha-go/scripts/sync_shared_estate.py` — resolves portable estate artifacts
  through the shared estate contract.
- `alpha-go/src/download/edgar.py` — requires a local SEC EDGAR user agent.
- `alpha-go/tests/test_analogs.py`
- `alpha-go/tests/test_corpus_ingest.py`
- `alpha-go/tests/test_doc_matches.py`
- `alpha-go/tests/test_downloader_language.py`
- `alpha-go/tests/test_edgar.py`
- `alpha-go/tests/test_facets.py`
- `alpha-go/tests/test_index_chunker.py`
- `alpha-go/tests/test_index_embeddings.py`
- `alpha-go/tests/test_linear_results.py`
- `alpha-go/tests/test_result_partition.py`
- `alpha-go/tests/test_search_filters.py`
- `alpha-go/tests/test_search_keyword.py`
- `alpha-go/tests/test_search_scoping.py`
- `alpha-go/tests/test_track_a_mentions.py`
- `alpha-go/tests/test_trends.py`

The modified Alpha tests make no-estate behavior explicit, remove hidden local
corpus assumptions, and cover the expanded retrieval and document contracts.

### Earnings

- `earnings/.gitignore` — keeps analyst records, study outputs, audit
  inventories, and vendored data in the separately transferred research
  bundle.
- `earnings/README.md` — documents repo-local setup and configurable model
  discovery.
- `earnings/pyproject.toml`
- `earnings/requirements.txt`

The Earnings dependency files now declare the workbook, HTML, and PDF packages
used by documented paths.

- `earnings/scripts/build_metrics_hist.py`
- `earnings/scripts/scope_model_vintages.py`

These scripts now accept configurable search roots instead of assuming this
machine's directory layout.

- `earnings/tests/test_bootstrap_v3.py` — uses synthetic/no-estate fixtures when
  private data is unavailable.
- `earnings/vendor/alpha-go/src/download/edgar.py` — mirrors the required local
  SEC EDGAR identity behavior.

### Soft

- `soft/README.md` — documents the repo-local Python 3.11 setup and external
  data boundary.
- `soft/deploy/README.md`
- `soft/deploy/com.soft.refresh.plist`
- `soft/scripts/_complete_universe.sh`

The Soft deployment files now use placeholders, repo-relative locations, and a
safe default that does not publish automatically.

- `soft/src/coverage/applicability.py` — treats explicit five-year availability
  as satisfying the shorter-history requirement.
- `soft/tests/test_analysis_blocks.py`
- `soft/tests/test_bank_valuation.py`
- `soft/tests/test_gap_template.py`
- `soft/tests/test_native_derivations.py`
- `soft/tests/test_reit_valuation.py`
- `soft/tests/test_smoke.py`
- `soft/tests/test_spec_and_template.py`
- `soft/tests/test_valuation.py`

The modified Soft tests remove dependence on private local files and verify the
portable/synthetic execution paths.

### Root acquisition, estate, and deployment code

- `src/acquisition/adapters.py` — strengthens adapter behavior and source
  handling.
- `src/acquisition/cli.py` — exposes the revised acquisition controls.
- `src/acquisition/models.py` — adds the normalized source and result fields
  required by the expanded downloader.
- `src/acquisition/registry.py` — loads and validates the expanded issuer
  registry.
- `src/acquisition/service.py` — coordinates audit, plan, scoped apply,
  idempotency, and source readiness.
- `src/acquisition/writer.py` — writes portable artifact references and
  acquisition receipts.
- `src/consumers/derivatives.py` — resolves portable originals and stores
  portable derivative references.
- `src/deployment/preflight.py` — checks schema, object keys, paths, outbox,
  consumers, and source readiness before production use.
- `src/download/downloader.py` — expands strict discovery, validation, and
  adapter-aware document downloading.
- `src/shared/document_estate.py` — defines bundle-relative artifact storage and
  backward-compatible resolution.
- `src/shared/paths.py` — centralizes portable project and estate paths.
- `scripts/build_segments.py` — removes hidden path assumptions from segment
  generation.
- `scripts/certify_clean_clone.py` — implements the all-module clean-snapshot
  gate.

### Modified root tests

- `tests/test_acquisition_ledger_writer.py`
- `tests/test_acquisition_registry.py`
- `tests/test_build_segments.py`
- `tests/test_delivery_contract.py`
- `tests/test_deployment_preflight.py`
- `tests/test_derivative_consumer.py`
- `tests/test_paths.py`
- `tests/test_phase_manifest.py`
- `tests/test_quarterly_acquisition.py`

These tests cover the expanded registry/downloader, relative estate paths,
delivery/outbox behavior, production blockers, and the required release
payload.

### Files removed from the publication candidate

- `soft/configs/gfinbur.yaml`
- `soft/data/bloomberg/fibra_uptown.csv`
- `soft/inputs/gfinbur.bloomberg.csv`
- `soft/inputs/gfinbur.md`

These issuer-specific/private input artifacts are removed from the proposed
source payload. Their external-data copies, if needed, belong in the private
data bundle rather than Git.

## Files created

All paths below are currently untracked and must be added only through a
reviewed source allowlist.

### CI and native Airflow

- `.github/workflows/airflow-native.yml` — installs Airflow natively, runs
  focused tests, migrates disposable metadata, and verifies DAG imports.
- `.github/workflows/release-gate.yml` — audits the full Git history and runs
  all four no-estate suites in their declared Python versions.
- `deploy/airflow/dags/quarterly_estate.py` — defines the read-only audit DAG
  and the separately gated synchronization DAG.
- `deploy/airflow/native/.env.example` — documents non-secret native runtime
  settings.
- `deploy/airflow/native/systemd/bmv-airflow@.service` — provides an
  exportable Linux service template.
- `requirements/airflow-native.txt` — pins the native Airflow release.
- `scripts/configure_airflow.py` — renders local Airflow configuration while
  keeping metadata separate from the document estate.
- `scripts/install_airflow_native.sh` — creates the Python 3.13 Airflow
  environment, installs project acquisition dependencies, and enforces
  `pip check`.
- `src/deployment/airflow_task.py` — validates environment controls and invokes
  the root acquisition CLI from the Airflow interpreter.
- `tests/test_airflow_deployment.py` — tests DAG defaults, task validation,
  environment rendering, and CI coverage.

### Acquisition and source-readiness additions

- `src/acquisition/bmv_issuer.py` — implements BMV issuer document discovery.
- `src/acquisition/readiness.py` — calculates per-issuer source readiness.
- `src/shared/company_aliases.py` — centralizes issuer aliases shared across
  modules.
- `scripts/audit_primary_pdf_sources.py` — audits the primary-PDF registry
  without applying downloads.
- `docs/acquisition/PRIMARY_PDF_ACQUISITION.md` — documents adapter strategy and
  onboarding.
- `docs/acquisition/PRIMARY_PDF_SOURCE_MATRIX_2026-07-30.csv` — records the
  dated source-coverage matrix.
- `tests/test_acquisition_readiness.py`
- `tests/test_bmv_issuer_pdf.py`
- `tests/test_company_aliases.py`
- `tests/test_downloader_strict_pattern.py`

The new acquisition tests cover readiness, BMV PDF behavior, shared aliases,
and strict period/document matching.

### Release-gate tooling and documentation

- `scripts/audit_git_payload.py` — scans the candidate and reachable Git blobs
  without mutating the repository or printing matched secrets.
- `tests/test_audit_git_payload.py` — verifies payload classification,
  redaction, and history behavior.
- `tests/test_certify_clean_clone.py` — verifies snapshot creation, dirty-HEAD
  refusal, environment isolation, and estate rejection.
- `docs/deployment/AIRFLOW_PORTABILITY.md` — gives the other-computer native
  Airflow and external-disk setup procedure.
- `docs/deployment/GITHUB_RELEASE_PAYLOAD.md` — defines the exact source-only
  publication allowlist and global data/history exclusions.
- `docs/deployment/RELEASE_GATE_2026-07-30.md` — records gate evidence,
  blockers, and the ordered publication/migration sequence.
- `earnings/.python-version`
- `soft/.python-version`

The two version files pin the documented Python 3.11 runtime for those
subprojects.

### New Earnings publication-safe fixtures

- `earnings/tests/fixtures/protocol/log_3T26.csv`
- `earnings/tests/fixtures/protocol/protocolo_llamadas_3T26.md`

These synthetic fixtures test the analyst-protocol schema and label contract
without requiring the private call log or generated Earnings output.

- `earnings/vendor/alpha-go/VENDORING.md` — records the frozen-fork boundary,
  five approved source divergences, and the external-data/licensing boundary.

### Release-candidate portability hardening

- `tests/_corpus.py`
- `tests/test_compare_extractions.py`
- `alpha-go/tests/test_sentiment.py`
- `alpha-go/scripts/retag_doc_types.py`
- `alpha-go/scripts/verify_mentions.py`
- `earnings/scripts/eval_simple_analyst.py`
- `earnings/tests/test_simple_analyst_report.py`
- `soft/scripts/publish_pages.py`
- `soft/tests/test_publish_pages.py`

These changes remove clean-clone dependence on private research files, replace
personal environment/repository defaults with explicit configuration, and
retain synthetic coverage of the same behaviors.

The following allowlisted files received formatting-only cleanup so a
single-root Git diff has no whitespace errors:

- `alpha-go/configs/analogs.yaml`
- `alpha-go/src/news/models.py`
- `earnings/vendor/alpha-go/src/news/models.py`
- `soft/data/bloomberg/walmex.csv`
- `src/shared/company_aliases.py`

### New Alpha Go tests

- `alpha-go/tests/__init__.py`
- `alpha-go/tests/test_bmv_issuer_adapter.py`
- `alpha-go/tests/test_bmv_xbrl_kinds.py`
- `alpha-go/tests/test_brief.py`
- `alpha-go/tests/test_content_classification.py`
- `alpha-go/tests/test_dashboard_modes.py`
- `alpha-go/tests/test_dashboard_search_audit.py`
- `alpha-go/tests/test_doc_types.py`
- `alpha-go/tests/test_document_catalog.py`
- `alpha-go/tests/test_downloader_annual.py`
- `alpha-go/tests/test_llm.py`
- `alpha-go/tests/test_mention_analytics.py`
- `alpha-go/tests/test_news_corpus.py`
- `alpha-go/tests/test_news_gdelt.py`
- `alpha-go/tests/test_news_google.py`
- `alpha-go/tests/test_parse_tables_render.py`
- `alpha-go/tests/test_pdf_evidence.py`
- `alpha-go/tests/test_query_understanding.py`
- `alpha-go/tests/test_reembed_index.py`
- `alpha-go/tests/test_render_search_semantic_primary.py`
- `alpha-go/tests/test_source_adapters.py`
- `alpha-go/tests/test_summarize.py`
- `alpha-go/tests/test_sync_shared_estate.py`
- `alpha-go/tests/test_unknown_document_generalization.py`
- `alpha-go/tests/test_upload.py`

These tests form the clean-checkout Alpha regression set for BMV ingestion,
document typing, retrieval, dashboards, evidence rendering, news, LLM/provider
selection, shared-estate sync, and upload flows.

### New Soft tests

- `soft/tests/__init__.py`
- `soft/tests/test_a2_extraction_fixes.py`
- `soft/tests/test_accuracy_fixes.py`
- `soft/tests/test_applicability.py`
- `soft/tests/test_book_scorecard.py`
- `soft/tests/test_broken_fixes.py`
- `soft/tests/test_build_dense.py`
- `soft/tests/test_certify_master.py`
- `soft/tests/test_cnbv.py`
- `soft/tests/test_currency_detection.py`
- `soft/tests/test_depreciation_mapping.py`
- `soft/tests/test_dividend_fill.py`
- `soft/tests/test_market_cache_ttl.py`
- `soft/tests/test_master_gate.py`
- `soft/tests/test_master_html.py`
- `soft/tests/test_publish_pages.py`
- `soft/tests/test_reconcile.py`
- `soft/tests/test_refresh_straggler.py`
- `soft/tests/test_reit_ffo.py`
- `soft/tests/test_universe_gap.py`
- `soft/tests/test_validate_notes.py`
- `soft/tests/test_xbrl_gzip.py`
- `soft/tests/test_yahoo_fallback.py`

These tests form the clean-checkout Soft regression set for extraction,
valuation, applicability, market fallbacks, publishing, reconciliation, master
certification, and XBRL handling.

## Files and systems intentionally left untouched

- Original-repository Git index, refs, branch history, and remotes — the
  isolated candidate does not mutate or inherit them.
- The live legacy document estate and its blobs — every inspection was
  read-only because the catalog is not migration-ready.
- Local `.env` files and credentials — these remain outside the release
  payload; the Alpha environment file was restricted to owner-only access.
- Ignored corpora, reports, model caches, and the separate Alpha vector index —
  these are external artifacts, not source-code payload.
- The legacy estate's schema and absolute paths — migration must operate on a
  fenced copy, never directly during code publication.
- Full-fleet applied synchronization — only audit/plan and scoped disposable
  canaries were permitted.
- Soft and Earnings production refresh consumers — Airflow currently refreshes
  the root raw-document estate, not every downstream analytical output.

## Sources of truth

- `estate.json` and `estate_bridge.py` define how modules locate a shared
  external estate.
- `configs/issuers.yaml` defines the active issuer registry and downloader
  source bindings.
- `pyproject.toml`, each subproject's `requirements.txt`, and the four
  `.python-version` files define executable environments.
- `docs/DEPLOYMENT.md` defines production preflight and the legacy-estate
  boundary.
- `docs/deployment/AIRFLOW_PORTABILITY.md` is the operational setup procedure
  for the second computer.
- `docs/deployment/RELEASE_GATE_2026-07-30.md` is the evidence-backed release
  decision.
- `docs/ACQUISITION_ENGINE.md` and
  `docs/acquisition/PRIMARY_PDF_ACQUISITION.md` define downloader behavior and
  coverage policy.

## Current project state

- The exact isolated candidate passes the no-estate suites: root 846 passed,
  Alpha Go 479 passed, Soft 326 passed, and Earnings 116 passed. Optional
  data/model tests skip cleanly.
- Native Airflow 3.3.0 imports both DAGs with zero errors and passes
  `pip check`. The audit task succeeds; the sync DAG remains paused and
  environment-gated.
- The exact-candidate canary stored 70 scoped documents. Its repeat found 10
  recent objects unchanged and stored 0.
- The greenfield estate layout is relocatable, uses relative artifact/object
  paths, and passed SQLite/blob/catalog integrity checks.
- The sanitized Git payload is ready for a private repository: 1,229 files,
  one root commit, clean history, and zero scanner findings. The original
  repository/history remains intentionally unsuitable for direct push.
- The live legacy estate has seven read-write cutover blockers and remains
  intentionally untouched.
- Alpha's estate-selected hashing/256 index does not match the configured
  multilingual MiniLM/384 runtime. The correct exported index must be chosen
  deliberately.
- Raw-document freshness is automated, but production consumers for raw XBRL,
  Soft, and Earnings are not yet wired into that freshness chain.

## Immediate next steps

1. Confirm the GitHub owner and repository name, and create an empty private
   repository.
2. Push only the isolated one-root-commit candidate; never push or mirror the
   original repository history.
3. Require both GitHub Actions workflows to pass.
4. On the second computer, clone the private repository and rerun the payload
   audit plus all four no-estate suites.
5. Install native Airflow, attach only a disposable/greenfield external-disk
   estate, and rerun the scoped canary twice.
6. Keep all databases, analyst data, generated Earnings outputs,
   non-synthetic Bloomberg exports, and research ground truth outside Git.
7. Keep full-fleet sync disabled until the destination-disk canary passes and
   a separate legacy-estate migration is approved.
8. Select a repository license before any public publication.

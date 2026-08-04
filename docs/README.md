# Financial reports pipeline

Downloads Mexican quarterly reports, extracts financial metrics (deterministic cascade:
XBRL → BMV statements → command-F search → regex → tables → optional LLM), and generates standardized **Segments** Excel models —
driven from the terminal. The simplest entry point is `scripts/build_segments.py`: one
markdown file in, one retained company build plus one canonical analyst handoff.

## Root quarterly acquisition status

The root `src/acquisition/` module currently supports BMV XBRL, issuer-IR PDFs,
official BMV issuer-page PDFs, and explicit Wayback backfill. It does not
implement root SEC EDGAR or upload adapters. `audit` and `plan` are
local/read-only; scheduling is external.

The module is not production-ready as the sole writer. Only 25 of 179 active
issuers have a configured primary PDF source (24 issuer-IR sources and one
official BMV source), five have explicit root live-canary evidence, and
listing/activity dates, fiscal-year ends, and filing-grace values have not been
issuer-verified. Alpha Go, Soft, and Earnings retain transitional writers. A
bounded outbox dispatcher, PDF/HTML/text-to-Markdown derivative consumer, and
optional batched Alpha Go projection consumer now exist, but have not been
migrated into the live catalog or scheduled.

```bash
# Safe local/read-only inspection.
python3 scripts/refresh_quarterly_estate.py audit
python3 scripts/refresh_quarterly_estate.py plan --only gap,kof
python3 scripts/audit_primary_pdf_sources.py --json

# Read-only deployment and outbox health checks.
python3 scripts/deployment_preflight.py audit
python3 scripts/process_estate_outbox.py status \
  --database data/document_estate/catalog.db --json

# Root acquisition contract tests.
python3 -m pytest tests/test_quarterly_acquisition.py \
  tests/test_acquisition_ledger_writer.py -q
```

Do not place `sync --apply` on cron or a managed production job until source and
lifecycle enrichment, consumer cutover, the controlled consumer migration, and
the production backend contract are complete. See
[DEPLOYMENT.md](DEPLOYMENT.md) for preflight and worker safety and
[ACQUISITION_ENGINE.md](ACQUISITION_ENGINE.md) for the full acquisition gate.

For the canonical phase wiring, use `docs/architecture/pipeline_phases.yaml`;
`docs/PHASES.md` is the human-readable map for agents and maintainers.

**Onboarding a new company end-to-end** (metric list → outline → config → optional custom
extractor → STRONG verification gate → independent accuracy certification): follow
`docs/COMPANY_ONBOARDING.md` — the frozen runbook used to ship Soriana and Herdez at 100%
certified accuracy. It is also invocable as the `/onboard-company` skill.

## Layout (role-based packages)

```
src/
  acquisition/ issuer registry, source adapters, recurring refresh service
  consumers/  durable delivery, document derivatives, optional Alpha Go projection
  deployment/ read-only environment and estate preflight
  download/   downloader.py, bmv_xbrl.py            # fetch reports from IR sites / BMV XBRL
  parse/      parse_pdf.py                           # PDF → aligned markdown (+ DocMeta)
  extract/    extract_metrics.py, parse_tables.py,   # metric extraction cascade
              tiered_extract.py, xbrl_facts.py,
              llm_extract.py, pipeline.py, interface.py
  excel/      segments_sheet.py                       # Segments workbook generator
  eval/       compare_extractions.py                  # accuracy vs ground truth
  model/      financial_model.py, sport_metrics.py    # metric registry + configs glue
  shared/     paths.py, validator.py, report_index.py, document_estate.py # cross-cutting helpers
scripts/      build_segments.py                        # one-command markdown → Segments xlsx
configs/      <company>.yaml + <company>_segments.yaml, xbrl_concepts.yaml
data/
  document_estate/     SQLite metadata, immutable local objects, and compatibility views
  reports/<company>/   legacy cache being migrated into the shared estate
  ground_truth/        optional private accuracy baselines (not in Git)
  style/               optional private reference workbooks (not in Git)
docs/         current guides, architecture, deployment, and verification records
tests/        pytest suite
archive/      retired/duplicate artifacts (safe to delete)
```

All resource paths resolve through `src/shared/paths.py` (`PROJECT_ROOT`, `CONFIGS_DIR`,
`REPORTS_DIR`, `GROUND_TRUTH_DIR`, `STYLE_DIR`).

## Run

CLIs are invoked as modules from the project root:

```bash
# Strict onboarding: the analyst CSV/XLSX, explicitly pinned Markdown outline,
# and a successful company acquisition receipt must all agree.
python3 scripts/build_segments.py path/to/company.md \
  --analyst-metrics requests/Metrics.xlsx --analyst-company TICKER

# Extract metrics → CSV
python -m src.extract.pipeline --dir data/reports/sport --config configs/sport.yaml --csv out.csv

# Generate a Segments workbook (outline mode, offline)
python -m src.excel.segments_sheet --config configs/walmex.yaml \
    --source data/reports/walmex --out walmex_segments.xlsx
```

The root acquisition service is the migration target for keeping the shared
estate current; it is not yet the only writer. The implemented outbox worker can
create parsed derivatives and optionally project them into Alpha Go, but it is
not a deployed service and does not cover Soft or Earnings. See
[ACQUISITION_ENGINE.md](ACQUISITION_ENGINE.md). The current local estate and
compatibility contract is documented in [DOCUMENT_ESTATE.md](DOCUMENT_ESTATE.md).

```bash
# Accuracy vs ground truth
python -m src.eval.compare_extractions lacomer walmex

# Parse a PDF to markdown
python -m src.parse.parse_pdf report_q1.pdf
```

### The metrics list is a lightweight outline

`build_segments.py` reads the markdown's metrics section as a Segments **outline**, fed to
the existing `parse_outline` → `pipeline.run` → `build_outline_workbook` machinery:

```markdown
# Soriana
IR: https://www.organizacionsoriana.com/principales_reportes_en.html
Analyst-Metrics: requests/Metrics.xlsx#Requested Metrics
Analyst-Company: SORIANA

## Total Income
- Total Income {revenue}
- YoY

## Profitability
- Gross Income {gross_profit}
- Margin
- bps change
- EBITDA {ebitda}
- Margin
- Net Income {net_income}
- Margin
```

- `## Heading` → a workbook section.
- `- Label` → drafting convenience only; strict onboarding rejects an unpinned data row.
- `- Label {metric_key}` → a data row with an explicit, authoritative key.
- `- YoY` / `- Margin` / `- As % of Total` / `- bps change` / `- Check` / `- 2-year comp`
  → derived rows, rendered as live Excel formulas.

The analyst sheet, Markdown, and rendered workbook are compared by ordered label, row role
(section versus requested row), and any analyst-declared canonical key. Requested rows are never
silently pruned, and formulas with no evaluable periods fail the Excel audit. A weak/audit-failing
candidate cannot replace `outputs/latest/` and the CLI returns status 3. The previous
single-workbook handoff moves to `outputs/archive/deliverables/` after a serialized,
crash-recoverable publication.

A populated cache is not proof of freshness. Strict builds require a successful, non-backfill
canonical acquisition receipt for the exact company within 24 hours; use
`refresh-quarterly-estate sync --only <slug> --apply` first. `--allow-stale-estate` and
`--allow-auto-map` are recorded exploratory escape hatches, not shipping modes. Publication binds
the receipt to the exact selected catalog artifacts and their hashes, so a stale view or local
cache cannot pass merely because discovery ran. Extraction reads the zero-copy union of the
reports compatibility view, versioned parsed derivatives, and any transitional local report cache.
The adjacent `outputs/latest_manifest.json` binds the current workbook SHA/build ID to its analyst
contract and estate watermark while `outputs/latest/` itself remains exactly one `.xlsx` file.

## Extraction Config Knobs

- `tier_precedence`: per-metric or default source ordering (`xbrl`, `bmv`, `search`,
  `regex_table`, `prose`, `table`, `calc`). Use this when a company-specific row is
  more reliable than a generic tier.
- `text_search`: deterministic command-F layer over parsed markdown. Supports
  `disabled: true`, `skip_annual_rows: true`, `max_line_chars`, and metric-specific
  scaling rules.
- `table_extract.disabled: true`: skips the generic fuzzy PDF table tier for tuned
  configs. Exact YAML table regexes still run and are tagged `[regex_table]`.
- `ytd_quarterly_periods`: period-scoped accumulated-to-quarter differencing for
  metrics whose source rows are YTD only in specific periods. Prefer this over
  global `ytd_quarterly` when the same metric is quarterly in later reports.

## Tests

```bash
pytest tests/ -q -m "not network"
```
`pyproject.toml` puts the project root on `pythonpath`, so `import src.<role>.<module>`
resolves without any `sys.path` hacks.

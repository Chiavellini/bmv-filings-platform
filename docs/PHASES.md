# Pipeline Phases

This file is the human map for the financial reports pipeline. The machine-readable source of truth is `docs/architecture/pipeline_phases.yaml`; keep that manifest authoritative when phase wiring changes.

## Product Phases

1. **Acquire** (`src.acquisition`; manifest id `download` for graph compatibility)
   Plan fleet coverage, fetch PDFs and exact BMV XBRL filings through `src.download` adapter primitives, validate them, and durably version them in `data/document_estate/`. The stable orchestration entrypoint is `QuarterlyAcquisitionService`; `scripts/refresh_quarterly_estate.py` is its operator CLI. Direct `src.download` calls are transitional primitives, not a second durable corpus.

2. **Parse** (`src.parse`)
   Convert PDFs into aligned markdown plus `DocMeta`. The stable entrypoint is `parse_pdf(..., with_meta=True)`.

3. **Extract** (`src.extract`)
   Convert `PeriodSource` objects into canonical metrics through the tiered cascade. Use `extract_metrics_tiered` for one period and `pipeline.run` or `interface.extract` for batches. Table and evidence search use the deterministic semantic dictionary in `configs/metric_search.yaml` through `src.extract.semantic_search`. Once a row is matched, Tier 2 selects the cell whose column header matches the document's target quarter via `src.extract.table_periods` (prior = same quarter, prior year); it falls back to the legacy positional rule (first numeric = current) when the period or header can't be parsed.
   When changing table-search behavior, measure the old alias matcher versus the semantic matcher with `python3 scripts/measure_semantic_search.py lacomer sport walmex`; this compares both paths against `data/ground_truth` using the same PDF table rows.

4. **Generate Excel** (`src.excel`)
   Build Segments workbooks from extracted DataFrames and outline specs. Use `generate_company_segments` for company workbooks and `build_outline_workbook` for direct DataFrame-driven tests.

5. **CLI** (`scripts/build_segments.py`)
   Single-command workflow: read one regularized markdown (company name, IR link, and a metrics **outline**), retain technical artifacts under `outputs/<Company>/{csv,excel,validation}/`, and publish the newest workbook as the only file in `outputs/latest/`. The metrics outline (`## sections`, `- Label`, `- Label {metric_key}` pins, and derived rows like `YoY`/`Margin`/`As % of Total`) is fed through the existing **outline machinery** — `segments_sheet.parse_outline` → `pipeline.run` → `segments_sheet.build_outline_workbook` — so derived rows become live Excel formulas and unmapped rows render as graceful blanks. If `configs/<slug>.yaml` exists it is reused for tuned download/extraction; if `data/reports/<slug>/` already holds parsed reports they are reused (download/parse skipped, `--force-download` overrides).

Validation and evaluation are quality gates, not product phases:

- `src.shared.validator` flags suspicious extracted values and confidence.
- `src.eval.compare_extractions` measures coverage and accuracy against ground truth.

## Phase Graph

```text
acquire -> shared estate -> parse -> extract -> generate_excel -> cli
                                      \                          ^
                                       \-------------------------/
```

The CLI invokes all earlier phase facades end-to-end, but the underlying data
flow remains one-way: reports are acquired into the shared estate, parsed,
extracted, and then rendered into CSV and Excel outputs.

## Directory Ownership

- `src/acquisition/`: issuer registry, coverage planning, source orchestration, validation, immutable versioning, acquisition ledger, and estate write boundary.
- `src/download/`: network and archive adapter primitives used by acquisition; not a durable corpus owner.
- `src/parse/`: PDF-to-markdown conversion and parse metadata.
- `src/extract/`: metric registry application, tiered extraction, evidence mode, and batch orchestration.
- `src/excel/`: Segments workbook layout and formulas, plus the metric-list → Segments helpers (`segments_from_queries`, `build_segments_xlsx`).
- `scripts/build_segments.py`: the single-markdown CLI that orchestrates all phases.
- `src/shared/`: cross-phase helpers, paths, validation, report indexing, and the phase registry.
- `configs/`: company configs, segment outlines, XBRL concept mappings, and the deterministic metric search dictionary.
- `data/document_estate/`: canonical shared catalog, immutable originals, and compatibility views.
- `data/reports/`: transitional local cache and parsed markdown; not the canonical durable-original store.
- `data/ground_truth/`: comparison baselines.
- `outputs/latest/`: analyst handoff; exactly one workbook from the newest completed onboarding build.
- `outputs/<Company>/`: technical CSV, workbook, and validation artifacts retained for review.
- `downloads/`, `excels/`: legacy generated-artifact locations.
- `docs/handoffs/` and top-level handoff docs: historical context, not live contracts.
- `archive/`: retired or duplicate artifacts; do not treat as active pipeline input unless a task explicitly says so.

## Agent Workflow

Start with `docs/architecture/pipeline_phases.yaml` when a task changes phase wiring, public entrypoints, or artifact flow.

For behavior changes, update the phase contract test first or in the same change. The core offline gate is:

```bash
python3 -m pytest tests/test_phase_contracts.py tests/test_phase_manifest.py -q -m "not network"
```

Use broader tests when touching shared behavior:

```bash
python3 -m pytest -q -m "not network"
```

For extraction levers that should improve table matching, run the focused ground-truth measurement before claiming a lift:

```bash
python3 scripts/measure_semantic_search.py lacomer sport walmex --details
```

Network and real API checks are opt-in through the `network` marker and should not block normal agent iterations.

# Reuse Map

How alpha-go reuses the parent financial-reports repo, and what is net-new.

## Strategy: vendor by copy, same `src.` namespace

The parent uses `import src.<role>.<module>` with `pyproject.toml` `pythonpath = ["."]`.
alpha-go replicates that **inside itself**:

- The reusable parent packages are **copied verbatim** into `alpha-go/src/`.
- `alpha-go/pyproject.toml` also sets `pythonpath = ["."]`, so running from `alpha-go/`,
  `import src.extract.tiered_extract` resolves to **alpha-go's own copy** — **zero import
  rewrites**, and total isolation from the parent.
- New layers live under the same `alpha-go/src/` tree and import the vendored core with the
  same `src.` prefix.
- `src/shared/paths.py` derives `PROJECT_ROOT` from `Path(__file__).parents[2]`, so once
  copied it points at `alpha-go/` and resolves `alpha-go/configs`, `alpha-go/data` — **no
  edit needed** (verified by `tests/test_smoke.py::test_paths_reroot_into_alpha_go`).

**alpha-go never imports from the parent repo.** Reuse is by copy, recorded and re-runnable
via `scripts/vendor_sync.py`. The parent tree is never modified.

## Vendored packages (copied verbatim from `<parent>/src/`)

| Package | Role in alpha-go | Key reused entrypoints |
|---------|------------------|------------------------|
| `src/download/` | Ingest: fetch reports from IR sites / BMV XBRL / Wayback | `downloader:download_from_ir`, `bmv_xbrl:download_ticker` |
| `src/parse/` | Ingest: PDF → aligned markdown + DocMeta (+ parse cache) | `parse_pdf:parse_pdf` |
| `src/extract/` | Financials panel (4-tier cascade) + synonym dictionary for keyword expansion | `tiered_extract:extract_metrics_tiered`, `semantic_search` (alias loader) |
| `src/model/` | Metric registry + config glue for the financials panel | `financial_model:load_config/apply_config`, `METRICS` |
| `src/shared/` | Paths (re-root), period normalization, validation | `paths`, `report_index:infer_period_label`, `validator:validate` |
| `src/excel/` | Optional: export a company's financials to a Segments workbook | `segments_sheet:generate_company_segments` |
| `src/eval/` | Optional: accuracy of the financials panel vs ground truth | `compare_extractions:run_comparison` |

The whole parent `src/` was copied for completeness (company-specific extractors
`gruma`/`liverpool`/`lab`, `phase_registry`, etc. come along), minus the exclusions below.

## Vendored config assets (copied from `<parent>/configs/`)

All `configs/*.yaml`: company configs (`walmex.yaml`, `lacomer.yaml`, …) + segment outlines
(`*_segments.yaml`) + the dictionaries `metric_search.yaml` and `xbrl_concepts.yaml`. These
drive the financials panel and the keyword-search synonym expansion.

## Deliberately NOT vendored

| Skipped | Why |
|---------|-----|
| `src/ui/` (parent's retired Streamlit app) | alpha-go ships its own `app/`. |
| parent `tests/` | They reference parent corpora; alpha-go has its own `tests/`. |
| `data/reports`, `data/ground_truth`, `outputs/`, `__pycache__` | Generated/corpus data; alpha-go builds its own `data/`. |

## Net-new (alpha-go's actual contribution)

None of this exists in the parent — it is the search/intelligence layer:

| New package | What it adds |
|-------------|--------------|
| `src/corpus/` | Corpus manifest + ingestion orchestration over the vendored phases. |
| `src/index/` | Chunker, SQLite store + schema, FTS5 keyword index, local embeddings, index build. |
| `src/search/` | Hybrid retriever, RRF fusion, snippet highlighting, filters. |
| `src/qa/` | Optional cited RAG synthesis. |
| `app/` | Streamlit dashboard. |

## Keeping the vendored copy fresh

```bash
python3 scripts/vendor_sync.py --check   # show what would be (re)copied
python3 scripts/vendor_sync.py --sync    # re-copy from the parent (overwrites vendored pkgs)
```

The authoritative copy list lives in `scripts/vendor_sync.py` (`VENDORED_PACKAGES`,
`VENDORED_CONFIGS`, `EXCLUDE`) and is mirrored by this document. Because vendored code is not
edited in place, a re-sync is always safe; new behavior is added in the new packages above.

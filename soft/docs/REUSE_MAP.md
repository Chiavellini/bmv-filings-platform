# Reuse Map

`soft/` is self-contained. It **vendors** (copies) the reusable parent packages under its own
`src/` so that `import src.<role>.<module>` resolves to *this* copy, never the parent repo. The
copy is one-directional and reproducible via `scripts/vendor_sync.py`.

## Vendored from the parent (do NOT edit in place)

| Package | What soft uses it for |
|---------|-----------------------|
| `src/extract/` (`pipeline`, `tiered_extract`, `extract_metrics`, `parse_tables`, `xbrl_facts`) | `[filing]` fundamentals — run the 4-tier cascade over cached filings |
| `src/model/financial_model.py` | `MetricDef` registry + `apply_config` (metric defs the cascade consumes) |
| `src/excel/segments_sheet.py` | Workbook styling + hard-input-vs-formula cell helpers reused by `src/sheets/` |
| `src/shared/{paths,validator}.py` | Path re-rooting into `soft/`; accounting-identity validation of `[filing]` values |
| `src/eval/verification_gate.py` | Coverage/confidence scoring of the fundamentals |
| `src/download/`, `src/parse/` | Refresh the filings cache (only when re-downloading; pilot reuses the cache) |
| `configs/*.yaml` | Per-company tuned configs (e.g. `walmex.yaml`) + `metric_search.yaml` |

Deliberately **not** vendored: `src/ui/` (parent's retired app) and parent `tests/`.

## Net-new in soft (this is where all new logic lives)

| Package | Purpose |
|---------|---------|
| `src/bloomberg/` | `schema.py` (BloombergPack + field registry), `template.py` (emit blank template), `ingest.py` (parse filled template) — the `[bbg]` side |
| `src/coverage/` | `spec.py` (parse `inputs/<slug>.md`), `fundamentals.py` (cascade adapter), `valuation.py` (metric dictionary + calculators), `peers.py` (peer cross-section) |
| `src/sheets/` | `valuation_sheet.py` — render Sheet 1 |

## Self-containment invariant

`tests/test_smoke.py::test_self_contained_no_parent_imports` scans every `soft/src/**/*.py` and
fails if any file references `pdfs/src`, `pdfs/configs`, `import pdfs`, or `from pdfs`. Re-sync
after parent infra changes:

```bash
python3 scripts/vendor_sync.py --check   # show drift
python3 scripts/vendor_sync.py --sync    # re-copy (overwrites vendored packages)
```

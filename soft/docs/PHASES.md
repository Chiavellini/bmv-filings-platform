# Phases

| Phase | Package / entry | Purpose | Status |
|---|---|---|---|
| 0. Scaffold + vendor | `scripts/vendor_sync.py` | Copy parent infra under `src/`; self-contained | ✅ |
| 1. Spec + BBG template | `src/coverage/spec.py`, `src/bloomberg/{schema,template,ingest}.py` | Parse `inputs/<slug>.md`; emit + ingest the fill-in template | ✅ |
| 2. Fundamentals | `src/coverage/fundamentals.py` | Run the vendored cascade over cached filings → LTM + FY views | ✅ |
| 3. Valuation | `src/coverage/{valuation,peers}.py` | Metric dictionary + calculators (pure, tested) | ✅ |
| 4. Workbook | `src/sheets/valuation_sheet.py`, `scripts/build_coverage.py` | Render Sheet 1 (live formulas) + CSV, end-to-end | ✅ |
| 5. Validation | `src/coverage/validate.py` | Coverage sanity + fundamentals identities → `validation/*.md` | ✅ |

## Data flow

```
inputs/<slug>.md ─► spec ─┬─► emit template ─► (user fills) ─► data/bloomberg/<slug>.csv ─► pack
                          │                                                                   │
data/reports/<slug>/ ─────┴─► vendored cascade ─► fundamentals (LTM + FY) ──────┐            │
                                                                                ▼            ▼
                                                                          build_model(spec, fund, pack)
                                                                                ▼
                            outputs/<Name>/{excel/<Name>.xlsx, csv/<slug>_coverage.csv, validation/<slug>_validation.md}
```

Run order: `emit_bloomberg_template.py` → fill → `build_coverage.py`.

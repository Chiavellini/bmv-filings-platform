# Pipeline Stress Test — Phase-by-Phase Health

Date: 2026-06-21. Environment: Python 3.14, system interpreter. Test suite baseline:
**433 passed, 1 skipped** (`python3 -m pytest -q`, 238s).

Scope: full 6-stage pipeline (download → parse → 4-tier extraction cascade → validate → compare →
excel), measured against `data/ground_truth/*.csv`. Companies with local reports: **SPORT, WALMEX,
LACOMER** (measured here). **CHEDRAUI, BIMBO, LIVERPOOL** have ground truth but no downloaded
reports — see "Blocked" at the end.

Reproduce: `python3 -m src.eval.compare_extractions <co> --tiers xbrl,table,prose`
Raw output archived at `/tmp/stress/full_cascade.txt`.

---

## Verdict per phase

| Phase | Health | Notes |
|---|---|---|
| 1 Download | ✅ OK | SPORT 40, WALMEX 49, LACOMER 41, HERDEZ 75 reports on disk. |
| 2 Parse → MD | ✅ OK | All local companies parsed; superscript/scale/section stages run. |
| **3 Extraction cascade** | ⚠️ **WEAKEST** | Tier-1 XBRL **dead (0 facts)**; coverage gaps + definitional/scale bugs in Tiers 2/3. Details below. |
| 4 Validate | ⚠️ Passive | Cross-checks run but never flag/exclude bad values. |
| 5 Compare | ✅ OK | Harness extended to cover SPORT (currency scaled miles→P$mn). |
| 6 Excel | ✅ Fixed | Was blocked on missing `openpyxl`; installed. 3 workbooks generated & verified. |

**Conclusion: Phase 3 (the 4-tier extraction cascade) needs the most work.** It is stepified into
a production-readiness checklist in `tasks/index.md`.

---

## UPDATE — Phase 3 strengthening progress

**Phase A (Tier 1 XBRL) — DONE & verified.** Wired real XBRL facts into `data/reports/` via the new
`scripts/sync_xbrl_facts.py` (joins BMV filings to reports on the canonical period). Found & fixed a
scaling bug: `xbrl_facts._scale` hardcoded ÷1000 (right for SPORT `miles_mxn`, 1000× too large for
WALMEX/LACOMER `millions`) — now company-unit-aware via `pesos_per_unit_for(cfg)`. Result: the
`xbrl` tier answers 2021+ **revenue, gross_profit, operating_income at 100% / 0.00% error** for all
three companies. EBITDA and KPIs are not IFRS concepts → they stay on rules. Suite: **437 passed,
1 skipped**; excels regenerated (now XBRL-backed for 2021+), revenue still <0.1% vs GT.

**Key reclassification — much of the residual "inaccuracy" is ground-truth, not extraction.**
- SPORT `ebitda` "32%" was a **mapping error**: headline EBITDA is reported *with* IFRS 16 but was
  compared to the *pre*-IFRS-16 GT row. Corrected (`ebitda`→post, `ebitda_sin_ifrs`→pre).
- SPORT `clientes_activos` mismatches are **GT restatements** — the extractor matches the
  as-reported figure (e.g. 3Q24 "alcanzó los 86,110"); GT (106,740) reflects a later definition
  change. Not fixable from source; should not be overfit.
- SPORT pre-2019 post-IFRS-16 EBITDA in GT is a **retroactive model construct** never present in the
  source PDFs.
- LACOMER `stores_city_market_cafe` "misses" are mostly quarters where the format didn't exist
  (GT = 0, row absent).

**Genuine extraction bugs still open** (need deeper, iterative per-era work): WALMEX regional
metrics pre-2023 coverage; WALMEX `total_stores` 2014–16 overcount; LACOMER `revenue` 3Q16 outlier;
SPORT `ebitda_sin_ifrs` two-column corruption. Tracked in `tasks/index.md` (Phases C–E).

---

## Phase 3 detail — per-tier and per-metric

### The cascade's biggest structural hole: Tier 1 is dead
Across **all three companies the `xbrl` tier answers 0 metrics.** The harness/pipeline look for a
`<stem>_facts.json` sibling next to each report `.md`; none exist under `data/reports/`. So every
2021+ quarter that *could* be answered authoritatively from structured XBRL instead falls back to
regex/table guessing. This is the single highest-leverage fix (checklist T1.1–T1.3).

### Per-tier accuracy (source tag that produced each answer)
| Company | xbrl | table | prose |
|---|---|---|---|
| SPORT   | 0 answered | 94 @ 81% | 146 @ 72% |
| LACOMER | 0 answered | ~290 @ 97% | ~97 @ 91% |
| WALMEX  | 0 answered | ~275 @ 88% | ~80 @ 69% |

Prose is the noisiest tier everywhere; the table tier is materially more accurate where it fires.

### SPORT — `--tiers xbrl,table,prose`
| metric | coverage | accuracy | worst err | issue |
|---|---|---|---|---|
| revenue | 100% | 98% | 6.85% | good |
| revenue_memberships | 65% | 100% | 0.65% | coverage |
| revenue_sports | 20% | 88% | 99.9% | coverage |
| revenue_sponsorships | 65% | 65% | 10% | accuracy |
| **ebitda** | 100% | **32%** | 938% | **pre- vs post-IFRS-16 mismatch** |
| **clientes_activos** | 98% | **64%** | 20,630 | wrong figure variant (avg vs EOP) |
| clubs_count | 100% | 82% | 4 | minor count drift |
| net_churn | 51% | 100% | — | coverage |
| gross_churn / visits_per_member | partial | high | — | coverage |

### WALMEX — headline good, segments + store counts weak
| metric | coverage | accuracy | issue |
|---|---|---|---|
| gross_profit, operating_income | 100% | 100% | good |
| revenue | 100% | 92% | good |
| ebitda | 100% | 95% | good |
| revenue_mexico / revenue_cam / sss_mexico / sss_cam | **16–18%** | 100% when found | **regional patterns miss** |
| **total_stores** | 98% | **48%** | 118–130-unit overcount 2014–16 |
| total_stores_mexico | 94% | 57% | same era overcount |
| total_stores_cam | 94% | 85% | minor |

### LACOMER — strong, with a few sharp outliers
| metric | coverage | accuracy | issue |
|---|---|---|---|
| stores_la_comer / fresko / city_market / sumesa | 100% | 100% | good |
| revenue | 100% | 97% | **3Q16 191% outlier** (10,829 vs 3,713) |
| sales_floor_total | 97% | 84% | early-quarter scale errors |
| sales_floor_la_comer | 100% | 82% | up to 9.9% off |
| sales_floor_city_market / fresko | 100% | 94–97% | a few 26% spikes |
| total_stores | 70% | 87% | coverage |
| **stores_city_market_cafe** | **9%** | 100% when found | **label rarely matched** |

---

## Phase 6 — Excel output (fixed & verified)
`excels/WALMEX.xlsx`, `excels/LACOMER.xlsx`, `excels/SPORT.xlsx` generated via
`scripts/build_excels.py` (SPORT currency scaled ÷1000 miles→P$mn). Revenue cross-checked vs ground
truth for the latest 5 quarters: **all three companies <0.1% error**. New segments specs:
`configs/lacomer_segments.yaml`, `configs/sport_segments.yaml` (WALMEX already had one).

---

## Blocked — needs IR "reportes" subpage links
Ground truth exists but no reports are downloaded for:
- **BIMBO**, **LIVERPOOL** — configs are XBRL-annual only; quarterly reportes URL required.
- **CHEDRAUI** — `configs/chedraui.yaml` has a URL the downloader can attempt, but a confirmed
  reportes link avoids a failed crawl.

Once links arrive: download → parse → add a `metric_map` to `compare_extractions.COMPANIES` →
author a `*_segments.yaml` → `scripts/build_excels.py <co>`.

# Extraction Layer — Living Roadmap

The extraction layer is a **never-ending surface**: every new issuer, format era, and
report quirk is a fresh improvement. This file turns that open-ended reality into a
managed, prioritized backlog. It is the single forward-looking index for the root
extraction engine (previously scattered across `NIGHT_LOG.md` and
the extraction-cascade contracts exercised by the current tests).

**Conventions.** Each item carries `{leverage, effort, risk}` (low/med/high) and points at
the exact file/seam. Check items off as they land; add new ones freely — that's the point.
Ranked P0 (highest leverage) → P4.

_Last updated: 2026-07-06. Current root engine: ~17 companies onboarded, 9 with checked-in
ground-truth certification; soft universe = 138._

---

## P0 — Systemic infra / generalization
_Reduces the per-company cost of every future onboarding — the best leverage for a perpetual task._

- [x] **`[statement]` confidence band.** `score_confidence` had no `[statement]` branch, so
  every custom-extractor read fell through to the `[table]` band (0.8) despite `pipeline.py`
  treating `[statement]` as authoritative. Added an explicit branch scoring it like `[xbrl]`
  (0.95, or 1.0 when a passing identity involves it). `src/shared/validator.py`. `{leverage: high, effort: low, risk: low}` — done 2026-07-06.
- [x] **Generalize column orientation.** Added two reusable primitives to
  `src/extract/table_periods.py`: `orient_by_growth(a, b, growth_pct)` (the printed-Δ% value
  tiebreaker the module lacked) and `current_column_in_header_line(header_line, target)`
  (header-line → current-column index, matching quarter-tags **or** plain years — the
  year-only case `select_value_for_period` can't do). `gmexico.py` now delegates its `_orient`,
  `_orient_idx`, and Pass-2 inline year-match to them (behavior-preserving: 44 tests + 107/107
  GT-cell diff unchanged). `{leverage: high, effort: med, risk: med}` — done 2026-07-06.
  **Follow-up:** point `lab.py::_current_prior_from_value_margin_row` at `orient_by_growth`
  (identical err-compare) — deferred until LAB's cert can be re-run (iCloud-blocked now).
- [x] **Shared statement-block utilities — safe core.** Created `src/extract/statement_utils.py`
  as the canonical home: re-exports `parse_number` (already the shared scalar parser — 4
  extractors import it), plus new `repair_split_digits` (from gmexico `_repair`) and
  `looks_like_year` (unifying lab `_looks_like_year` + orbia's inline guard, `lo` param
  preserving both bounds). Migrated gmexico/lab/orbia onto them (verified: 123 tests incl.
  lab/orbia end-to-end + 107/107 gmexico GT diff). Audit finding: the *rest* of the per-company
  number helpers are **deliberately divergent, not duplicated** (`gmexico._tokens` drops signs;
  `herdez.parse_num` rejects `$`/`%`; `soriana._despace` is one-number-per-line; `liverpool`
  bundles magnitude+currency scaling; `lab._NUM_RE` uses space-as-thousands) — merging them
  would regress a certified company, and none have unit tests to verify a swap under the
  iCloud/cert block. `{leverage: med, effort: med, risk: low}` — done 2026-07-06.
  **Follow-up (deferred, needs characterization tests + cert access):** fold
  soriana/herdez/liverpool onto the shared module where a flagged superset can reproduce their
  exact behavior.
- [ ] **`statement_block` walker framework.** The higher-leverage arc: a config-driven block
  walker (header-orientation via `table_periods` + prior-capture + Q4 quarterly-vs-FY policy +
  the `statement_utils` primitives) so the next company needs a **config, not a bespoke file**.
  `{leverage: high, effort: high, risk: med}`

## P1 — Environment reliability
_Unblocks reliable validation of everything below._

- [x] **iCloud dataless-file eviction — mitigation shipped.** Added `src/shared/materialize.py`
  (`brctl download` + bounded `cat` force-read; `is_dataless`/`dataless_count` via `stat -f %Sf`)
  and `scripts/materialize.py` (warm the code tree, a `<slug>` corpus, or `--all`, with
  before/after counts). `tests/conftest.py` now materializes each corpus file before reading
  it (the old `exists()` skip didn't catch dataless files, which still `exists()`) + an opt-out
  session warm. Docs in `docs/DEV_SETUP.md`. **Honest caveat:** measured fetch is ~90 s/file
  (Apple's sync throttle) — the tool makes runs *complete instead of hanging*, it can't make
  them *fast* while on iCloud. `{leverage: high, effort: low, risk: low}` — done 2026-07-06.
- [x] **Permanent fix — DONE (2026-07):** the working checkout moved to a
  non-iCloud-synced disk, so eviction no longer occurs and `scripts/materialize.py` is
  no longer needed for normal work. `docs/DEV_SETUP.md` is kept as historical reference.

## P2 — Accuracy depth

- [ ] **Generic tier matches "Other Accounts Receivable/Payable" over the primary rows.**
  Found 2026-07-28 by the first live DeepSeek cross-check on soriana 2025-1T:
  `accounts_receivable` extracted 6,285 ("Other Accounts Receivable") vs the true Trade
  row 1,056; `accounts_payable` extracted 3,739 ("Other Accounts Payable") vs Suppliers
  22,866. Outside the certified outline (so certification never saw it), but soft's
  `industrial_fundamentals` consumes both keys → pollutes working-capital lines fleet-wide.
  Fix the alias/row-priority so "Other …"-prefixed rows never outrank the primary row.
  `{leverage: high, effort: low, risk: low}`

- [ ] **`test_no_regression` baseline is stale (2026-07-02) and femsa scores 0 observations.**
  As of 2026-07-28 the test fails for femsa/herdez/kimber against the July-2
  `tests/fixtures/scorecard_baseline.json`. Verified NOT caused by the 2026-07-28 reliability
  sweep (identical scores with the quarantine change reverted). Breakdown: herdez GT was
  intentionally expanded (105 → 269 cells; 258 correct, 2 FAIL: 3Q24A equity_associates
  grabs 9,297 vs 32; 4Q24A revenue 3,378 vs 9,897); kimber has 2 real quality FAILs
  (3Q21A revenue=2.0 garbage `[search]` hit; 4Q25A `[bmv]` 13,770 vs 14,058);
  **femsa's `FEMSA_Model_post1Q26_Segments.csv` yields ZERO comparable cells** (was 20 at
  baseline) — its metric_map/GT-label matching is broken and needs its own diagnosis.
  Fix the femsa GT wiring + the 4 FAIL cells, then regenerate the baseline (command in
  `tests/test_no_regression.py` docstring). `{leverage: med, effort: med, risk: low}`

- [ ] **Real embeddings behind the semantic seam.** `src/extract/semantic_search.py:1-6`
  documents the seam; `SemanticMatcher.best_match` (lines 210–229) is the injection point.
  A MiniLM `Embedder` already exists at `alpha-go/src/index/embeddings.py`
  (all-MiniLM-L6-v2 + hashing fallback) — lift it behind the seam. Gate behind
  `scripts/measure_semantic_search.py lacomer sport walmex --details` (must not regress). `{leverage: med, effort: med, risk: med}`
- [ ] **Expand per-company ground truth.** Only 9 of ~17 onboarded companies have a
  `data/ground_truth/*-actual.csv`; widen stratified periods on the thin ones so accuracy
  claims rest on more than a handful of cells. `{leverage: med, effort: med, risk: low}`

## P3 — Coverage breadth & corpus debt

- [ ] **Onboard more companies to the certified root engine.** Deep root extraction covers
  ~9–17 vs a 138-company soft universe. Use `/onboard-company`; prioritize issuers whose
  soft coverage already flags filing-side gaps. `{leverage: med, effort: high, risk: low}`
- [ ] **Corpus regeneration** to retire the `_FN` / `(\d{2})\d*` split-digit hacks
  (see the current cascade tests). `{leverage: med, effort: med, risk: med}`
- [ ] **Download missing source reports** for bimbo / chedraui / liverpool — they have
  `actuales/` CSVs but no on-disk reports, so they are currently untestable. `{leverage: low, effort: low, risk: low}`

## P4 — GMEXICO polish
_Completeness of the just-built deliverable (STRONG + 107/107); optional._

- [ ] **Mining volumes** (`vol_cobre` / `molibdeno` / `zinc` / `plata`) — deferred because
  the prose phrasing (and quarterly-vs-accumulated) varies by era. Needs quarter-anchored
  prose extraction (SPORT-style; see memory `project_dictionary_lever`). `{leverage: low, effort: med, risk: med}`
- [ ] **2019-4T consolidated recovery** via an FY−9M derivation (the report has only
  subsidiary statements + full-year prose; currently blanked). `{leverage: low, effort: med, risk: med}`
- [ ] **`net_debt_to_ebitda`** as a derived trailing-4Q Excel formula (dropped from the
  outline because the prose/calc value was noisy). `{leverage: low, effort: low, risk: low}`

---

## Fork status (2026-07-28)

**root ↔ soft: RECONCILED.** Two-way merge completed 2026-07-28: soft-side improvements
back-ported into root (`xbrl_facts` USD detection/conversion + per-filing millions sensing +
`_PREFER_NONZERO`, gzip-at-rest `bmv_xbrl` with `_logical_stem`, `minority_interest` metric +
concepts, femsa `shares_per_unit`), then `soft/scripts/vendor_sync.py --sync` re-vendored root
into soft. The two currency regimes are config-switched (`xbrl.currency_mode: convert_to_mxn`,
injected by soft's `fundamentals.py` at call time; root default = native currency + filter).
`vendor_sync.NET_NEW` now protects ALL soft-only modules (`market_data`, `macro`, `cnbv`,
`bank_ratios`, `fibra_kpis`) — `cnbv.py` was lost once to the unprotected sync and
reconstructed against `soft/tests/test_cnbv.py` (parse validated byte-identical vs the cached
table). Soft test suite green post-sync (325 passed). Post-sync sample rebuild: walmex,
banco_del_bajio, gcc, orbia PASS (statuses restored once rebuilt WITH network — the offline
`--no-network` rebuild blanks live-price cells and reads as INCOMPLETE, an artifact, not a
regression); grupo_bafar/gfnorte INCOMPLETE for the pre-existing no-Bloomberg-pack reason.
**Pending:** networked rebuild of femsa / fibra_uno / cemex (their status files still show the
offline-artifact INCOMPLETE) — one command from `soft/`:
`for s in femsa fibra_uno cemex; do ~/.venvs/alpha-go/bin/python scripts/build_coverage.py inputs/$s.md; done`

- [ ] **Expand the zero-shot pilot** (`scripts/zero_shot_eval.py`, `docs/ZERO_SHOT.md`) from 8
  to all 36 unconfigured estate slugs once the "Other Accounts Receivable/Payable" alias fix
  lands; track the DeepSeek agreement rate as the cold-start KPI. `{leverage: high, effort: low, risk: low}`

## Known divergence — root `src/download/` vs `alpha-go/src/download/` (recorded 2026-07-28)

The two trees are a **two-way fork**, not copies; neither is a superset. Any future
consolidation must merge in both directions:

- **alpha-go only:** English-only language filtering (`_filter_anchors_by_language`,
  `language`/`lang_include`/`lang_exclude` params) — note **22 root configs carry
  `company.language: en` that the ROOT downloader silently ignores**; the
  `_scoped_download_context` ContextVar scoping; an EDGAR adapter (`edgar.py`); and
  `download_ticker(filings=, kinds=)` + `--kind` on `bmv_xbrl.py`.
- **root only:** `download_from_url_templates` (sole download path for Grupo México);
  the Playwright `domcontentloaded`+settle navigation fix (alpha-go still uses
  `networkidle`, which never fires on SPA IR sites like ircuervo.com); the
  `_slugify_url` `{year}/{qN}/` path-context prefix (prevents Orbia's identical
  per-quarter filenames from colliding); and the 2026-07 hardening pass (curl-fallback
  `raise_for_status`, tmp-file cleanup, template PDF validation, pre-floor-year
  `--force-refresh` preservation).
- `wayback.py` is currently **byte-identical** in both trees — it will silently drift
  the moment either side touches it.

Other download-layer items deliberately left as-is (recorded, not fixed):
`_should_fallback_to_curl`'s broad token list (bare "ssl"/"tls" substring match);
HEAD requests get no urllib3 retries (`allowed_methods=["GET"]`); restated/Dictaminado
re-issues are never picked up without `--force-refresh` (canonicalize skips existing
targets); `docs/descargar_reportes.sh` hardcodes 40 exact SPORT filenames (most
rot-prone artifact in the layer).

---

### Already done — do not re-propose
XBRL Tier-1 consumption + 4-tier cascade; superscript/footnote stripping; `[xbrl]`/`[llm]`/
`[statement]` confidence bands; consolidated-region section scoping; USD-reporter currency
handling; per-table era-flipping orientation *inside* `gmexico.py`; minority-interest /
bank-ratio / FIBRA native extraction (soft side). Companies onboarded + certified: see
`docs/NIGHT_LOG.md`.

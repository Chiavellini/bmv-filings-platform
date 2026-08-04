# Company Onboarding Runbook

**Goal:** take a company from a raw metric list to a rich Excel workbook that is **STRONG through
the verification gate** AND **accuracy-certified against independent ground truth**. This is the
exact, frozen process used to ship Soriana (68 metrics) and Herdez (118 metrics) at 100% certified
accuracy. A freshly-spawned agent should be able to follow this end-to-end with no other context.

> Invocable as the `/onboard-company` skill (`.claude/skills/onboard-company/`). This doc is the deep
> reference; the skill is the lean orchestration checklist.

## Principles (read first)
1. **Reuse the built infra; don't reinvent.** Everything runs through `scripts/build_segments.py`
   (outline → cascade → workbook → gate) and `scripts/verify_extraction.py` (the gate loop). Do not
   hand-build per-metric scripts or a parallel pipeline.
2. **The Excel is the CSV in the specified format.** "Specified format" = the shared visual THEME,
   NOT a fixed structure. Each company's workbook structure reflects ITS OWN metric list. Different
   lists ⇒ different workbooks; identical lists ⇒ identical workbooks (not a bug).
3. **Phase-gate the corpus before trusting accuracy.** Confirm download + parse coverage vs the
   reports you expect BEFORE reporting extractor accuracy — never score a broken corpus.
4. **Accuracy-first, honesty-first.** Where the raw extractor is wrong, FIX it or record an honest
   override; never inflate a number. Ground truth is transcribed blind to extractor output.

## Stages at a glance
| # | Stage | Entry point | Done when |
|---|---|---|---|
| 0 | Intake | — | slug + metric list + IR URL in hand |
| 1 | Outline | `inputs/<slug>.md` | compiles with all keys pinned |
| 2 | Reports cache | `data/reports/<slug>/` | parsed `.md`+`.pdf` present for target periods |
| 3 | Config | `configs/<slug>.yaml` | from `_TEMPLATE.yaml`, keys set |
| 4 | Custom extractor (if needed) | `src/extract/<name>.py` + `_CUSTOM_EXTRACTORS` entry | custom keys appear in output |
| 5 | Build + gate loop | `scripts/verify_extraction.py <slug>` | gate verdict **STRONG** |
| 6 | Accuracy certification | `python3 -m src.eval.compare_extractions <slug>` | ≥95%/≥95% (aim 100%) on the sample |
| 7 | Tests + regression | `pytest` | new tests + batch green |
| 8 | Memory + handoff | Claude project memory | recorded |

---

## Stage 0 — Intake
You need four things from the user:
- The **original analyst metric sheet** (`.csv` or `.xlsx`), not only a transcription or summary.
  It is hashed into the build manifest and its ordered labels are compared with the executable
  outline before extraction starts.
- A **themed metric list** (sections + line items). Each line is either a *reported* number or a
  *derived* one (YoY, margin, % of total, check).
- The **IR URL** (for downloads / provenance).
- The **slug** = the lowercase H1 of the input file (see Stage 1 gotcha).

If the metric list is ambiguous (sample size, which segments), ask. Do not start extraction before
the list is settled — the list defines the workbook.

---

## Stage 1 — Outline → `inputs/<slug>.md`
Translate the metric list into the lightweight outline grammar (parsed by
`scripts/build_segments.py`):

```markdown
# Herdez                ← H1 = company name; slugifies to the slug (see GOTCHA)
IR: https://grupoherdez.com.mx/investors-financial-information#reportarchive
Analyst-Metrics: requests/Metrics.xlsx#Requested Metrics
Analyst-Company: HERDEZ

## Net Sales            ← "## Section" header
- Consolidated Net Sales {revenue}   ← "- Label {key}" = a PINNED data row
- YoY                                ← derived token → live YoY formula
- Domestic {ns_domestic}
- As % of Consolidated               ← derived token → % formula
- Export {ns_export}
- Check                              ← section-scoped: =revenue − SUM(domestic, export)
```
(No `Eliminations` row — Herdez never discloses it, so it stays out of the outline.)

Every reported row must carry an explicit `{metric_key}` pin. Exact-name auto-resolution is useful
only while drafting and is rejected by strict onboarding. `--allow-auto-map` is an exploratory
escape hatch, never a shipping mode.

The analyst sheet and outline are an ordered contract: sections, reported rows, and requested
derived rows must match 1:1 after whitespace/case normalization. Row roles cannot be changed (a
requested metric cannot become a section), and any nonblank analyst `key` cell must match the
Markdown pin. You may supply the source with
`--analyst-metrics PATH --analyst-company MARKER --analyst-sheet WORKSHEET` instead of the metadata
lines above. A mismatch writes `outputs/<Company>/validation/<slug>_metrics_manifest.json`, aborts
before download/extraction, and leaves `outputs/latest/` unchanged.

**Derived rows are LIVE Excel formulas, never hardcoded.** Every supported derived token below renders
a `=…` formula computed from workbook data rows. On the onboarding path, analyst-requested rows are
never silently pruned: a missing input makes the Excel audit fail and prevents publication.
Recognized by `_derived_token` (`scripts/build_segments.py`) +
`_classify_derived`/`build_outline_workbook` (`src/excel/segments_sheet.py`):

| Token (outline label) | Renders | Inputs required |
|---|---|---|
| `YoY` | this ÷ same-quarter-prior-year − 1 | the data row above (+ a prior-year column) |
| `YoY <X>` (e.g. `YoY Sales per m²`) | YoY of the row labeled `<X>` | that row |
| `Margin` | metric ÷ its segment's net sales (consolidated → revenue; `gp_<seg>`→`ns_<seg>`; `megamex_*`→`megamex_net_sales`) | the metric + its sales base |
| `bps change` | (margin − prior-year margin) × 10000 | the `Margin` row above |
| `As % of Consolidated` | segment ÷ the section's consolidated (family-aware: `gp_domestic`/`gross_profit`) | the family consolidated key |
| `As % of Total` | per-format ÷ the GLOBAL total (`units_*`→`total_units`, `area_*`→`total_sales_floor`) | `total_units`/`total_sales_floor` |
| `2-year comp` | stacked comp of a rate: (1+this)(1+prior-yr)−1 | the rate row above (e.g. `sss`) |
| `Effective Tax Rate` | `tax_expense / ebt` | both keys |
| `Avg Store Size` | section's area row ÷ its units row | an `*area*` + `*units*` row in the section |
| `Sales per m²`/`Sales per Store` | `revenue` ÷ `total_sales_floor`/`total_units` | those keys |
| `Capex per m²`/`Capex per Store` | `capex` ÷ `total_sales_floor`/`total_units` | those keys |
| `Check` | section consolidated − Σ(same-family segments **in this section**) | a family consolidated + ≥1 segment, same section |

To add a new ratio token, extend `_RATIO_DERIVED` (and `_derived_token`). Anything with a `{key}` pin
is a data row; an unrecognized/unpinned label fails the strict metric contract before extraction.

**Do NOT list unreachable metrics in the analyst request.** If a requested item is genuinely not
disclosed, settle that scope with the analyst instead of silently removing it during implementation.
The Stage-5 Excel audit fails on any requested row it cannot faithfully render.
`inputs/<slug>.md` must equal both the analyst sheet and rendered workbook 1:1.

**Keys:** reuse base keys where they exist (`revenue, cogs, gross_profit, operating_expense, ebitda,
depreciation, operating_income, interest_expense, ebt, tax_expense, net_income, capex` …). Invent new
snake_case keys for company-specific lines (segments, KPIs) — these become `custom_metrics` in Stage 3.

> **GOTCHA — the H1 IS the slug.** `slugify` (`build_segments.py:285`) turns the H1 into the slug used
> to find `configs/<slug>.yaml` and `data/reports/<slug>/`. Use `# Herdez` (→ `herdez`), NOT
> `# Grupo Herdez` (→ `grupo_herdez`, which silently misses the config and the cached reports and
> triggers a fresh download). Match the H1 to your existing config/reports dir name.

Exemplars: `inputs/herdez.md`, `inputs/soriana.md`.

---

## Stage 1.5 — Source binding (any of the 179 roster issuers)

```bash
.venv/bin/python scripts/onboard_source.py <slug>            # inspect / canary
.venv/bin/python scripts/onboard_source.py <slug> --apply    # write the ir: overlay
```

Picks the right mode for the issuer's state: **canary** for the 25 bound issuers (fetches the newest
expected quarter through the configured production adapter) and **discovery** for the rest (proposes
an `ir:` overlay from BMV directory seeds, then with `--apply` writes it, stamps
`live_verified_period/on/url`, and re-canaries). It wraps `scripts/discover_ir_sources.py`, whose
two modes have mutually exclusive flags — `--verify-live` needs `--staging-dir` *and* an exact
`--expected-period` and refuses to run with `--apply`.

**An unbound issuer is not a blocker.** `configs/issuers.yaml` carries a ticker for all 179, and
`registry._sources_from_raw` auto-creates a `bmv_xbrl` source for every one of them, so the BMV XBRL
archive covers any issuer from ~2021 on with no configuration at all. A binding is what buys
pre-2021 quarters and the issuer's own earnings-release PDFs.

Configuration is resolved by `src/shared/company_source.resolve_company_source`, which merges the
three surfaces that describe the same thing — `configs/<slug>.yaml` → `ir_website:`, then
`configs/issuers.yaml` → `overlays.<slug>.ir`, then the roster entry. That is why Stage 2 now works
before Stage 3 exists, and why `configs/orbia.yaml` / `configs/grupo_mexico.yaml` (an `ir_website:`
block with no `url:`) no longer raise `KeyError`.

⚠️ A source's `coverage_from_period` states how far back **that binding** was verified. It is not
the issuer's listing date and must never be read as "we have everything since then".

---

## Stage 2 — Canonical acquisition + zero-copy report inputs

Do not infer freshness from a populated cache. First configure the issuer/source in
`configs/issuers.yaml` (Stage 1.5), live-certify its newest report URL, then run the canonical
company-scoped updater:

```bash
.venv/bin/refresh-quarterly-estate plan --only <slug> --json
.venv/bin/refresh-quarterly-estate sync --only <slug> --apply --json
```

The applied command must exit zero. A partial/failing discovery, unresolved due period, failed PDF,
or trailing-quarter recheck that observes nothing is not a successful freshness receipt.
`build_segments` requires a successful non-backfill receipt for the exact issuer from the last 24
hours. `--allow-stale-estate` exists for offline/exploratory work and is recorded in the metric
manifest; it is not a shipping mode. A fresh run receipt alone is insufficient: the publication
gate hash-binds the exact effective extraction files to their current catalog document/artifact
lineage and run watermark. An unrefreshed compatibility view, superseded derivative, or local copy
therefore produces a candidate only.

Process newly stored originals through the derivative worker and refresh the compatibility view as
specified in `docs/DOCUMENT_ESTATE.md`. Extraction then reads a zero-copy union of:

- `data/document_estate/views/reports/<slug>/` (PDFs, legacy Markdown, facts);
- `data/document_estate/views/parsed/<slug>/` (versioned acquisition derivatives); and
- transitional `data/reports/<slug>/` inputs, if present.

Duplicate periods are resolved using explicit directory/catalog version precedence. Ambiguous
uncatalogued derivatives fail rather than winning by filename accident.

**Phase-gate — now enforced, not advisory:**

```bash
.venv/bin/python scripts/report_coverage.py <slug>                     # exits 1 below 90%
.venv/bin/python scripts/report_coverage.py <slug> --require-narrative # prose-dependent metric lists
```

One row per expected quarter with the source backing it, and a non-zero exit below
`--min-coverage`. Sources, strongest first:

| source | meaning |
|---|---|
| `pdf` | the issuer's earnings-release PDF, parsed to Markdown |
| `pdf-raw` | PDF on disk, **never parsed** — certification globs `*.md` and scores it zero; re-run the fetcher with `--parse` |
| `mdna` | narrative rendered from the XBRL filing's MD&A text blocks |
| `facts` | tagged IFRS concepts only, no narrative |
| `—` | nothing |

Expected periods run from the floor year (or the registry's `listed_from`) through the last
completed quarter, so a post-2016 IPO no longer reports phantom gaps.

The offline half of the corpus is produced by
`.venv/bin/python scripts/fetch_company_reports.py <slug> --parse`, whose last step calls
`src/download/xbrl_corpus.materialize_xbrl_corpus`: it hardlinks each filing's facts to
`<period>_facts.json` and renders its MD&A to `<period>.md` **only where no PDF exists** (the PDF
parse owns those periods), then writes `provenance.json` recording every period's true source.
Run it standalone with `scripts/materialize_xbrl_corpus.py <slug>`.

⚠️ **Materialization is bootstrap-only by default** — it fills a directory holding no PDFs and no
Markdown, and otherwise records provenance without writing. That is the corpus phase-gate, not
timidity: making previously-invisible facts and MD&A readable *adds observations*, and uncertified
observations move pinned baselines. Materializing into corpora that already had reports raised the
FAIL count for `lab`, `chedraui` and `femsa` in `tests/test_no_regression.py`. To extend an existing
corpus, pass `--fill-gaps` (standalone) or `--fill-xbrl-gaps` (fetcher) **and re-certify the
company**.

The MD&A HTML is Aspose.Words output in which every styled run is its own `<span>`, so BeautifulSoup
splits words and numbers mid-token (`202\n1`, `5.4 v eces`). `xbrl_corpus.render_mdna_html` rejoins
inline runs with no separator and breaks lines only at real block boundaries, emitting table rows as
`cell | cell | cell`. Do not substitute `load_mdna_text` — it is the unrepaired renderer and is kept
as-is so `_from_bmv_xbrl` behaviour (and the numbers it has already certified) does not shift.

A missing/garbled quarter is a corpus problem, not an extractor problem. Wayback is for explicit
historical backfill, never the recurring freshness path.

---

## Stage 3 — Config → `configs/<slug>.yaml`
Copy `configs/_TEMPLATE.yaml` → `configs/<slug>.yaml`. Every key, what it does, and the code that
reads it:

| Key | Meaning | Consumed by |
|---|---|---|
| `company` | name/ticker/currency/exchange/language + `unit` (millions/thousands) | `build_segments`, `pipeline` |
| `custom_extractor` | registry key in `_CUSTOM_EXTRACTORS` → `src/extract/<name>.py` (need not equal slug) | `tiered_extract.py` |
| `custom_metrics` | new keys (key/label/section/unit/optional patterns) | `financial_model.apply_config` |
| `metric_overrides` | extra patterns / aliases / identity `calc` on a base metric | `apply_config` |
| `metric_expectations` | per-key `window` / `sparse` / `sign` / `expected_empty` / `range` | `verification_gate.score_metrics` |
| `exclude_metrics` | drop base metrics entirely (stops identity-poisoning) | `apply_config` |
| `reject_negative` | drop negative values for these keys | `pipeline.run` |
| `delta_metrics` | metric = Δ of another over prior period | `pipeline.run` |
| `tier_precedence` | rank tiers (default + per-key) | `tiered_extract._merge_rows` |
| `income_note` | header-aligned `[800200]` income-note segment rows (see below) | `income_note.extract_from_income_note` |
| `ir_website` | download tuning (url/playwright/impersonate/max_reports) | `build_segments._ir_options` |

A company the generic cascade extracts well needs only `company` (+ `custom_metrics` for its
bespoke rows). Reach for the rest as the gate (Stage 5) surfaces issues.

### BMV income-note (`[800200]`) segments — declare, don't hand-roll a positional regex
Many BMV filers disclose the revenue split only in the regulatory note
`[800200] Notas - Análisis de ingresos y gastos` (`Venta de bienes` → commercial, `Intereses` →
financial, `Arrendamiento` → real estate, plus `Total de ingresos`). **Do not** grab these with a
positional regex that captures a fixed column ("the 3rd number") — the note's column ORDER varies by
year/quarter and silently flips:

- `2024-3T`: `[Acum-cur, Acum-prior, Trim-cur (3rd), Trim-prior]` → "3rd" is correct
- `2025-3T`: `[Trim-cur (1st), Acum-cur, Trim-prior, Acum-prior]` → "3rd" is **prior-year** (wrong!)
- Q1: `[Acum-cur, Acum-prior]` → Q1 acumulado **is** the quarter

Instead declare an `income_note:` block; the `note` tier reads the column header
(`Trimestre/Acumulado` × `Año Actual/Anterior`) and selects the single-quarter Actual column whose
quarter matches the target, scaling full pesos → the company `unit`. It degrades to `{}` (so any
positional fallback still runs) whenever the header can't be confidently aligned.

```yaml
income_note:
  enabled: true
  code: "800200"                 # default; fallback_codes: ["310000"] rescues consolidated revenue only
  rows:                          # canonical metric key → exact note label, or a LIST summed together
    revenue_commercial:  ["Venta de bienes", "Servicios"]   # operating segment = sum of revenue types
    revenue_financial:   "Intereses"
    revenue_real_estate: "Arrendamiento"
    revenue:             "Total de ingresos"
# Rank it ahead of looser tiers for those keys:
tier_precedence:
  revenue_commercial: [xbrl, bmv, note, search, regex_table, prose, table, calc]
```

**Watch the basis.** An *operating segment* (what GT usually labels) is often the SUM of several
note revenue *types* — Liverpool's Commercial segment = `Venta de bienes` + `Servicios` (verified
14/14 within 2% vs GT; goods alone falls ~3% short in Q4 by the seasonal services amount). Map a key
to a list to sum; a single label that already matches GT (Financial = `Intereses`, Real Estate =
`Arrendamiento`) stays a string. Confirm the mapping against ground truth before trusting it.

**Traps:** never pass a YTD/`Acumulado` figure as the quarter (the tier emits nothing for YTD-only
Q2–Q4 filings — an honest gap); row labels are matched **exactly** after normalization, so
`Intereses` (revenue) is never confused with `Intereses ganados`/`Intereses devengados a cargo` or
`Total de ingresos financieros`. Reference: `configs/liverpool.yaml`, `src/extract/income_note.py`,
`tests/test_income_note.py`.

---

## Stage 4 — Custom extractor (CONDITIONAL)
**Build one only when** the generic cascade can't reliably recover the company's structure: segment
tables (per-division revenue/GP/EBIT/EBITDA), equity-in-associates, standalone-JV statements, or
rotated/transposed PDF tables, or bespoke prose the regex tiers miss. Otherwise skip this stage.

**Contract:**
```python
# src/extract/<slug>.py
def extract_<slug>(text: str, metric_defs, period: str | None = None,
                   pdf_path=None) -> dict[str, MetricRow]:
    ...  # return {canonical_key: MetricRow(...)} ; tag source_line with a tier, e.g. "[statement] ..."
```
`MetricRow` fields (`src/extract/extract_metrics.py:43`): `metric, label_es, current, prior, var_pct,
unit, source_line`. The `source_line` prefix is the tier tag the cascade ranks on (`[statement]`,
`[qcapex]`, …).

Do not create one extractor per company merely for identity. Prefer generic/config-driven tiers.
A company extractor is justified only for a stable structural exception (rotated tables, unusual
segment basis, bespoke note layout). It may return the legacy row mapping or an `ExtractionBatch`
from `src.extract.revisions` with typed `FactObservation` records.

Typed observations separate the period being measured from the report that asserted it. This is
the restatement contract: a 4Q release can explicitly revise 3Q, and a later version of the same
filing family can supersede an earlier value. Series identity includes period kind, accounting
basis, currency, unit, and dimensions, so a YTD/USD/segment observation cannot overwrite a
quarterly/MXN/consolidated cell. Trusted explicit revisions win; unsafe conflicts remain in
`df.attrs['revisions']` for review instead of being guessed away. Legacy `restated_prior` labels
such as `2Q21A` are normalized to production labels such as `2021-2T`; optional
`latest_comparative: allow` is restricted to trusted tiers, while `force` is an explicit override.

**Register the dispatch**: add ONE entry to the `_CUSTOM_EXTRACTORS` dict near the top of
`src/extract/tiered_extract.py`:
```python
_CUSTOM_EXTRACTORS = {
    ...
    # config value → (module path, function name, wants_period, wants_pdf_path)
    "<name>": ("src.extract.<name>", "extract_<name>", True, True),
}
```
The `custom_extractor:` config value is the dict key and **need not equal the slug** — e.g.
`configs/grupo_mexico.yaml` uses `custom_extractor: gmexico` → `src/extract/gmexico.py`. An
unregistered value prints a loud `unknown custom_extractor` warning on stderr instead of silently
doing nothing. Note: custom extractors run under the `search` tier gate, so any `--tiers` list that
omits `search` also disables them.

> **GOTCHA — confirm the registration actually landed.** Symptom: consolidated lines get filled by
> generic tiers with WRONG values and your custom keys are absent from the CSV (or the stderr
> warning above fires). Verify by extracting one period directly:
> `extract_metrics_tiered(src, defs, cfg)` should show your keys tagged `[statement]`.

**Porting from `archive/`:** if a standalone extractor exists (e.g. `archive/<co>_reportes/`), port
its engine verbatim and replace its file-loop/CSV-writer with the per-period `extract_<slug>` wrapper
that maps the archive keys → your outline keys.

**Pair it with `exclude_metrics`.** When the custom extractor supplies the consolidated lines, exclude
the WHOLE non-deliverable base set (cogs, margins, balance-sheet, cash-flow, ratios). A stray
generic-tier `cogs`/`total_assets`/`ebitda_margin` will fail `gross_profit_identity` /
`balance_sheet_identity` / `margin_consistency` and poison the confidence of the lines you keep. (This
single lever took Herdez's first-pass suspects from 107 → 11.)

> **GOTCHA — `statement` vs `bmv` precedence.** If the company reports a management basis
> (press-release "net sales") that differs from the regulatory IFRS line ("Ingresos"), set
> `tier_precedence.default: [xbrl, statement, bmv, ...]` so the management value wins. Herdez 1Q26 was
> 5,209 (management) vs 6,155 (IFRS) — the wrong order silently certifies the wrong basis.

---

> **The three-check robustness model** (`docs/ZERO_SHOT.md`): (1) the deterministic engine +
> validator/gate below; (2) an ADVISORY DeepSeek cross-check (`--crosscheck` on
> `src/extract/pipeline`, or `llm_crosscheck.enabled: true` in config) that independently
> re-extracts every produced cell and reports agreement — it never gates, but its disagreeing
> cells are the first ones to hand to Stage-5 verification subagents; (3) this manual
> verification loop. On a company with no ground truth, the check-2 agreement rate is the only
> accuracy proxy available.

## Stage 5 — Build + STRONG gate loop
```bash
python3 scripts/verify_extraction.py <slug> \
  --analyst-metrics requests/Metrics.xlsx --analyst-company TICKER
```
This runs the full pipeline (download/parse if needed → extract → workbook → validation →
**verification gate**) and writes `outputs/<Co>/validation/<slug>_worklist.json`. The gate
(`src/eval/verification_gate.py`) classifies each metric STRONG / WEAK / SUSPECT / EMPTY /
EXPECTED_EMPTY; **STRONG overall = no SUSPECT and no WEAK/EMPTY disclosed metric.** See
`docs/VERIFICATION.md` for the classification detail.

For the analyst, the file to open is always `outputs/latest/<Company>.xlsx`.
`outputs/latest/` contains exactly one workbook and is replaced by a serialized,
crash-recoverable swap only after the strict metric contract, rendered-row round trip, formula
evaluable-period audit, and STRONG verification gate all pass. The adjacent
`outputs/latest_manifest.json` binds the workbook SHA/build ID to its analyst sheet and estate
watermark. The
entire prior handoff directory moves to a timestamped location under
`outputs/archive/deliverables/`; it is never mixed with the new handoff or silently deleted. A weak
candidate remains under `outputs/<Company>/excel/`, cannot replace the current analyst file, and
returns CLI status 3 so automation cannot mistake it for a publication.

**The loop:**
1. **Triage systematic causes FIRST** (cheap, shrinks the manual set): a wave of negatives on a
   small segment → `metric_expectations: {key: {sign: any}}`; a legacy series outside its window →
   `{window: "…:…"}`; identity-driven cross-check flags on out-of-scope lines → `exclude_metrics`.
   Re-run.
2. For the residual worklist, **spawn one verification subagent per cell (or per period)**, blind to
   extractor output, reading `data/reports/<slug>/<period>.md` (+ `.pdf`). Use
   `.claude/skills/onboard-company/templates/verify_subagent_prompt.md`. Collect their verdicts into a
   JSON list:
   ```json
   [{"period":"2023-4T","key":"revenue","value":9809,"note":"4Q23 quarter column; 'Net Sales 9,809'"},
    {"period":"2017-3T","key":"ebitda","value":"UNRESOLVED","note":"table garbled in source PDF; checked md + pdf"}]
   ```
   Verdict outcomes: a **numeric value** = confirmed/corrected → pinned `[verified]` (green, no
   flag); `value: null` (or `"BLANK"`) = not disclosed → blanks any wrong extraction;
   `"UNRESOLVED"` = the subagent read the source and could not settle the cell → the extracted
   value ships with a **red-flag comment** stating verification was attempted, and the cell stops
   re-entering the worklist.
3. Apply + re-run:
   ```bash
   python3 scripts/verify_extraction.py <slug> --apply verdicts.json   # → data/verified/<slug>.csv
   python3 scripts/verify_extraction.py <slug>                          # rebuild + re-score
   ```
4. Repeat until **✅ STRONG**. Each pass must shrink the suspect count.

**Shipped-workbook rule (verify-before-flag):** a cell carries a red-flag comment in the deliverable
ONLY if verification could not resolve it (UNRESOLVED). Every other suspect must end the loop as
confirmed (pinned, green), corrected, or blanked. UNRESOLVED cells don't block STRONG — an
attempted-and-documented verification is an *explained* suspect — but they are counted in the
scorecard's **Unresolved** column so shipped flags stay visible.

> **GOTCHA — a magnitude-outlier flag is often a REAL low quarter,** not an error (e.g. a JV whose
> earnings dropped 75% YoY). Confirm against the report and `[verified]`-pin the true value to resolve
> it — don't "fix" a correct number.

`data/verified/<slug>.csv` (cols `period,key,value,note`) is the override layer: it reinforces the
shipped workbook (cells render green) at confidence 1.0, `BLANK` blanks an undisclosed cell, and
`UNRESOLVED` records a failed verification (value untouched; red-flag comment ships). It is
applied in `pipeline.run` (`_load_verified`), so it affects the deliverable but NOT the raw-extractor
certification in Stage 6.

---

## Stage 6 — Accuracy certification (REQUIRED for "done")
Certify the **raw extractor** (no override layer) against **independent** ground truth.

### 6a. Build independent ground truth (blind)
Pick **~8 stratified periods** spanning every format era (early/mid/recent + any layout shift + a Q4
for the annual-vs-quarterly trap). Spawn subagents (≈2–3 periods each) that read ONLY the reports and
transcribe true quarterly values, using
`.claude/skills/onboard-company/templates/groundtruth_subagent_prompt.md`. They must read the **PDF**
when the markdown table is garbled.

> **Blank genuinely-undisclosed cells** in the GT (derived-only figures, not-stated counts, ended
> legacy series). Excluding a cell means you don't certify what the report doesn't cleanly disclose —
> it is never used to inflate the number. NEVER reuse `data/verified/<slug>.csv` as ground truth: it
> is a biased (suspect-only) sample.

Write `data/ground_truth/<slug>-actual.csv` in the EXACT wide format (mirror
`data/ground_truth/herdez-actual.csv`):
```csv
Company Name,description line
Segment data,,,,,,,,
,Section (P$mn),2Q16A,4Q18A,3Q19A,3Q21A,1Q23A,2Q24A,2Q25A,1Q26A   ← header: NQ{YY}A tags (see parse_period)
,P&L,,,,,,,,                                                       ← section header (all period cells blank)
,Net Sales,4431,5848,5569,6767,8632,9297,9708,5209                ← data row: ,Label,v1,…
,EBITDA,662,1056,978,886,1316,1600,1575,810
```
Period tags map via `compare_extractions.parse_period` (`2016-2T` → `2Q16A`). Values in the unit
declared in `company.unit`.

### 6b. Register in `COMPANIES`
Add an entry in `src/eval/compare_extractions.py` (`COMPANIES` dict). `metric_map` maps each extractor
key → `(section, label, tol_type)` whose `(section, label)` EXACTLY match the GT CSV rows:
```python
'<slug>': {
    'config':      'configs/<slug>.yaml',
    'source_dir':  'data/reports/<slug>',
    'actual_file': 'data/ground_truth/<slug>-actual.csv',
    'metric_map': {
        'revenue': ('P&L', 'Net Sales', 'currency'),
        'ebitda':  ('P&L', 'EBITDA',    'currency'),
        # tol_type ∈ currency (2% / ±0.5 floor) | area (0.5%) | pct (0.5pp) | count (exact)
    },
    # 'currency_scale': 0.001,   # only if GT unit ≠ extractor unit
}
```

### 6c. Run + report
```bash
python3 -m src.eval.compare_extractions <slug> --tiers xbrl,bmv,note,search,table,prose
```
> **GOTCHA — module form is required.** `python3 src/eval/compare_extractions.py` crashes with
> `ModuleNotFoundError: No module named 'src'` (the script does not self-bootstrap `sys.path`;
> `scripts/verify_extraction.py` and `scripts/check_metric.py` do, which is why those two run as
> plain files). Always invoke it as `python3 -m src.eval.compare_extractions` from the repo root.

> **GOTCHA — pass the FULL tuned tier set, and keep it consistent across runs.** Include every
> tier the config relies on: `xbrl,bmv,note,search,table,prose`. Three ways this bites:
> 1. `_build_source` (`compare_extractions.py:608`) only wires `pdf_path` when `table` is enabled —
>    omitting it starves PDF-dependent extractors (rotated store tables) → false MISSes.
> 2. Omitting `bmv`/`note`/`search` starves the income-note segment tier and command-F search that
>    companies like Liverpool/Chedraui depend on. The 2026-06-22 scorecard used `xbrl,table,prose`
>    and badly UNDER-reported those companies (Liverpool showed 32% vs a true 61%). **Never compare
>    two accuracy numbers measured with different `--tiers`** — re-baseline first.
> 3. Custom extractors are gated under the `search` tier — a list without `search` silently
>    disables them (also true of `scripts/check_metric.py`, whose default is `xbrl,prose`).
>
> **Decision rule for `table`:** include it in the certification run whenever the company has a
> custom extractor that takes `pdf_path` or any table-tier-tuned metric; drop it only for companies
> whose config disables table extraction (liverpool/chedraui — it can stall on very large BMV
> filings), and in that case use the markdown tiers for the tuning loop and say so. **Record the
> exact tier list used in `<slug>_accuracy.md`** so future numbers are comparable.

Acceptance bar: **≥95% accuracy at ≥95% coverage, count metrics ≥90%, no quarter >10% off on a
headline metric** — aim **100%** on the sampled cells. Where a metric is below target: **fix the
extractor** (e.g.
Herdez 2026 needed a new prose parser + a precedence flip — see its history) rather than papering over
it, OR blank a genuinely-undisclosed GT cell. Then write `outputs/<Co>/validation/<slug>_accuracy.md`
(mirror `herdez_accuracy.md`): per-metric table, overall %, and the **raw-extractor vs deliverable**
note (the harness scores the raw extractor; `data/verified/<slug>.csv` reinforces the shipped sheet).

---

## Stage 7 — Tests + regression
- `tests/test_<slug>_extractor.py` — assert the custom extractor's values on one known clean period,
  any cross-era reconstruction (e.g. domestic = sum of sub-segments), and that `exclude_metrics`
  drops what it should. (Mirror `tests/test_herdez_extractor.py`.)
- `tests/test_compare_extractions.py` — add `<slug>` to the parametrized
  `test_registered_company_metric_map_labels_resolve` (guards section/label typos — the #1 silent
  registration failure).
- Run the batch:
  ```bash
  python3 -m pytest tests/test_<slug>_extractor.py tests/test_compare_extractions.py \
      tests/test_verification_gate.py tests/test_phase_contracts.py -q
  ```

---

## Stage 8 — Memory + handoff
Record what was non-obvious (new config levers, the custom extractor's quirks, residual disclosure
gaps) in the Claude project memory: write a fact file under
`~/.claude/projects/<project-key>/memory/` and add its one-line pointer to the `MEMORY.md` index in
that same directory (there is deliberately no `MEMORY.md` in the repo).

---

## Gotchas catalog (consolidated)
1. **H1 = slug.** `# Herdez` not `# Grupo Herdez`; must match `configs/<slug>.yaml` + `data/reports/<slug>/`.
2. **Confirm the registration landed.** An unregistered `custom_extractor` value now warns on
   stderr, but still verify keys come out tagged `[statement]` (a typo'd module/function name in the
   `_CUSTOM_EXTRACTORS` entry fails at dispatch, not at import).
3. **`--tiers …,table` for certification.** Without it, `pdf_path` is never wired and PDF metrics MISS.
4. **`statement` > `bmv` precedence** when management basis ≠ IFRS basis (else you certify the wrong basis).
5. **`exclude_metrics` stops identity-poisoning** — drop every non-deliverable base metric a custom
   extractor makes redundant.
6. **Magnitude outliers are often real low quarters** — confirm, then `[verified]`-pin; don't "fix" a
   correct value.
7. **Ground truth must be independent + blind + blank-undisclosed.** The `[verified]` overrides are a
   biased suspect-only sample — never reuse them as GT.
8. **Phase-gate the corpus** before reporting accuracy; a missing quarter is a corpus bug.

## Templates & references
- Config skeleton: `configs/_TEMPLATE.yaml`
- Gate loop detail: `docs/VERIFICATION.md`
- Phase map: `docs/PHASES.md` · architecture: `docs/architecture/pipeline_phases.yaml`
- Source/config exemplars: `{inputs,configs}/{herdez,soriana}.*`. Optional private
  comparison baselines and generated validation reports belong outside Git
  under `data/ground_truth/` and `outputs/`; behavior is retained in the test
  suite and synthetic fixtures.

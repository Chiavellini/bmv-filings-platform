---
name: onboard-company
description: Build/onboard a company's financial Excel deliverable from a metric list — outline → config → (custom extractor) → STRONG verification gate → independent accuracy certification. Use when the user gives a company's metric list + IR page and wants its rich Segments workbook, or asks to add/extract a new company, or to certify a company's extraction accuracy.
---

# Onboard a company (financial Excel deliverable)

Take a company from a raw metric list to a rich Excel workbook that is **STRONG through the
verification gate** AND **accuracy-certified against independent ground truth**. This skill is the
lean orchestration; the deep reference is **`docs/COMPANY_ONBOARDING.md`** — read it before/while
executing. Exemplars to mirror: Soriana and Herdez (`{inputs,configs}/{soriana,herdez}.*`).

## Operating principles
- **Python:** use the repo venv — `.venv/bin/python` at the repo root (Homebrew `python3` 3.14 has a
  broken `pyexpat`, which kills `openpyxl` imports → every Excel build). Commands below say
  `python3`; substitute `.venv/bin/python`.
- **Reuse the built infra.** Everything runs through `scripts/build_segments.py` +
  `scripts/verify_extraction.py` + `src/eval/compare_extractions.py`. Don't reinvent or hand-build
  per-metric scripts.
- **Analyst handoff:** after every completed build, tell the analyst to open
  `outputs/latest/<Company>.xlsx`. That directory contains exactly one file: the newest workbook.
  CSVs, validation evidence, and the retained company copy stay under `outputs/<Company>/`.
- The workbook structure reflects THIS company's metric list (shared theme, per-company structure).
- Phase-gate the corpus before trusting accuracy. Fix or honestly record; never inflate a number.

## Workflow (8 stages — see runbook for full detail)
0. **Intake** — get the themed metric list + IR URL + slug. Ask if the list/segments/sample are
   ambiguous. Don't extract before the list is settled. ⚠️ The slug must be a canonical issuer in
   `configs/issuers.yaml` (179 of them); check with
   `.venv/bin/python -c "from src.shared.company_source import resolve_company_source as r; print(r('<slug>'))"`.
1. **Outline** → `inputs/<slug>.md`. Grammar: `## Section`, `- Label {key}` (pinned), derived tokens
   (`YoY`/`Margin`/`bps change`/`As % of`/`As % of Total`/`Check`/`2-year comp`). Reuse base keys;
   invent snake_case keys for bespoke rows. ⚠️ **H1 = slug** (`# Herdez`, not `# Grupo Herdez`).
1.5 **Source binding** → `python3 scripts/onboard_source.py <slug>`. Reports whether the issuer has
   an IR binding and canaries it live. Only 25 of 179 issuers are bound; for the rest this proposes
   one from BMV directory seeds — inspect it, then `--apply` to write the `ir:` overlay into
   `configs/issuers.yaml` (it stamps `live_verified_*` and re-canaries). **An unbound issuer is not
   a blocker**: the BMV XBRL archive covers every ticker from ~2021 on. A binding is what buys
   pre-2021 quarters and the issuer's own earnings-release PDFs. If `--apply` finds no
   high-confidence proposal, hand-write `ir.url` + `ir.pdf_link_pattern` and re-run.
2. **Reports cache** → `python3 scripts/fetch_company_reports.py <slug> --parse` (key flags:
   `--max`, `--floor-year`, `--no-wayback`, `--no-xbrl`, `--jsonl`; ⚠️ `--force-refresh` deletes and
   re-downloads the refreshable artifacts — never needed for a first onboarding). Layers, in order:
   IR engine → annual section → `direct_url_templates` → Wayback → **BMV XBRL archive** → MD&A
   materialization. It resolves config from `configs/<slug>.yaml`, then the `issuers.yaml` overlay,
   then the roster, so it works before Stage 3 exists and for issuers with no IR binding at all.
   ⚠️ The MD&A materialization step is **bootstrap-only**: it fills a report dir that has no PDFs
   and no Markdown, and otherwise only records provenance. Extending a corpus that already has
   reports adds *uncertified* observations (it moved `lab`/`chedraui`/`femsa` against their pinned
   baselines) — opt in with `--fill-xbrl-gaps` and re-certify.
   **Then phase-gate for real:** `python3 scripts/report_coverage.py <slug>` — one row per expected
   quarter with its source (`pdf` > `pdf-raw` > `mdna` > `facts` > missing) and a **non-zero exit**
   below `--min-coverage` (default 0.9). Use `--require-narrative` when the metric list needs prose.
   `pdf-raw` means a PDF is on disk but was never parsed — certification globs `*.md` and would
   score it zero, so re-run with `--parse`. Do not proceed past a failing gate without saying, in
   the accuracy report, which periods are absent and why. `provenance.json` records every period's
   true source.
   ⚠️ For a *publishable* build the estate also needs an acquisition receipt (see Stage 5).
3. **Config** → copy `configs/_TEMPLATE.yaml` → `configs/<slug>.yaml`; set `company` + the optional
   levers (`custom_metrics`, `metric_expectations`, `exclude_metrics`, `tier_precedence`, …). For BMV
   filers' segment revenue, declare an `income_note:` block (header-aligned `[800200]` extraction)
   instead of a positional regex — the note's column order flips by year/quarter. See onboarding doc.
4. **Custom extractor (only if needed)** → `src/extract/<name>.py` returning `{key: MetricRow}` tagged
   `[statement]`; register it as one entry in the `_CUSTOM_EXTRACTORS` dict in
   `src/extract/tiered_extract.py` (the `custom_extractor:` config value need not equal the slug —
   grupo_mexico uses `gmexico`; an unregistered value now warns loudly on stderr instead of silently
   doing nothing). Still verify by extracting one period directly: keys must appear tagged
   `[statement]`. Pair with `exclude_metrics` (drop the non-deliverable base set) and
   `statement`-before-`bmv` precedence if basis differs.
5. **Build + STRONG gate loop** → `python3 scripts/verify_extraction.py <slug> --analyst-metrics
   <request.xlsx>`. ⚠️ **Two gates fire before extraction and both abort the build:**
   (a) *Analyst sheet* — `--analyst-metrics` (or an `Analyst-Metrics:` line in `inputs/<slug>.md`)
   is required; sections/rows must match the outline 1:1. (b) *Estate freshness* — the build
   demands a successful **non-backfill** acquisition receipt <24 h old for this issuer. The only
   command that writes one is `refresh-quarterly-estate sync --only <slug> --apply`
   (`.venv/bin/refresh-quarterly-estate`); `fetch_company_reports.py` does **not**. If the catalog
   reports `acquisition_runs_table_missing`, the ledger simply has never run here — it creates its
   own schema on first successful sync. `--allow-stale-estate` bypasses the abort but then
   *guarantees* `publish_blockers`: the build ends "⚠ REVIEW CANDIDATE", exit 3, and
   `outputs/latest/` is not updated. Use it for iteration, never for delivery. Optional but
   recommended: run the pipeline once with `--crosscheck` (advisory DeepSeek re-extraction;
   needs `DEEPSEEK_API_KEY`) and read the "LLM cross-check" section of the validation report —
   disagreeing cells are the highest-yield candidates for the subagent worklist even though they
   never block STRONG (see `docs/ZERO_SHOT.md`, three-check model). Triage systematic
   causes via config FIRST (sign/window/exclude), THEN fan out one **verification subagent** per
   residual worklist cell (blind to extractor output and using the source artifact resolved from
   the configured Estate catalog; prompt template:
   `.claude/skills/onboard-company/templates/verify_subagent_prompt.md`) → collect verdicts JSON →
   `--apply verdicts.json` → re-run. Loop until **✅ STRONG**. (A magnitude outlier is often a real
   low quarter — confirm, then `[verified]`-pin.) Verdict outcomes: **confirmed** = pin the value
   (green, no flag) · **corrected** = pin the fixed value · **not disclosed** = `"value": null` →
   BLANK · **source unreadable/ambiguous** = `"value": "UNRESOLVED"` → the extracted value ships
   with a red-flag comment. Shipped-workbook rule: a cell carries a flag comment in the deliverable
   ONLY if verification could not resolve it; UNRESOLVED doesn't block STRONG but is counted in the
   scorecard's Unresolved column.
6. **Accuracy certification (required for done)** → build INDEPENDENT ground truth: ~8 stratified
   periods spanning every format era; fan out **ground-truth subagents** (blind; resolve source
   artifacts from the configured Estate catalog and read the PDF when markdown is garbled; prompt template:
   `.claude/skills/onboard-company/templates/groundtruth_subagent_prompt.md`); blank genuinely-
   undisclosed cells. Write `data/ground_truth/<slug>-actual.csv` (exact wide format — mirror
   `herdez-actual.csv`), register `COMPANIES['<slug>']` in `src/eval/compare_extractions.py`, then run
   `python3 -m src.eval.compare_extractions <slug> --tiers xbrl,bmv,note,search,table,prose`
   (⚠️ module form — `python3 src/eval/…` crashes with ModuleNotFoundError; ⚠️ include `note` for any
   company with an `income_note:` block, and `table` or `pdf_path` is starved; record the exact tier
   list used in the accuracy report). Acceptance bar: **≥95% accuracy at ≥95% coverage, count metrics
   ≥90%, no quarter >10% off on a headline metric** (aim 100%); below → fix the extractor or blank a
   genuine non-disclosure. Fast inner loop for one metric:
   `python3 scripts/check_metric.py` (⚠️ its default is `--tiers xbrl,prose` — always pass the full
   tier list or results under-report). Write `outputs/<Co>/validation/<slug>_accuracy.md`.
7. **Tests** → `tests/test_<slug>_extractor.py` + add `<slug>` to the parametrized label-coverage
   test in `tests/test_compare_extractions.py`; run the regression batch.
8. **Memory** → record what was non-obvious in the Claude project memory (the auto-memory directory
   under `~/.claude/projects/<project>/memory/` — write the fact file and add its `MEMORY.md` index
   line there, not in the repo).

## When to pause and ask the user (AskUserQuestion)
- The metric list is ambiguous (which segments, sample scope).
- Ground-truth sample size/scope for certification (default: stratified ~8 periods).

## Definition of done
`scripts/report_coverage.py <slug>` passing (or its shortfall explicitly recorded) AND a successful
`refresh-quarterly-estate sync --only <slug> --apply` receipt AND gate verdict **STRONG** AND
`python3 -m src.eval.compare_extractions <slug> --tiers xbrl,bmv,note,search,table,prose` at the
acceptance bar (≥95%/≥95%, aim 100%) on the sampled cells, with `<slug>_accuracy.md` written (naming
the corpus composition from `provenance.json`) and the regression batch green.

## When the company has no earnings-release PDFs
156 of 179 issuers have **zero** PDF-backed quarters; ~72 are XBRL-only. That is a normal outcome,
not a failure — but it changes what you can promise:
- Coverage starts at the **2021-2T XBRL wall**. BMV purges older zips, so pre-2021 is unreachable
  without an IR binding or Wayback. Say so rather than shipping silent blanks.
- The trustworthy tier is **`xbrl` (tagged IFRS facts)**. The `<period>.md` rendered from MD&A is
  real narrative and feeds the prose/search tiers, but it is thinner than an earnings release and
  generic extraction over it is noisy — expect to need `custom_metrics`/a custom extractor, and
  lean on `metric_expectations`.
- Annual (`YYYY-FY`) filings carry facts but usually no narrative; they land as `facts` in the
  coverage report.
- Record the corpus composition (from `provenance.json`) in `<slug>_accuracy.md`. A workbook built
  on MD&A must not read as if it were built on the issuer's own reports.

## Gotchas (full list in the runbook)
H1=slug · slug must be in `configs/issuers.yaml` · register in `_CUSTOM_EXTRACTORS` (key need not
equal slug) · `--tiers …,table` for pdf_path and `…,note` for income_note companies · module form
for compare_extractions · `statement` > `bmv` · `exclude_metrics` stops identity-poisoning ·
magnitude outliers are often real · GT must be independent + blind + blank-undisclosed (never reuse
`data/verified/` as GT) · `--analyst-metrics` is mandatory · a `sync --apply` receipt is what makes
a build publishable · registry `coverage_from_period` describes the *source*, not the issuer's
listing date — never read it as "we have everything since then".

# Decisions

- **D1 — Reuse by copy, not import.** Like `alpha-go`, `soft` vendors the parent packages under
  its own `src/` and never imports the parent. `pyproject.toml pythonpath=["."]` + the vendored
  `src/shared/paths.py` re-root everything into `soft/`. Re-sync via `scripts/vendor_sync.py`.

- **D2 — Hybrid sourcing with a filled template.** Market data + estimates come from Bloomberg via
  a long-format fill-in CSV the engine emits (`src/bloomberg/`); fundamentals come from filings via
  the vendored cascade. Chosen because the engine has no terminal access and valuation needs both.

- **D3 — Reconcile in the valuation layer, not the adapter.** The fundamentals adapter stays a thin
  cascade wrapper. The valuation engine decides source precedence per input: reliable P&L from
  filings; balance-sheet/cash-flow items reconciled against the BBG pack with a scale-artifact guard
  (`_plausible` vs revenue); a residual fill for a single missing segment EBITDA (consolidated −
  known). This absorbs real extraction gaps in WALMEX releases (no clean balance sheet / cash flow).

- **D4 — Live workbook.** `bbg`/`filing` inputs are hard cells; headline multiples + peer medians
  are Excel formulas, so the sheet re-prices when an input changes. Reuses the vendored
  `segments_sheet` style vocabulary.

- **D5 — Pilot = WALMEX, cached filings, no network.** Avoids the known headless download/Playwright
  hangs and gives a tuned config + ground truth to lean on.

- **D6 — Native-first sourcing; Bloomberg is the precise residual.** Resolve every cell in order and
  stop at the first hit: `[filing]` BMV XBRL fact → `[calc]` native derivation → `[price]` Yahoo
  Finance → `[macro]` Banxico SIE / INEGI → `[bbg]` (residual only). Natively derived from XBRL:
  EBITDA (EBIT+D&A), tangible book (equity−intangibles−goodwill), shares (net income÷EPS), FCF,
  ROE/ROA/margins; **peers are BMV names too**, so their fundamentals come from the same XBRL
  pipeline. The Bloomberg template is **gap-driven** — `build_coverage` runs the native layer, then
  re-emits `inputs/<slug>.bloomberg.csv` containing only the cells native sourcing couldn't fill
  (net debt, forward estimates, dividend, bank/REIT operating ratios, unresolved micro-cap prices).
  Native price source: Yahoo `<SYMBOL>.MX` (Stooq's free CSV is now behind a JS proof-of-work wall);
  Yahoo needs the series-suffixed trading symbol, auto-resolved by trying candidates. Macro needs a
  free Banxico SIE / INEGI token via env (`BANXICO_SIE_TOKEN` / `INEGI_TOKEN`).

- **D7 — Annual (financial-sector) XBRL handling.** Bank filings are annual-only, labelled by filing
  year, with income facts as prior-FY durations that exact period_end matching misses. Soft takes the
  latest full-year duration fact directly (`_latest_duration_facts`, annual filings only) + treats
  annual flows as latest-not-summed. This makes bank net income / EPS / revenue → native (so P/E,
  P/BV, ROE come without Bloomberg; only NIM/CET1/cost-of-risk remain residual).

  Known gaps (follow-ups): issuers tagging D&A/capex under `ifrs_mx-cor_*`/`Adjustments*` variants →
  EBITDA/FCF fall to residual until those concepts are added; `total_debt` isn't an XBRL concept → net
  debt is residual (needs Tier-2 balance-sheet table extraction); USD reporters (Grupo México, Orbia,
  AMX) distort cross-currency multiples (P/E, P/BV) though same-currency ratios (ROE, ROA, margins)
  are correct.

# Analysis Metrics Contract (v1)

The authoritative contract for the **deeper financial-analysis** feature. Workstreams A (engine),
B (HTML artifact), and C (master Excel) all code to this doc. If a label/unit here disagrees with
code, this doc wins — change it here first, then update all three.

## Where the data comes from

All new analysis is computed from **`fund.annual`** — the per-fiscal-year metric dict the XBRL
cascade already produces (`soft/src/coverage/fundamentals.py`). `fund.annual` is
`{year:int -> {metric:str -> value:float}}` and already carries, per FY (when the filing has them):
`revenue`, `operating_income` (= EBIT), `ebitda` (derived = operating_income + |depreciation|),
`gross_profit`, `net_income`, `inventory`, `total_assets`, `total_equity`, `cfo`, `capex`,
`accounts_receivable`, `accounts_payable`, `current_assets`, `current_liabilities`, `cash`.

LTM/current values come from `fund.get("<metric>")` and the resolved `model.subject_inputs`
(`CompanyInputs`: `market_cap`, `ev`, `sales`, `ebitda`, `net_income`, `equity`, `fcf`, `net_debt`).

**Bloomberg time-series is an opt-in gap-filler, not a dependency.** The blocks read `fund.annual`
first; where a year/field is missing they may consult `pack.timeseries` (new, opt-in — see A1). The
feature works today for any company with cached XBRL (WALMEX is the pilot). Companies with thin/no
filings render blank rows gracefully — never fabricated.

## CSV / model contract (unchanged)

Each block is a `Block(id, title, rows=[Cell(label, value, unit, source, note)])`. The CSV writer
(`build_coverage.py:_write_csv`) emits `block,label,value,unit,source,note`. `render_dashboard.py`
and `build_master.py` parse **by `block` id + exact `label`** — so labels below are load-bearing.

- `unit` ∈ `pct | x | currency | price | count | ratio`.
- `source` for every new derived row is `"calc"` (Bloomberg-sourced series rows use `"bbg"`).
- Percentages are stored as **whole numbers** (e.g. `12.3` means 12.3%), matching existing blocks.
- Time-series rows use the label prefix **`FY{yr}: <name>`** (mirrors the existing
  `FY{yr}: P/E` convention in `block_historical`) so B/C can regex the series out.

---

## New blocks (industrial / default template)

Scope: the industrial/default template only. Bank (`financials`) and REIT templates keep their
existing blocks unchanged (no regression). Register all four in `_BUILDERS` and append to the
default industrial block list (see A4).

### `profitability` — "Profitability trend & DuPont"

Per FY `yr` in `sorted(fund.annual)` where `revenue` present (skip a metric's row if its numerator
is absent that year):
- `FY{yr}: Gross margin` — pct — `gross_profit/revenue*100`
- `FY{yr}: EBIT margin` — pct — `operating_income/revenue*100`
- `FY{yr}: EBITDA margin` — pct — `ebitda/revenue*100`
- `FY{yr}: Net margin` — pct — `net_income/revenue*100`

LTM summary + DuPont (all LTM):
- `EBIT margin (LTM)` — pct — `operating_income_LTM/revenue_LTM*100`
- `DuPont — net margin` — pct — `NI/revenue*100`
- `DuPont — asset turnover` — x — `revenue/total_assets`
- `DuPont — equity multiplier` — x — `total_assets/total_equity`
- `DuPont — implied ROE` — pct — `net_margin_frac * asset_turnover * equity_multiplier * 100`
  (must reconcile to `financial_analysis`'s `ROE` within 0.5pp — the auditor checks this.)

### `fcf_liquidity` — "FCF, liquidity & inventory"

LTM:
- `FCF yield` — pct — `fcf_LTM / market_cap * 100` (fcf from `subject_inputs.fcf`, mcap from `subject_inputs.market_cap`)
- `Current ratio` — x — `current_assets / current_liabilities`
- `Quick ratio` — x — `(current_assets − inventory) / current_liabilities`
- `Working capital` — currency — `current_assets − current_liabilities`
- `Inventory turns` — x — `COGS / inventory` where `COGS = revenue − gross_profit` (else skip)
- `Cash conversion cycle (days)` — count — `DIO + DSO − DPO`,
  `DIO = inventory/COGS*365`, `DSO = accounts_receivable/revenue*365`, `DPO = accounts_payable/COGS*365`
  (emit only when all three legs computable; note which legs used)

Per FY `yr` (trend series for charts):
- `FY{yr}: FCF` — currency — `cfo − |capex|`
- `FY{yr}: Inventory days` — count — `inventory / COGS * 365` (COGS = revenue − gross_profit; else on revenue, note it)

Apply the existing scale-artifact guard `_plausible(value, revenue, …)` to balance-sheet inputs
(current_assets/liabilities/AR/AP/inventory) before use — drop implausible values to blank, exactly
as `block_financial` does for `inventory`/`total_assets`. Never emit a ratio built on a rejected input.

### `temporal_ebit` — "EBIT & EBITDA (temporal)"

Per FY `yr`:
- `FY{yr}: EBIT` — currency — `operating_income`
- `FY{yr}: EBITDA` — currency — `ebitda`

Summary (use the two most recent COMPLETE FYs that carry the metric, like `block_financial`'s
revenue-growth guard):
- `EBIT YoY (latest)` — pct — `(ebit_cy/ebit_py − 1)*100`
- `EBITDA YoY (latest)` — pct — `(ebitda_cy/ebitda_py − 1)*100`
- `EBIT CAGR 3y` — pct · `EBIT CAGR 5y` — pct
- `EBITDA CAGR 3y` — pct · `EBITDA CAGR 5y` — pct

### `growth` — "Growth"

Per FY `yr` (from the 2nd available year onward):
- `FY{yr}: Revenue YoY` — pct — `(rev_yr/rev_prev − 1)*100`

Summary:
- `Revenue CAGR 3y` — pct · `Revenue CAGR 5y` — pct
- `Net income CAGR 3y` — pct · `Net income CAGR 5y` — pct
- `Revenue growth stability (σ)` — pct — population stdev of the FY-YoY revenue series (lower = steadier)

> CAGR/YoY live in exactly ONE block each (EBIT/EBITDA CAGR → `temporal_ebit`; revenue/NI CAGR →
> `growth`) so the auditor has a single re-derivation per metric. Do not duplicate a CAGR across blocks.

## Shared helpers (add to valuation.py, reuse everywhere)

```python
def _cagr(series_by_year: dict[int, float], years: int) -> float | None:
    """CAGR over the last `years` span using the earliest & latest available annual points
    within the window. Returns a FRACTION (…-1). None if <2 points, non-positive endpoints,
    or a zero span."""
```
Reuse `safe_div` (`peers.py:14`) for every division. Follow `block_financial`'s pattern of
appending a `Cell` only when its inputs are present.

---

## A1 — opt-in Bloomberg time-series (secondary)

Add a `timeseries` scope/entity_kind so a terminal user CAN supply annual
`revenue/ebit/ebitda/gross_profit/net_income/cfo/capex/fcf/inventory/current_assets/current_liabilities`
for the last 5–10 FYs. It is **opt-in** (behind a spec flag, e.g. `timeseries_years: 0` default) so
the 138 residual templates are NOT flooded with new blank required cells. When present it merges into
a unified per-year series the blocks read after `fund.annual`. `pack.timeseries: {year:{field:value}}`.
Ship the plumbing; leave it disabled by default this version.

---

## B — artifact (render_dashboard.py) reads these

New sections (degrade to a muted "—" when the series/rows are absent):
- **Margin trend** — multi-line SVG of `FY*: Gross/EBIT/EBITDA/Net margin` from `profitability`.
- **EBIT & EBITDA** — grouped bars over FYs from `temporal_ebit` `FY*: EBIT` / `FY*: EBITDA`.
- **Growth** — bars of `FY*: Revenue YoY` from `growth`; CAGR tiles (`Revenue CAGR 5y`, `EBITDA CAGR 5y`, `Net income CAGR 5y`).
- **FCF & liquidity** — tiles for `FCF yield`, `Current ratio`, `Quick ratio`, `Cash conversion cycle (days)`, `Inventory turns`.
- **Inventory days** — line over FYs from `fcf_liquidity` `FY*: Inventory days`.

## C — master Excel (build_master.py) headline columns per company

One row per company; pull these LTM/summary labels (blank when absent):
`EBITDA margin` & `Net margin` & `ROE` & `ROIC` (`financial_analysis`),
`EBIT margin (LTM)` & `DuPont — implied ROE` (`profitability`),
`Revenue CAGR 5y` & `Net income CAGR 5y` & `Revenue growth stability (σ)` (`growth`),
`EBITDA CAGR 5y` & `EBITDA YoY (latest)` (`temporal_ebit`),
`FCF yield` & `Current ratio` & `Quick ratio` & `Cash conversion cycle (days)` & `Inventory turns` (`fcf_liquidity`),
plus valuation `P/E (LTM)` & `EV/EBITDA` & `Dividend yield` & `P/FFO` (`snapshot_multiples` /
`reit_snapshot`),
plus `CET1 ratio` & `P/TBV` (`bank_returns` / `bank_snapshot` — financials only; band **Capital
(financials)**). `P/TBV` = market cap / tangible book value (`equity − intangibles − goodwill`,
`fundamentals.py`), already computed in `block_bank_snapshot`'s `MULTIPLES_BANK` for the whole
financials template (banks, brokers, insurers) — no new extraction, only newly surfaced in the
master grid. Structurally N/A for industrial/reit templates (no tangible-book concept outside
financials, `applicability._INDUSTRIAL_NA` / `_REIT_NA`).

`P/FFO` = market cap / FFO, where FFO is reconstructed from cached XBRL when the Bloomberg pack
has no FFO (`src/coverage/reit_ffo.py`, wired via `_reit_ffo_fallback()` in `valuation.py`):
`FFO = ProfitLoss − fair_value_adjustment + depreciation` — the fair-value adjustment is the same
IAS 40 investment-property mark-to-market line that makes `P/E (LTM)` / `Net margin` / `Net income
CAGR` na_template for fibras/real_estate. Ships only when ProfitLoss and at least one fair-value
XBRL concept are both present (blank-not-wrong — a wrong FFO corrupts P/FFO); ~12/26 REITs measured
(`configs/reit_ffo_concepts.yaml` has the concept list + hit-rate table). The value is an ANNUAL
(FY) figure, not trailing-twelve-month — the Cell keeps the load-bearing `"FFO (LTM)"` label
(`reit_snapshot`) but its note says `FY{year}` explicitly rather than silently claiming LTM.
Structurally N/A for financials/industrial (`_FINANCIALS_NA` / `_INDUSTRIAL_NA`) — no IAS 40
investment-property mechanism outside the REIT template.

Grouped column bands: **Valuation | Profitability | Growth | FCF/Liquidity**. Name cell links via
`=HYPERLINK()` to the company's `<slug>_soft_cov.html` artifact.

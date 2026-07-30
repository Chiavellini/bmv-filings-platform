# Vision

**soft** is a *soft coverage engine*: give it a company, and it produces an equity-research
**coverage summary** as a themed Excel workbook. It is the valuation/analysis counterpart to the
parent pipeline (which extracts operational figures) and to `alpha-go` (which searches the
document corpus).

## What it is
- **Hybrid data**: Bloomberg supplies market data + consensus estimates (via a fill-in template);
  the vendored extraction cascade supplies fundamentals from filings; the engine reconciles and
  computes the multiples/ratios. Every number carries a source tag (`bbg`/`filing`/`calc`/`macro`).
- **Sheet-per-theme**: Sheet 1 = Fundamental Valuation Metrics. More sheets are added over time
  by writing new modules under `src/sheets/` — the spec, fundamentals, and pack machinery are shared.
- **Live workbook**: market/fundamental inputs are hard cells; multiples/ratios are Excel formulas,
  so editing a price re-prices the sheet.

## Sheet 1 blocks
Snapshot multiples (vs peers) · Historical multiples (mean-reversion band) · Sum-of-the-parts ·
Replacement value · Financial analysis (revenue/peers, profitability, FCF/liquidity/inventory,
ROIC/ROA/ROE) · Macro & sector.

## Non-goals
- Not a live market-data feed (no terminal access — the user supplies Bloomberg values).
- Not a re-implementation of extraction (it *reuses* the parent cascade by vendoring).
- Not multi-tenant SaaS.

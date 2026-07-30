# Bloomberg — the ONLY necessary pull (locked)

Bloomberg is used **only for fields with no public source.** Every field was audited:

| Data | Source | Bloomberg? |
|---|---|---|
| Prices (subject + peers + FY-close history) | Yahoo `<clave>.MX` | No |
| Revenue, EBITDA, net income, equity, tangible book, assets, cash, shares, FCF | BMV-XBRL | No |
| **Minority interest** | BMV-XBRL (`NoncontrollingInterests`) | No |
| **Net debt** | Balance sheet (filings) | No |
| **Bank NIM / CET1 / efficiency / cost of risk** | CNBV annual narrative (filings) | No |
| **FIBRA FFO / NOI / occupancy / GLA** | Quarterly MD&A (filings) | No |
| ROE / ROTE / ROIC / margins | Derived | No |
| Dividend yield | Public dividends ÷ price | No |
| Macro (GDP, rate, USDMXN, CPI) | Banxico/INEGI | No |
| **Consensus forward EPS (`eps_ntm`)** | analyst estimates — nowhere public | **Yes** |
| **REIT NAV/share (`nav_ps`)** | MX FIBRAs don't disclose it | **Yes** |

**The entire Bloomberg ask is those last two fields.** → `outputs/_bloomberg/necessary_pull.csv`:
**145 unique tickers** (de-duplicated across all subjects + peers), one `=BDP(...)` per cell.

## One-paste workflow
1. Open `necessary_pull.csv` in a Bloomberg-connected Excel — columns `ticker | BEST_EPS | NAV_PER_SHARE`.
2. The `=BDP(...)` cells resolve live. Paste-special → values.
3. Send it back — `necessary_map.csv` (ticker → companies) routes each value to every
   `data/bloomberg/<slug>.csv` that uses it (peers are shared, so each ticker is pulled once).

## Zero-Bloomberg option
If you don't need **forward P/E** or **P/NAV**, this pull is **zero** — trailing P/E, EV/EBITDA, P/BV,
P/FFO (from filings), margins, and returns are all native. The sheet is opt-in for two optional multiples.

> Regenerate: `python3 scripts/emit_bdp_matrix.py --necessary`. (`--full`/`--residual` exist for
> reference but over-ask — don't use them.)

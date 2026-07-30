# Onboarding a company

The engine soft-boards a company from three inputs: a **coverage spec**, a **minimal config**
(XBRL identity), and a **filled Bloomberg pack**. Fundamentals come from BMV XBRL first
(no per-company tuning); Bloomberg is the final pillar (market data, estimates, EBITDA, bank ratios).

## Recipe

1. **Confirm the BMV XBRL clave** (needs network):
   ```bash
   python3 src/download/bmv_xbrl.py --list-tickers | grep -i <name>
   ```
   Note whether it has quarterly filings (industrials) or annual-only (financial-sector filers).

2. **Config** — `configs/<slug>.yaml` (slug = the H1 name slugified, e.g. `Grupo Mexico`→`grupo_mexico`):
   ```yaml
   company: {name: Grupo Mexico, ticker: "GMEXICOB MM", currency: MXN, exchange: BMV, unit: millions}
   tier_precedence: {default: [xbrl, bmv, prose, search, table, calc]}
   ir_website: {xbrl_ticker: "GMEXICO"}   # the clave from step 1 (quote if it has '&')
   ```

3. **Spec** — `inputs/<slug>.md`:
   ```markdown
   # Grupo Mexico
   Ticker: GMEXICOB MM

   ## Settings
   - template: industrial        # or: financials (banks/insurers)
   - peer_currency: mixed         # omit for a single-currency (MXN) peer set
   - history_years: 5

   ## Peers
   - SCCO US {scco}
   - FCX US {fcx}

   ## Macro
   - Mexico GDP growth, % {gdp_growth}
   ```
   `template` picks the default block set: **industrial** = snapshot multiples + historical band +
   financial analysis + macro; **financials** = bank valuation (P/BV, P/TBV) + returns/capital
   (ROE/ROTE/NIM/efficiency/cost of risk/CET1) + growth + P/BV history + macro. Add an explicit
   `## Blocks` section to opt into `sum_of_the_parts` / `replacement_value` (need segment inputs).

4. **Emit + fill the Bloomberg template**:
   ```bash
   python3 scripts/emit_bloomberg_template.py inputs/<slug>.md
   #  -> inputs/<slug>.bloomberg.csv  (fill 'value' from the terminal; blanks the XBRL already covers are fine)
   #  save the filled file to data/bloomberg/<slug>.csv
   ```

5. **Build** (first run fetches + caches XBRL — needs network):
   ```bash
   python3 scripts/build_coverage.py inputs/<slug>.md
   #  -> outputs/<Name>/{excel,csv,validation}
   ```

## What comes from where

| source | industrials | banks (annual XBRL only) |
|---|---|---|
| `[filing]` (BMV XBRL) | revenue, operating income, net income, total equity, total assets, cash | annual equity, net income, assets (feed P/BV band + annual ROE) |
| `[bbg]` (final pillar) | price, shares, net debt, **EBITDA**, estimates, dividend | price, shares, book/tangible book, NIM, CET1, ROTE, efficiency, cost of risk, loan/deposit growth |
| `[calc]` | multiples, margins, ROIC/ROA/ROE, historical band | P/E, P/BV, P/TBV, ROE, growth, P/BV band |

Leave a Bloomberg cell blank whenever XBRL already covers it — the engine prefers a plausible
filing value and falls back to Bloomberg only for blanks/implausible cells.

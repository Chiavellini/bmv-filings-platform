# Onboarding a no-XBRL company from quarterly-report PDFs

How to fill a company's matrix row from its IR-site quarterly PDFs when it is **not in the BMV free
XBRL archive** (the `ABSENT` set). The infrastructure is fully built and vendored — this is a
per-company config + download + a verification loop, not new code. Proven end-to-end on **Grupo
Bafar** (`grupo_bafar`), the W5 pilot.

## The pipeline (already wired — no engine changes)

`scripts/fetch_company_reports.py <slug> --parse` → downloads quarterly PDFs via the vendored
`download_from_ir` (+ `direct_url_templates` + Wayback), parses each to `data/reports/<slug>/*.md`.
Then `load_fundamentals` (`src/coverage/fundamentals.py:322`) sees local `.md`/`.pdf` and takes the
`run()` cascade branch automatically — the same path WALMEX uses — so `build_coverage` /
`build_master` need nothing special.

## Per-company steps

1. **Find the IR quarterly-reports URL + PDF pattern.** WebFetch the IR page. Note whether report
   URLs are deterministic (an S3/CDN template) — if so, `direct_url_templates` is far more reliable
   than crawling. Bafar: `…/ReportesTrimestrales/{quarter}T{yy}/GB_PR_{quarter}T{yy}.pdf`.
   - The template engine (`download_from_url_templates`) exposes `{year}` (4-digit), `{yy}`
     (2-digit — a soft-local add), and `{quarter}`. It probes each expected period and keeps only
     URLs that actually serve a PDF, so dead older periods are silently skipped.

2. **Write `configs/<slug>.yaml`.** `company` (name/ticker/currency/**unit**/exchange) +
   `tier_precedence` + `ir_website` (`url`, optional `direct_url_templates`, `pdf_link_pattern`,
   `use_playwright` if JS-rendered). **Do not leave an `xbrl_ticker` stub** if the company has no
   XBRL — it's misleading (though harmless once local PDFs exist).

3. **Fetch + parse:** `python3 scripts/fetch_company_reports.py <slug> --parse --floor-year <Y>`.
   Confirm `data/reports/<slug>/YYYY-NT.{pdf,md}` landed.

4. **Author `metric_overrides` for the statement layout, then VERIFY every value.** The generic
   cascade often mis-maps a new filer's tables. Read one parsed `.md`, find the consolidated summary
   + balance tables, and write one regex per line item. The three traps that bit Bafar (expect them):
   - **Scale.** Tables are often in *miles de pesos* (thousands) while the coverage layer works in
     millions. `parse_pdf` may not detect it (Bafar: scale 1.0). Fix per-pattern with
     `multiplier: 0.001`, or document-wide with `table_scale`.
   - **Column order.** Mexican tables frequently put the **prior** year first
     (`<prior> <prior%> <current> <current%> <growth>`). `_build_row` maps group 1 → *current*, so
     capture the **2nd** number as the single group (skip prior/%/growth).
   - **Parenthesised negatives + acumulado rows.** A negative prior prints as `(102,712)` and can
     break the row match, so the pattern silently grabs the **accumulated (YTD)** figure from a
     later table. Make the prior column `\(?-?[\d,]+\)?` and the % `-?…`. Bafar 2T25 net income was
     1,579,856 (quarter), NOT the 2,349,482 acumulado — verify against the report's prose
     ("la utilidad neta alcanzó los $1,579.9 millones").
   - **Base metric keys.** Override the *registry* key (`equity`, not `total_equity`) — a typo'd key
     creates an unused override and the generic pattern ships a wrong value. Check
     `src/model/financial_model.py`.

   Verification is mandatory (blank-not-wrong): cross-check ≥2 periods' revenue/EBITDA/net
   income/equity against the reports' own prose figures before trusting the row. `scripts/build_coverage.py
   inputs/<slug>.md` must PASS the math gate — but the gate only checks internal ratio consistency,
   NOT whether a value is the right line, so the manual cross-check is what catches wrong-but-consistent
   extractions.

5. **What fills vs. stays blank.** From a summary + balance table you get revenue, operating income,
   EBITDA, net income, total assets/equity/liabilities → **EBITDA margin, Net margin, ROE, ROA,
   ROIC, revenue growth**. Leverage (Net debt/EBITDA), Current ratio, and FCF yield need the detailed
   balance sheet + cash-flow statement (cash, debt, current items, cfo, capex) — if those aren't
   cleanly in the report, they stay honestly **blank**, never guessed. 5-year CAGR/σ need ≥5 annual
   points; an IR site that only serves ~2 recent years leaves those `na_source`.

# The BMV-XBRL route (the better path for no-XBRL-archive names)

For a name absent from the free XBRL *archive-index* but with a BMV **issuer page**, the issuer page
serves its filings as standard IFRS **XBRL** — the reliable path (no per-company regex tuning, unlike
the PDF recipe above). This is how Sanborns/Alfa were onboarded. The route:

1. **Get the emisora id.** GET a token from `/rest/tokenservice/token`, then POST
   `/api/searchservice/v1` (Bearer auth) `{"lang":"es","payload":{"term":"<CLAVE>","term2":"","termT":"<CLAVE>","searchType":"busquedaClaveCotizacion"}}`
   → the `id` (e.g. GSANBOR 5227, ALFA 5052; suspended issuers use `id_empresa`, e.g. ELEKTRA 5457).
2. **Harvest doc URLs from the issuer-page HTML** (plain `curl`, no browser needed):
   `curl .../en/issuers/financialinformation/<CLAVE>-<id>-CGEN_CAPIT | grep -oE '(ifrsxbrl|anexon|constrim)_[0-9]+_[0-9-]+_[0-9]+\.(zip|pdf)'`.
   The page exposes the **latest** quarterly (`ifrsxbrl_…`) and the latest **annual** (`anexon_…`).
   The **annual `anexon`** is the prize — it carries the full-year statements **plus 2 prior-year
   comparatives** (e.g. Sanborns FY2024 anexon holds FY2022/2023/2024).
3. **Download + stage:** `docs-pub/anexon/<file>` → gunzip → gzip the inner JSON to
   `data/reports/<slug>/xbrl/<TICKER>_<FY>-FY.json.gz`. soft's `bmv_xbrl.extract_artifacts` +
   `_extract_via_xbrl` process it exactly like any BMV XBRL — **no metric_overrides**.
   ⚠️ Stage the annual filing **alone** (don't also stage a quarterly): mixing a quarter with the
   annual makes soft's FY rollup require 4 quarters and drop the anexon's full-year flows.
4. **Scale.** Standard BMV filers file in **full pesos** (`unit: millions` → ÷1e6). Some file in
   **thousands** (Alfa: FY2025 revenue raw 177,853,452 = $177,853mn) → set `unit: miles` (÷1e3).
   Verify a known figure (revenue) after staging; a 1000× miss means the wrong `unit`.
5. **Limitation — one annual year today.** soft extracts the anexon's *current* FY (not its
   in-filing comparatives), so a single anexon fills margins/ROE/ROA for one year; growth/CAGR and
   EBITDA-margin (needs depreciation) stay `na_source`. **Follow-on:** teach `fundamentals.py` to
   emit each comparative-year duration from an annual filing into `fund.annual[year]` (additive, so
   no regression) — the data is already on disk and would unlock growth + 3-year history cheaply.

## Status

- **grupo_bafar** — DONE, verified (PDF recipe). 6 quarters (4T24–1T26), fills margins/returns/growth
  accurately (Net margin 15.7%, ROE 25.9%, EBITDA margin 19.3%, all cross-checked). Leverage/liquidity
  blank (no clean cash-flow/debt lines in the summary reports).
- **grupo_sanborns** — DONE, verified (BMV-XBRL route). FY2024 anexon → Net margin 5.35%, ROE 9.94%,
  ROA 6.01%, ROIC 8.59% (all cross-checked: revenue 73,353mn, NI 3,925mn match the filing). Growth /
  EBITDA-margin / leverage blank (one annual year; no depreciation/debt detail).
- **alfa** — DONE, with caveat (BMV-XBRL route). FY2025 anexon, `unit: miles` (files in thousands).
  Revenue 177,853mn and Net margin 4.96% are solid; ROE 58.8% / ROIC 74.6% are arithmetically correct
  but reflect Alfa's unusual post-restructuring capital base (ALFA S.A.B. ≈ Sigma after the
  Alpek/Nemak spinoffs — thin equity $15bn on $123bn assets). Faithful to the filing; read the
  return ratios with that context.
- **grupo_elektra** — NOT shipped (honest blank). Data IS retrievable (FY2024 anexon: revenue
  201,296mn, NI −11,153mn), but Elektra consolidates **Banco Azteca**, so its assets (479bn) and
  liabilities are a retail+bank blend — the **industrial** template's Net debt/EBITDA, current ratio,
  and EV metrics would be computed on a bank balance sheet and ship **misleading** values. Left blank
  (blank-not-wrong) pending a `financials`-aware template or per-cell suppression for mixed holdings.
  Its staged XBRL was removed so the matrix doesn't emit the distorted cells.

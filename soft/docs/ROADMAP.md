# Roadmap

## Done (pilot: WALMEX, Sheet 1)
Phases 0–5 complete. `scripts/build_coverage.py inputs/walmex.md` produces
`outputs/Walmex/{excel,csv,validation}` from cached filings + a filled Bloomberg pack. 30 offline
tests pass (`pytest -m "not network and not model"`).

## Terminal-free by default (2026-07-12)
The default deliverable dropped the Bloomberg-only fields with no free/native source, so they no
longer appear as blank rows or on the residual worklist: forward P/E (`eps_ntm` / `pe_fwd`), bank
`loan_growth`/`deposit_growth`, and REIT `nav_ps` (`p_nav`/"P/NAV"), `ltv`, `cap_rate`, `gla`.
Natively-sourced fields stay (AFFO/`p_affo`, FFO, NOI, occupancy, distribution yield, all bank
ratios). The Bloomberg pack is strictly optional — a missing `data/bloomberg/*.csv` yields honest
blanks, never a wrong value.

## Known limitations / follow-ups
- **Cross-currency peers.** Absolute cross-company figures (revenue share) break when a peer is in
  another currency (COST US). Multiples (ratios) are currency-neutral, but the peer set should be
  FX-normalised or same-currency. COST US is left blank in the pilot and flagged by validation.
- **Historical multiples approximation.** Historical EV uses *current* shares & net debt against
  each FY's fundamentals (no historical share count / net debt). Noted per row.
- **Replacement value is sales-floor-only.** Lower bound; ignores land, DCs, inventory, brand. A
  fuller asset build-up is a follow-up.
- **Segment SOTP** relies on a residual fill for Mexico EBITDA (releases broke out segments only
  from 4Q23). A dedicated segment-EBITDA extractor would remove the residual.

## Live operating model (2026-07-10)
Two data clocks, two commands (details in `deploy/README.md`):
- **Daily** — `scripts/refresh_daily.py` re-prices every workbook from live Yahoo quotes, reusing the
  cached quarterly XBRL. A launchd agent (`deploy/com.soft.refresh.plist`) runs it each weekday after
  the BMV close. The market-data cache honours `SOFT_MARKET_TTL_HOURS` (default 18h) so a normal
  build doesn't hammer Yahoo but still picks up a fresh quote each morning; the refresh forces it.
- **Quarterly** — `fetch_xbrl_batch.py` + `build_all.py --cached-funds` when new filings land (the
  only time the expensive XBRL work re-runs).

## Deferred (known-INCOMPLETE — blank-not-wrong, documented)
- Bank NIM/CET1 (strict CNBV-narrative extractor, partial coverage).
- REIT FFO/AFFO/NAV/distribution extractor (a wrong FFO corrupts P/FFO).
- ~57-name XBRL corpus backfill (incl. GFInbursa + 3 ERROR fibras with no documents).
- Per-issuer residuals: Pena Verde (millions-scale filer), GNP (insurer equity=0 undimensioned),
  GBM/Procorp (broker share counts).

## Next
- Generalise beyond WALMEX (peer packs, a second pilot; FEMSA for a real multi-segment SOTP).
- Sheet 2+ (new `src/sheets/` module per theme).
- Optional: pull historical share count / net debt into the fundamentals adapter for exact
  historical multiples.

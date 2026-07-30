"""Valuation engine — the metric dictionary for Sheet 1.

Given a coverage spec, the filings :class:`Fundamentals`, and the ingested :class:`BloombergPack`,
build a structured coverage model: a list of blocks, each a list of typed cells with a source tag
(``bbg`` / ``filing`` / ``calc`` / ``macro``). Pure functions — no I/O, fully unit-testable.

The workbook builder (``src.sheets.valuation_sheet``) turns this model into Excel; the CSV/
validation writers consume the same model, so numbers are computed exactly once here.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from src.coverage.peers import (
    MULTIPLES,
    MULTIPLES_BANK,
    MULTIPLES_REIT,
    CompanyInputs,
    build_cross_section,
    company_multiples,
    company_multiples_bank,
    company_multiples_reit,
    safe_div,
)

# Default assumptions (documented; overridable later via config).
DEFAULT_TAX_RATE = 0.30      # Mexican statutory corporate rate — NOPAT proxy for ROIC


def _plausible(value, ref, lo=0.01, hi=50.0):
    """True if ``value`` is within [lo*ref, hi*ref] in magnitude — a scale-artifact guard.

    WALMEX release balance-sheet/cash-flow extractions sometimes come out ×1000 off or as a
    stray small number; sized against revenue this rejects both without hard-coding a scale.
    """
    if value is None or ref is None or ref == 0:
        return False
    m = abs(value)
    return lo * abs(ref) <= m <= hi * abs(ref)


def _reconcile(filing_value, bbg_value, ref, *, guard=True):
    """Prefer a plausible filing value; otherwise fall back to the Bloomberg value.

    Returns (value, source) where source is 'filing', 'bbg', or 'calc'/None.
    """
    if filing_value is not None and (not guard or _plausible(filing_value, ref)):
        return filing_value, "filing"
    if bbg_value is not None:
        return bbg_value, "bbg"
    return None, None


def _cagr(series_by_year: dict, years: int):
    """CAGR over the last ``years`` span using the earliest & latest available annual points
    within the window ``[latest-years, latest]``. Returns a FRACTION (…-1). None if <2 points,
    non-positive endpoints, or a zero span."""
    if not series_by_year:
        return None
    ys = sorted(series_by_year)
    latest = ys[-1]
    window = [y for y in ys if y >= latest - years]
    if len(window) < 2:
        return None
    y0, y1 = window[0], window[-1]
    span = y1 - y0
    if span <= 0:
        return None
    v0, v1 = series_by_year[y0], series_by_year[y1]
    if v0 is None or v1 is None or v0 <= 0 or v1 <= 0:
        return None
    return (v1 / v0) ** (1.0 / span) - 1.0


def _cagr_span_note(series_by_year: dict, years: int) -> str:
    """Actual FY span used by :func:`_cagr` for its ``years`` window — so a 'CAGR 5y' computed off
    only 3 fiscal years is transparently labelled as a 2y-span figure (avoids overstating horizon)."""
    ys = sorted(series_by_year)
    if not ys:
        return ""
    latest = ys[-1]
    win = [y for y in ys if y >= latest - years]
    if len(win) < 2:
        return ""
    return f"FY{win[0]}→FY{win[-1]} ({win[-1] - win[0]}y actual span)"


def _annual_series_years(fund, pack) -> list:
    """Merged sorted FY list the analysis blocks iterate: ``fund.annual`` years plus any the opt-in
    Bloomberg time-series supplies (default off → just ``fund.annual``)."""
    ts = getattr(pack, "timeseries", None) or {}
    return sorted(set(fund.annual) | set(ts))


def _annual_val(fund, pack, yr: int, field: str):
    """Per-FY value for (year, field): read ``fund.annual`` first, then fall back to the opt-in
    Bloomberg time-series (``pack.timeseries``). ``None`` when neither carries it."""
    v = (fund.annual.get(yr) or {}).get(field)
    if v is not None:
        return v
    ts = getattr(pack, "timeseries", None) or {}
    return (ts.get(yr) or {}).get(field)


@dataclass
class Cell:
    label: str
    value: float | None
    unit: str            # x | pct | currency | price | ratio | count
    source: str          # bbg | filing | calc | macro
    note: str = ""


@dataclass
class Block:
    id: str
    title: str
    rows: list[Cell] = field(default_factory=list)
    # Optional peer cross-section attached to the snapshot block (rendered as extra columns).
    cross_section: object | None = None


@dataclass
class CoverageModel:
    slug: str
    name: str
    currency: str
    units: str
    current_period: str
    blocks: list[Block] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Resolved inputs the blocks were computed from — exposed so the math auditor can re-derive
    # every ratio independently (raw inputs, not ratios). Populated by :func:`build_model`.
    subject_inputs: object | None = None
    peer_inputs: list = field(default_factory=list)
    template: str = ""


# ---------------------------------------------------------------------------
# Input assembly
# ---------------------------------------------------------------------------


def _subject_inputs(fund, pack) -> CompanyInputs:
    """Subject inputs: P&L from filings (reliable), balance-sheet/cash-flow reconciled against
    the BBG pack (WALMEX releases carry them unreliably or not at all)."""
    rev = fund.get("revenue")
    # EBITDA is not an XBRL concept → fall back to the BBG pack when the filing lacks it.
    ebitda, _ = _reconcile(fund.get("ebitda"), pack.subject.get("ebitda_ltm"), rev)
    # Book value: filings/releases often omit it → BBG fallback.
    equity, _ = _reconcile(fund.get("total_equity"), pack.subject.get("total_equity"), rev)
    # FCF: prefer CFO−capex when both are present AND CFO is a plausible fraction of EBITDA;
    # else use the BBG FCF. (WALMEX release CFO extractions are frequently ×1000 off.)
    cfo, capex = fund.get("cfo"), fund.get("capex")
    filing_fcf = (cfo - capex) if (cfo is not None and capex is not None
                                   and _plausible(cfo, ebitda, lo=0.2, hi=5.0)) else None
    # Fallback to the natively-derived fcf (cfo−capex, fundamentals._derive_native) when the
    # ebitda-referenced guard above couldn't vet it (commonly because ebitda is absent). Size it
    # against revenue instead, so a ×1000 scale artifact still blanks but a normal FCF populates
    # the FCF-yield cell that was otherwise starved.
    if filing_fcf is None:
        dfcf = fund.get("fcf")
        if dfcf is not None and _plausible(dfcf, rev, lo=0.0, hi=1.5):
            filing_fcf = dfcf
    fcf, _ = _reconcile(filing_fcf, pack.subject.get("fcf_ltm"), rev, guard=False)
    return CompanyInputs(
        name=fund.slug,
        px_last=pack.subject.get("px_last"),
        shares_out=pack.subject.get("shares_out"),
        net_debt=pack.subject.get("net_debt"),
        minority_interest=pack.subject.get("minority_interest", 0.0) or 0.0,
        sales=fund.get("revenue"),
        ebitda=ebitda,
        net_income=fund.get("net_income"),
        equity=equity,
        fcf=fcf,
        eps_ntm=pack.subject.get("eps_ntm"),
        dvd_yield=pack.subject.get("dvd_yield"),
    )


def _peer_inputs(slug: str, p: dict) -> CompanyInputs:
    return CompanyInputs(
        name=slug,
        px_last=p.get("px_last"),
        shares_out=p.get("shares_out"),
        net_debt=p.get("net_debt"),
        minority_interest=p.get("minority_interest", 0.0) or 0.0,
        sales=p.get("sales_ltm"),
        ebitda=p.get("ebitda_ltm"),
        net_income=p.get("net_income_ltm"),
        equity=p.get("total_equity"),
        fcf=p.get("fcf_ltm"),
        eps_ntm=p.get("eps_ntm"),
        dvd_yield=p.get("dvd_yield"),
    )


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


# Multiple-row keys whose value the Yahoo key-stats fallback may supply, mapped to the subject field
# it fills — so a Yahoo-sourced cell is labelled honestly in its note (see native.build_native_pack).
_YAHOO_MKEY_FIELD = {"dvd_yield": "dvd_yield", "dist_yield": "distribution_yield",
                     "roe": "roe", "net_margin": "net_margin"}


def _yahoo_note(pack, mkey: str, note: str) -> str:
    """Append a provenance tag when this multiple's value came from the Yahoo fallback, not filings."""
    field_key = _YAHOO_MKEY_FIELD.get(mkey)
    if field_key and field_key in getattr(pack, "yahoo_filled", ()):
        return f"{note} · source: Yahoo" if note else "source: Yahoo"
    return note


def block_snapshot(spec, fund, pack) -> Block:
    subj = _subject_inputs(fund, pack)
    peers = [_peer_inputs(p.slug, pack.peers.get(p.slug, {})) for p in spec.peers]
    xs = build_cross_section(subj, peers)
    mm = company_multiples(subj)

    rows: list[Cell] = [
        Cell("Price", subj.px_last, "price", "bbg"),
        Cell("Shares out (mn)", subj.shares_out, "count", "bbg"),
        Cell("Market cap", subj.market_cap, "currency", "calc"),
        Cell("Net debt", subj.net_debt, "currency", "bbg"),
        Cell("Minority interest", subj.minority_interest, "currency", "bbg"),
        Cell("Enterprise value", subj.ev, "currency", "calc"),
    ]
    for mkey, label, unit in MULTIPLES:
        prem = xs.subject_vs_median(mkey)
        note = ""
        if prem is not None and unit == "x":
            note = f"{(prem - 1) * 100:+.0f}% vs peer median"
        note = _yahoo_note(pack, mkey, note)
        rows.append(Cell(label, mm[mkey], unit, "calc", note))
    return Block("snapshot_multiples", "Snapshot multiples (vs peers)", rows, cross_section=xs)


def _band(rows: list, name: str, series: list[float], current: float | None) -> None:
    """Append mean/±1σ/percentile summary rows for a historical multiple series."""
    if not series:
        return
    mean = statistics.fmean(series)
    sd = statistics.pstdev(series) if len(series) > 1 else 0.0
    rows.append(Cell(f"{name} — historical mean", mean, "x", "calc"))
    rows.append(Cell(f"{name} — +1σ", mean + sd, "x", "calc"))
    rows.append(Cell(f"{name} — −1σ", mean - sd, "x", "calc"))
    if current is not None:
        pool = sorted(series + [current])
        pct = pool.index(current) / (len(pool) - 1) * 100 if len(pool) > 1 else 50.0
        z = safe_div(current - mean, sd) if sd else None
        rows.append(Cell(f"{name} — current percentile", pct, "pct", "calc",
                         note=f"z={z:+.1f}σ" if z is not None else "flat history"))


def block_historical(spec, fund, pack) -> Block:
    """Historical P/E and EV/EBITDA per fiscal year → mean, ±1σ, current percentile.

    P/E uses annual net income (XBRL provides it); EV/EBITDA uses annual EBITDA (often
    Bloomberg-only). Each band is reported only when it has data — so XBRL-sourced names still
    get a P/E band even without historical EBITDA.
    """
    shares = pack.subject.get("shares_out")
    net_debt = pack.subject.get("net_debt")
    rows: list[Cell] = []
    subj = _subject_inputs(fund, pack)
    cur = company_multiples(subj)

    # (display name, current-multiple key, per-year value function)
    bands = [
        ("P/E", "pe_ltm", lambda mc, ev, a: safe_div(mc, a.get("net_income"))),
        ("EV/EBITDA", "ev_ebitda", lambda mc, ev, a: safe_div(ev, a.get("ebitda"))),
    ]
    for name, curkey, fn in bands:
        series: list[float] = []
        frows: list[Cell] = []
        for yr in sorted(pack.history):
            px = pack.history[yr]
            a = fund.annual.get(yr, {})
            mc = px * shares if (px is not None and shares is not None) else None
            ev = (mc + net_debt) if (mc is not None and net_debt is not None) else None
            val = fn(mc, ev, a)
            if val is not None:
                series.append(val)
            frows.append(Cell(f"FY{yr}: {name}", val, "x", "calc",
                              note=(f"px {px:.1f}; current shares/net debt" if px is not None
                                    else "no price")))
        if not series:
            continue  # skip a band with no data (e.g. EV/EBITDA when EBITDA isn't in XBRL)
        rows.extend(frows)
        _band(rows, name, series, cur.get(curkey))

    if not rows:
        rows.append(Cell("Historical band", None, "x", "calc",
                         note="no annual fundamentals available for the history window"))
    return Block("historical_multiples", "Historical multiples (mean-reversion)", rows)


def block_sotp(spec, fund, pack) -> Block:
    shares = pack.subject.get("shares_out")
    net_debt = pack.subject.get("net_debt")
    minority = pack.subject.get("minority_interest", 0.0) or 0.0
    rows: list[Cell] = []
    total_ev = 0.0
    covered_ebitda = 0.0
    any_seg = False

    # Residual fill: if exactly one segment's EBITDA is missing, back it out of consolidated
    # EBITDA minus the known segments (WALMEX only broke out segment EBITDA from 4Q23).
    seg_ebitda_map = {seg.key: fund.get(f"ebitda_{seg.key}") for seg in spec.segments}
    missing = [k for k, v in seg_ebitda_map.items() if v is None]
    cons = fund.get("ebitda")
    if len(missing) == 1 and cons is not None:
        known = sum(v for v in seg_ebitda_map.values() if v is not None)
        residual = cons - known
        if residual > 0:
            seg_ebitda_map[missing[0]] = residual

    for seg in spec.segments:
        seg_ebitda = seg_ebitda_map.get(seg.key)
        is_residual = seg.key in missing and seg_ebitda is not None
        mult = pack.segment_multiples.get(seg.key)
        seg_ev = seg_ebitda * mult if (seg_ebitda is not None and mult is not None) else None
        tag = "filing residual" if is_residual else "filing"
        rows.append(Cell(f"{seg.label}: EBITDA × {mult if mult is not None else '—'}x",
                         seg_ev, "currency", "calc",
                         note=f"EBITDA {seg_ebitda:,.0f} [{tag}] × {mult}x [bbg]"
                         if seg_ev is not None else "missing EBITDA or multiple"))
        if seg_ev is not None:
            total_ev += seg_ev
            covered_ebitda += seg_ebitda
            any_seg = True

    if any_seg:
        rows.append(Cell("Sum of segment EV", total_ev, "currency", "calc"))
        implied_equity = total_ev - (net_debt or 0.0) - minority
        rows.append(Cell("Less: net debt + minorities",
                         -((net_debt or 0.0) + minority), "currency", "calc"))
        rows.append(Cell("Implied equity value", implied_equity, "currency", "calc"))
        implied_px = safe_div(implied_equity, shares)
        rows.append(Cell("Implied price / share", implied_px, "price", "calc"))
        upside = safe_div(implied_px, pack.subject.get("px_last"))
        if upside is not None:
            rows.append(Cell("Upside / (downside) to spot", (upside - 1) * 100, "pct", "calc"))
        # coverage vs consolidated EBITDA
        cons = fund.get("ebitda")
        cov = safe_div(covered_ebitda, cons)
        if cov is not None:
            rows.append(Cell("Segment EBITDA coverage", cov * 100, "pct", "calc",
                             note="share of consolidated EBITDA valued"))
    return Block("sum_of_the_parts", "Sum-of-the-parts", rows)


def block_replacement(spec, fund, pack) -> Block:
    cost = pack.subject.get("replacement_cost_per_sqm")
    rows: list[Cell] = []
    floor = 0.0
    for key in ("sales_floor_mexico", "sales_floor_cam"):
        v = fund.get(key)
        if v is not None:
            floor += v
    rows.append(Cell("Sales floor (m²)", floor or None, "count", "filing"))
    rows.append(Cell("Replacement cost / m²", cost, "currency", "bbg"))
    # Sales floor is in raw m² and cost in raw currency/m²; the rest of the model is in
    # `units` (millions) → normalise the product so EV/replacement is comparable.
    _scale = 1_000_000 if spec.units == "millions" else (1_000 if spec.units == "thousands" else 1)
    repl_assets = (floor * cost / _scale) if (floor and cost is not None) else None
    rows.append(Cell("Replacement value of sales floor", repl_assets, "currency", "calc"))
    if repl_assets is not None:
        net_debt = pack.subject.get("net_debt") or 0.0
        implied_equity = repl_assets - net_debt
        rows.append(Cell("Implied equity (replacement − net debt)", implied_equity, "currency", "calc"))
        subj = _subject_inputs(fund, pack)
        rows.append(Cell("Market EV", subj.ev, "currency", "calc"))
        ratio = safe_div(subj.ev, repl_assets)
        if ratio is not None:
            rows.append(Cell("Market EV / replacement value", ratio, "x", "calc",
                             note="Tobin's-Q-style; >1 = premium to rebuild cost"))
    return Block("replacement_value", "Replacement value", rows)


def block_financial(spec, fund, pack) -> Block:
    subj = _subject_inputs(fund, pack)
    peers = [_peer_inputs(p.slug, pack.peers.get(p.slug, {})) for p in spec.peers]
    rows: list[Cell] = []

    rev, ni = fund.get("revenue"), fund.get("net_income")
    # Use the SAME reconciled EBITDA as the snapshot block (falls back to the native/pack ebitda_ltm
    # when the raw fund EBITDA is missing/implausible) so EBITDA margin & Net debt/EBITDA fill
    # whenever EV/EBITDA does — they previously read raw fund.ebitda and blanked (Pinfra/CIE/Aleática).
    ebitda, _ = _reconcile(fund.get("ebitda"), pack.subject.get("ebitda_ltm"), rev)
    gp, oi = fund.get("gross_profit"), fund.get("operating_income")
    equity = subj.equity
    # Balance-sheet items: accept the filing value only if plausibly scaled vs revenue,
    # otherwise fall back to the BBG pack (total_assets) or drop (inventory).
    assets, _ = _reconcile(fund.get("total_assets"), pack.subject.get("total_assets"), rev)
    # Accounting identity: total assets = liabilities + equity ≥ equity. A reconciled assets value
    # below equity is a scale/extraction artifact (WALMEX once yielded ~500× too small → ROA
    # 10,078%). Drop it so ROA is left blank (a flagged gap) rather than a fabricated return.
    if assets is not None and equity is not None and assets < equity:
        assets = None
    inv = fund.get("inventory")
    if not _plausible(inv, rev, lo=0.005, hi=2.0):
        inv = None

    # Revenue analysis vs peers
    rows.append(Cell("Revenue (LTM)", rev, "currency", "filing"))
    # Revenue share is an ABSOLUTE cross-company comparison → only valid when the peer set is
    # single-currency. Suppressed when the spec marks peers as mixed-currency.
    peer_sales = [p.sales for p in peers if p.sales is not None]
    if rev is not None and peer_sales and getattr(spec, "peer_currency", "") != "mixed":
        share = safe_div(rev, rev + sum(peer_sales))
        rows.append(Cell("Revenue share of peer set", (share or 0) * 100, "pct", "calc"))
    # revenue growth (FY over FY) — use the two most recent COMPLETE fiscal years that carry a
    # summed revenue, so a partial current year (e.g. only Q1 filed) doesn't blank the metric.
    ryrs = [y for y in sorted(fund.annual) if fund.annual[y].get("revenue") is not None]
    if len(ryrs) >= 2:
        cy, py = ryrs[-1], ryrs[-2]
        g = safe_div(fund.annual[cy].get("revenue"), fund.annual[py].get("revenue"))
        if g is not None:
            rows.append(Cell(f"Revenue growth FY{py}→FY{cy}", (g - 1) * 100, "pct", "calc"))

    # Profitability vs peer medians
    ebitda_margin = safe_div(ebitda, rev)
    rows.append(Cell("EBITDA margin", (ebitda_margin or 0) * 100 if ebitda_margin is not None else None, "pct", "calc"))
    peer_em = [safe_div(p.ebitda, p.sales) for p in peers]
    peer_em = [v for v in peer_em if v is not None]
    if ebitda_margin is not None and peer_em:
        rows.append(Cell("Peer-median EBITDA margin", statistics.median(peer_em) * 100, "pct", "calc"))
    if gp is not None and rev:
        rows.append(Cell("Gross margin", safe_div(gp, rev) * 100, "pct", "calc"))
    _ks = getattr(pack, "yahoo_stats", None) or {}
    if ni is not None and rev:
        rows.append(Cell("Net margin", safe_div(ni, rev) * 100, "pct", "calc"))
    elif _ks.get("profit_margin") is not None and -100 <= _ks["profit_margin"] <= 100:
        # filing net income/revenue unavailable → Yahoo's own profit margin (whole %), fill-blank-only
        pack.yahoo_filled.add("net_margin")
        rows.append(Cell("Net margin", _ks["profit_margin"], "pct", "calc",
                         note=_yahoo_note(pack, "net_margin", "")))

    # FCF / liquidity / inventory
    if subj.fcf is not None:
        rows.append(Cell("Free cash flow (LTM)", subj.fcf, "currency", "calc", note="CFO − capex [filing]"))
        conv = safe_div(subj.fcf, ebitda)
        if conv is not None:
            rows.append(Cell("FCF / EBITDA conversion", conv * 100, "pct", "calc"))
    if inv is not None:
        cogs = (rev - gp) if (rev is not None and gp is not None) else rev
        inv_days = safe_div(inv, cogs)
        if inv_days is not None:
            rows.append(Cell("Inventory days", inv_days * 365, "count", "calc",
                             note="on COGS" if gp is not None else "on revenue (COGS n/a)"))

    # Returns
    roe = safe_div(ni, equity)
    roa = safe_div(ni, assets)
    nopat = oi * (1 - DEFAULT_TAX_RATE) if oi is not None else None
    invested = (equity + (subj.net_debt or 0.0)) if equity is not None else None
    roic = safe_div(nopat, invested)
    if roe is not None:
        rows.append(Cell("ROE", roe * 100, "pct", "calc"))
    elif _ks.get("roe") is not None and -150 <= _ks["roe"] <= 150:
        # filing net income/equity unavailable → Yahoo's own ROE (whole %), fill-blank-only
        pack.yahoo_filled.add("roe")
        rows.append(Cell("ROE", _ks["roe"], "pct", "calc", note=_yahoo_note(pack, "roe", "")))
    if roa is not None:
        rows.append(Cell("ROA", roa * 100, "pct", "calc"))
    if roic is not None:
        rows.append(Cell("ROIC", roic * 100, "pct", "calc",
                         note=f"NOPAT @ {DEFAULT_TAX_RATE:.0%} tax / (equity + net debt)"))
    peer_roe = [safe_div(p.net_income, p.equity) for p in peers]
    peer_roe = [v for v in peer_roe if v is not None]
    if peer_roe:
        rows.append(Cell("Peer-median ROE", statistics.median(peer_roe) * 100, "pct", "calc"))

    # Universal valuation + leverage — same inputs for every template, so the cross-company matrix
    # carries a P/BV and a leverage read for industrials and REITs, not just banks (whose P/BV lives
    # in bank_snapshot). Blank-not-wrong: emit only when the inputs are present.
    pbv = safe_div(subj.market_cap, equity)
    if pbv is not None:
        rows.append(Cell("P/BV", pbv, "x", "calc", note="market cap / book equity"))
    if subj.net_debt is not None and ebitda is not None and ebitda > 0:
        rows.append(Cell("Net debt / EBITDA", subj.net_debt / ebitda, "x", "calc",
                         note="net cash if negative"))
    return Block("financial_analysis", "Financial analysis", rows)


def block_macro(spec, fund, pack) -> Block:
    rows: list[Cell] = []
    for m in spec.macro:
        rows.append(Cell(m.label, pack.macro.get(m.key), "index", "macro"))
    return Block("macro_sector", "Macro & sector", rows)


# ---------------------------------------------------------------------------
# Deeper analysis blocks (industrial / default template) — computed from fund.annual.
# All rows are `source="calc"`; percentages stored as whole numbers; time-series rows use the
# `FY{yr}: <name>` label convention so B/C can regex the series out. See docs/ANALYSIS_METRICS.md.
# ---------------------------------------------------------------------------

# Scale-artifact guards for balance-sheet inputs, sized against revenue (mirror block_financial's
# inventory guard). Kept as explicit literals so the math auditor can replicate them exactly.
_BS_LO, _BS_HI = 0.001, 5.0       # current assets/liabilities, receivables, payables
_INV_LO, _INV_HI = 0.005, 2.0     # inventory


def block_profitability(spec, fund, pack) -> Block:
    """Margin trend series + LTM DuPont decomposition."""
    rows: list[Cell] = []
    for yr in _annual_series_years(fund, pack):
        rev = _annual_val(fund, pack, yr, "revenue")
        if rev is None or rev == 0:
            continue
        gp = _annual_val(fund, pack, yr, "gross_profit")
        oi = _annual_val(fund, pack, yr, "operating_income")
        eb = _annual_val(fund, pack, yr, "ebitda")
        ni = _annual_val(fund, pack, yr, "net_income")
        if gp is not None:
            rows.append(Cell(f"FY{yr}: Gross margin", safe_div(gp, rev) * 100, "pct", "calc"))
        if oi is not None:
            rows.append(Cell(f"FY{yr}: EBIT margin", safe_div(oi, rev) * 100, "pct", "calc"))
        if eb is not None:
            rows.append(Cell(f"FY{yr}: EBITDA margin", safe_div(eb, rev) * 100, "pct", "calc"))
        if ni is not None:
            rows.append(Cell(f"FY{yr}: Net margin", safe_div(ni, rev) * 100, "pct", "calc"))

    # LTM summary + DuPont. `equity`/`assets` are resolved exactly as block_financial does so the
    # implied ROE reconciles to financial_analysis's ROE (revenue and assets cancel out).
    subj = _subject_inputs(fund, pack)
    rev, oi, ni = fund.get("revenue"), fund.get("operating_income"), fund.get("net_income")
    equity = subj.equity
    assets, _ = _reconcile(fund.get("total_assets"), pack.subject.get("total_assets"), rev)
    if assets is not None and equity is not None and assets < equity:
        assets = None

    if oi is not None and rev:
        rows.append(Cell("EBIT margin (LTM)", safe_div(oi, rev) * 100, "pct", "calc"))
    nm = safe_div(ni, rev)
    at = safe_div(rev, assets)
    em = safe_div(assets, equity)
    if nm is not None:
        rows.append(Cell("DuPont — net margin", nm * 100, "pct", "calc"))
    if at is not None:
        rows.append(Cell("DuPont — asset turnover", at, "x", "calc"))
    if em is not None:
        rows.append(Cell("DuPont — equity multiplier", em, "x", "calc"))
    if nm is not None and at is not None and em is not None:
        rows.append(Cell("DuPont — implied ROE", nm * at * em * 100, "pct", "calc",
                         note="net margin × asset turnover × equity multiplier"))
    return Block("profitability", "Profitability trend & DuPont", rows)


def block_fcf_liquidity(spec, fund, pack) -> Block:
    """FCF yield, liquidity ratios, working-capital efficiency + FCF/inventory-days trend series."""
    subj = _subject_inputs(fund, pack)
    rows: list[Cell] = []
    rev = fund.get("revenue")
    gp = fund.get("gross_profit")
    cogs = (rev - gp) if (rev is not None and gp is not None) else None

    def _bs(key, lo=_BS_LO, hi=_BS_HI):
        v = fund.get(key)
        return v if _plausible(v, rev, lo=lo, hi=hi) else None

    ca = _bs("current_assets")
    cl = _bs("current_liabilities")
    ar = _bs("accounts_receivable")
    ap = _bs("accounts_payable")
    inv = _bs("inventory", lo=_INV_LO, hi=_INV_HI)

    mcap = subj.market_cap
    if subj.fcf is not None and mcap:
        rows.append(Cell("FCF yield", safe_div(subj.fcf, mcap) * 100, "pct", "calc",
                         note="FCF [filing] / market cap"))
    cr = safe_div(ca, cl)
    if cr is None:
        # Current assets/liabilities legitimately run many× revenue for asset-heavy, low-revenue
        # issuers (forestry like Proteak, holdings, FIBRAs) — the revenue-sized _bs gate (hi=5×) is a
        # ×1000-artifact guard and wrongly rejects them. The current RATIO is self-validating: accept
        # the RAW CA/CL when both are present and their ratio is in a sane range (a real ×1000 scale
        # error would blow the ratio far outside it, so this readmits truth without readmitting noise).
        ca_raw, cl_raw = fund.get("current_assets"), fund.get("current_liabilities")
        cr_raw = safe_div(ca_raw, cl_raw)
        if cr_raw is not None and 0.02 <= cr_raw <= 50:
            cr = cr_raw
    if cr is not None:
        rows.append(Cell("Current ratio", cr, "x", "calc"))
    if ca is not None and inv is not None:
        qr = safe_div(ca - inv, cl)
        if qr is not None:
            rows.append(Cell("Quick ratio", qr, "x", "calc"))
    if ca is not None and cl is not None:
        rows.append(Cell("Working capital", ca - cl, "currency", "calc"))
    if cogs is not None and inv is not None:
        it = safe_div(cogs, inv)
        if it is not None:
            rows.append(Cell("Inventory turns", it, "x", "calc"))
    # Cash conversion cycle: emit only when all three legs are computable.
    if all(x is not None for x in (cogs, inv, ar, ap)) and rev:
        dio = safe_div(inv, cogs)
        dso = safe_div(ar, rev)
        dpo = safe_div(ap, cogs)
        if None not in (dio, dso, dpo):
            rows.append(Cell("Cash conversion cycle (days)",
                             (dio + dso - dpo) * 365, "count", "calc",
                             note="DIO + DSO − DPO"))

    # Trend series
    for yr in _annual_series_years(fund, pack):
        cfo = _annual_val(fund, pack, yr, "cfo")
        capex = _annual_val(fund, pack, yr, "capex")
        if cfo is not None and capex is not None:
            rows.append(Cell(f"FY{yr}: FCF", cfo - abs(capex), "currency", "calc",
                             note="CFO − |capex|"))
        revy = _annual_val(fund, pack, yr, "revenue")
        invy = _annual_val(fund, pack, yr, "inventory")
        if not _plausible(invy, revy, lo=_INV_LO, hi=_INV_HI):
            invy = None
        if invy is not None and revy:
            gpy = _annual_val(fund, pack, yr, "gross_profit")
            cogsy = (revy - gpy) if gpy is not None else revy
            idays = safe_div(invy, cogsy)
            if idays is not None:
                rows.append(Cell(f"FY{yr}: Inventory days", idays * 365, "count", "calc",
                                 note="on COGS" if gpy is not None else "on revenue (COGS n/a)"))
    return Block("fcf_liquidity", "FCF, liquidity & inventory", rows)


def block_temporal(spec, fund, pack) -> Block:
    """EBIT & EBITDA level series + YoY (latest) and 3y/5y CAGRs."""
    rows: list[Cell] = []
    ebit_series: dict = {}
    ebitda_series: dict = {}
    for yr in _annual_series_years(fund, pack):
        ebit = _annual_val(fund, pack, yr, "operating_income")
        eb = _annual_val(fund, pack, yr, "ebitda")
        if ebit is not None:
            rows.append(Cell(f"FY{yr}: EBIT", ebit, "currency", "calc"))
            ebit_series[yr] = ebit
        if eb is not None:
            rows.append(Cell(f"FY{yr}: EBITDA", eb, "currency", "calc"))
            ebitda_series[yr] = eb

    def _yoy(series):
        ys = sorted(series)
        if len(ys) < 2:
            return None
        g = safe_div(series[ys[-1]], series[ys[-2]])
        return None if g is None else (g - 1) * 100

    for name, series in (("EBIT", ebit_series), ("EBITDA", ebitda_series)):
        y = _yoy(series)
        if y is not None:
            rows.append(Cell(f"{name} YoY (latest)", y, "pct", "calc"))
    for name, series in (("EBIT", ebit_series), ("EBITDA", ebitda_series)):
        for span in (3, 5):
            c = _cagr(series, span)
            if c is not None:
                rows.append(Cell(f"{name} CAGR {span}y", c * 100, "pct", "calc",
                                 note=_cagr_span_note(series, span)))
    return Block("temporal_ebit", "EBIT & EBITDA (temporal)", rows)


def block_growth(spec, fund, pack) -> Block:
    """Revenue-YoY trend, revenue/net-income CAGRs, and revenue-growth stability (σ)."""
    rows: list[Cell] = []
    rev_series: dict = {}
    ni_series: dict = {}
    for yr in _annual_series_years(fund, pack):
        r = _annual_val(fund, pack, yr, "revenue")
        if r is not None:
            rev_series[yr] = r
        n = _annual_val(fund, pack, yr, "net_income")
        if n is not None:
            ni_series[yr] = n

    ry = sorted(rev_series)
    yoy_vals: list = []
    for i in range(1, len(ry)):
        cur, prev = ry[i], ry[i - 1]
        g = safe_div(rev_series[cur], rev_series[prev])
        if g is not None:
            v = (g - 1) * 100
            rows.append(Cell(f"FY{cur}: Revenue YoY", v, "pct", "calc"))
            yoy_vals.append(v)

    # 1-year revenue "CAGR" = the latest fiscal-year revenue YoY (needs only ≥2 annual points).
    if yoy_vals:
        rows.append(Cell("Revenue CAGR 1y", yoy_vals[-1], "pct", "calc",
                         note=f"latest FY revenue YoY (FY{ry[-1]} vs FY{ry[-2]})"))

    # Per-FY net-income YoY + 1-year NI "CAGR". Only off a positive prior year — a YoY through a loss
    # year is meaningless (that is the sign-cross that makes the 5y NI CAGR N/A); skip it honestly.
    ny = sorted(ni_series)
    ni_yoy_vals: list = []
    ni_last_pair = None
    for i in range(1, len(ny)):
        cur, prev = ny[i], ny[i - 1]
        if ni_series[prev] > 0:
            g = safe_div(ni_series[cur], ni_series[prev])
            if g is not None:
                v = (g - 1) * 100
                rows.append(Cell(f"FY{cur}: Net income YoY", v, "pct", "calc"))
                ni_yoy_vals.append(v)
                ni_last_pair = (cur, prev)
    if ni_yoy_vals:
        rows.append(Cell("Net income CAGR 1y", ni_yoy_vals[-1], "pct", "calc",
                         note=f"latest FY net-income YoY (FY{ni_last_pair[0]} vs FY{ni_last_pair[1]})"))

    for span in (3, 5):
        c = _cagr(rev_series, span)
        if c is not None:
            rows.append(Cell(f"Revenue CAGR {span}y", c * 100, "pct", "calc",
                             note=_cagr_span_note(rev_series, span)))
    for span in (3, 5):
        c = _cagr(ni_series, span)
        if c is not None:
            rows.append(Cell(f"Net income CAGR {span}y", c * 100, "pct", "calc",
                             note=_cagr_span_note(ni_series, span)))
    if len(yoy_vals) >= 2:
        rows.append(Cell("Revenue growth stability (σ)", statistics.pstdev(yoy_vals), "pct", "calc",
                         note="population σ of the FY revenue-YoY series (lower = steadier)"))
    return Block("growth", "Growth", rows)


# ---------------------------------------------------------------------------
# Bank (financials-template) blocks — book-value driven, NO EV/EBITDA.
# XBRL gives banks annual equity/net_income/assets; ratios (NIM/CET1/ROTE/efficiency/
# cost of risk) come from Bloomberg. No revenue-scale guard (assets ≫ revenue for banks).
# ---------------------------------------------------------------------------


def _bank_subject_inputs(fund, pack) -> CompanyInputs:
    equity = fund.get("total_equity")
    if equity is None:
        equity = pack.subject.get("book_value")
    ni = fund.get("net_income")
    if ni is None:
        ni = pack.subject.get("net_income_ltm")
    return CompanyInputs(
        name=fund.slug,
        px_last=pack.subject.get("px_last"),
        shares_out=pack.subject.get("shares_out"),
        net_income=ni,
        equity=equity,
        tangible_book=pack.subject.get("tangible_book"),
        eps_ntm=pack.subject.get("eps_ntm"),
        dvd_yield=pack.subject.get("dvd_yield"),
    )


def _bank_peer_inputs(slug: str, p: dict) -> CompanyInputs:
    return CompanyInputs(
        name=slug,
        px_last=p.get("px_last"),
        shares_out=p.get("shares_out"),
        net_income=p.get("net_income_ltm"),
        equity=p.get("book_value"),
        tangible_book=p.get("tangible_book"),
        eps_ntm=p.get("eps_ntm"),
        dvd_yield=p.get("dvd_yield"),
    )


def block_bank_snapshot(spec, fund, pack) -> Block:
    subj = _bank_subject_inputs(fund, pack)
    peers = [_bank_peer_inputs(p.slug, pack.peers.get(p.slug, {})) for p in spec.peers]
    xs = build_cross_section(subj, peers, multiples=MULTIPLES_BANK,
                             multiples_fn=company_multiples_bank)
    mm = company_multiples_bank(subj)
    rows: list[Cell] = [
        Cell("Price", subj.px_last, "price", "bbg"),
        Cell("Shares out (mn)", subj.shares_out, "count", "bbg"),
        Cell("Market cap", subj.market_cap, "currency", "calc"),
        Cell("Book value", subj.equity, "currency", "filing"),
    ]
    for mkey, label, unit in MULTIPLES_BANK:
        prem = xs.subject_vs_median(mkey)
        note = f"{(prem - 1) * 100:+.0f}% vs peer median" if (prem is not None and unit == "x") else ""
        note = _yahoo_note(pack, mkey, note)
        rows.append(Cell(label, mm[mkey], unit, "calc", note))
    return Block("bank_snapshot", "Bank valuation (vs peers)", rows, cross_section=xs)


def block_bank_returns(spec, fund, pack) -> Block:
    subj = _bank_subject_inputs(fund, pack)
    s = pack.subject
    roe = safe_div(subj.net_income, subj.equity)
    rote = safe_div(subj.net_income, subj.tangible_book)
    rows = [
        Cell("ROE", (roe * 100) if roe is not None else s.get("roe"), "pct",
             "calc" if roe is not None else "bbg"),
        Cell("ROTE", (rote * 100) if rote is not None else s.get("rote"), "pct",
             "calc" if rote is not None else "bbg"),
        Cell("Net interest margin (NIM)", s.get("nim"), "pct", "filing"),
        Cell("Efficiency ratio", s.get("efficiency_ratio"), "pct", "filing"),
        Cell("Cost of risk", s.get("cost_of_risk"), "pct", "filing"),
        Cell("CET1 ratio", s.get("cet1"), "pct", "filing"),
    ]
    return Block("bank_returns", "Returns & capital", rows)


def block_bank_growth(spec, fund, pack) -> Block:
    s = pack.subject
    rows: list[Cell] = []
    # Book-value growth from annual XBRL equity, if two years are present.
    yrs = sorted(fund.annual)
    if len(yrs) >= 2:
        cur, prev = fund.annual[yrs[-1]], fund.annual[yrs[-2]]
        g = safe_div(cur.get("total_equity"), prev.get("total_equity"))
        if g is not None:
            rows.append(Cell(f"Book value growth FY{yrs[-2]}→FY{yrs[-1]}", (g - 1) * 100, "pct", "calc"))
        ge = safe_div(cur.get("net_income"), prev.get("net_income"))
        if ge is not None:
            rows.append(Cell(f"Net income growth FY{yrs[-2]}→FY{yrs[-1]}", (ge - 1) * 100, "pct", "calc"))
    return Block("bank_growth", "Growth", rows)


def block_bank_historical(spec, fund, pack) -> Block:
    """Historical P/BV per fiscal year → mean, ±1σ, current percentile (annual XBRL book value)."""
    shares = pack.subject.get("shares_out")
    rows: list[Cell] = []
    series: list[float] = []
    for yr in sorted(pack.history):
        px = pack.history[yr]
        eq = fund.annual.get(yr, {}).get("total_equity")
        mc = px * shares if (px is not None and shares is not None) else None
        pbv = safe_div(mc, eq)
        if pbv is not None:
            series.append(pbv)
        rows.append(Cell(f"FY{yr}: P/BV", pbv, "x", "calc",
                         note=(f"px {px:.1f}; current shares" if px is not None else "no price")))
    subj = _bank_subject_inputs(fund, pack)
    cur = company_multiples_bank(subj)["pbv"]
    if series:
        mean = statistics.fmean(series)
        sd = statistics.pstdev(series) if len(series) > 1 else 0.0
        rows.append(Cell("P/BV — historical mean", mean, "x", "calc"))
        rows.append(Cell("P/BV — +1σ", mean + sd, "x", "calc"))
        rows.append(Cell("P/BV — −1σ", mean - sd, "x", "calc"))
        if cur is not None and len(series) >= 1:
            pool = sorted(series + [cur])
            pct = pool.index(cur) / (len(pool) - 1) * 100 if len(pool) > 1 else 50.0
            z = safe_div(cur - mean, sd) if sd else None
            rows.append(Cell("P/BV — current percentile", pct, "pct", "calc",
                             note=f"z={z:+.1f}σ" if z is not None else "flat history"))
    else:
        rows.append(Cell("P/BV history", None, "x", "calc",
                         note="annual book value unavailable from XBRL for this period range"))
    return Block("bank_historical", "Historical P/BV (mean-reversion)", rows)


# ---------------------------------------------------------------------------
# REIT / FIBRA blocks — FFO/NAV/distribution driven. FFO/NAV aren't XBRL concepts
# (Bloomberg); revenue/net_income/assets come from XBRL.
# ---------------------------------------------------------------------------


# FFO note attached by _reit_ffo_fallback() when the Bloomberg pack has no FFO and the XBRL
# reconstruction shipped one — read back by block_reit_snapshot() to tag the Cell [filing] instead
# of [bbg] and surface the corroboration/single-tag provenance. Keyed by slug so a multi-company
# batch build (build_master.py) doesn't cross-contaminate between companies.
_FFO_FALLBACK_NOTES: dict[str, str] = {}


def _reit_ffo_fallback(slug: str) -> float | None:
    """XBRL-reconstructed FY FFO for ``slug``, or None — the Bloomberg pack is strictly optional
    (ROADMAP.md); this is the free-source path when it's absent. See src.coverage.reit_ffo for the
    IAS 40 fair-value-adjustment reconstruction and its blank-not-wrong corroboration gate."""
    from src.coverage.reit_ffo import ffo_for_slug
    result = ffo_for_slug(slug)
    if result is None:
        _FFO_FALLBACK_NOTES.pop(slug, None)
        return None
    _FFO_FALLBACK_NOTES[slug] = result.note
    return result.value


def _reit_subject_inputs(fund, pack) -> CompanyInputs:
    rev = fund.get("revenue")
    ebitda, _ = _reconcile(fund.get("ebitda"), pack.subject.get("ebitda_ltm"), rev)
    ni = fund.get("net_income")
    ffo = pack.subject.get("ffo")
    if ffo is None:
        ffo = _reit_ffo_fallback(fund.slug)
    return CompanyInputs(
        name=fund.slug,
        px_last=pack.subject.get("px_last"),
        shares_out=pack.subject.get("shares_out"),
        net_debt=pack.subject.get("net_debt"),
        minority_interest=pack.subject.get("minority_interest", 0.0) or 0.0,
        sales=rev, ebitda=ebitda, net_income=ni,
        ffo=ffo, affo=pack.subject.get("affo"),
        nav_ps=pack.subject.get("nav_ps"),
        distribution_yield=pack.subject.get("distribution_yield"),
    )


def _reit_peer_inputs(slug: str, p: dict) -> CompanyInputs:
    return CompanyInputs(
        name=slug,
        px_last=p.get("px_last"), shares_out=p.get("shares_out"),
        net_debt=p.get("net_debt"), minority_interest=p.get("minority_interest", 0.0) or 0.0,
        ebitda=p.get("ebitda_ltm"),
        ffo=p.get("ffo"), affo=p.get("affo"), nav_ps=p.get("nav_ps"),
        distribution_yield=p.get("distribution_yield"),
    )


def block_reit_snapshot(spec, fund, pack) -> Block:
    subj = _reit_subject_inputs(fund, pack)
    peers = [_reit_peer_inputs(p.slug, pack.peers.get(p.slug, {})) for p in spec.peers]
    xs = build_cross_section(subj, peers, multiples=MULTIPLES_REIT,
                             multiples_fn=company_multiples_reit)
    mm = company_multiples_reit(subj)
    # A filing-derived FFO is an ANNUAL (FY) figure, not trailing-twelve-month — the label stays
    # "FFO (LTM)" (load-bearing, parsed by build_master.py/render_dashboard.py by exact label) but
    # the note says so explicitly rather than silently claiming LTM. See _reit_ffo_fallback().
    ffo_note = _FFO_FALLBACK_NOTES.get(fund.slug, "")
    ffo_source = "filing" if ffo_note else "bbg"
    rows: list[Cell] = [
        Cell("Price", subj.px_last, "price", "bbg"),
        Cell("Shares/CBFIs out (mn)", subj.shares_out, "count", "bbg"),
        Cell("Market cap", subj.market_cap, "currency", "calc"),
        Cell("FFO (LTM)", subj.ffo, "currency", ffo_source, ffo_note),
    ]
    for mkey, label, unit in MULTIPLES_REIT:
        prem = xs.subject_vs_median(mkey)
        note = f"{(prem - 1) * 100:+.0f}% vs peer median" if (prem is not None and unit == "x") else ""
        note = _yahoo_note(pack, mkey, note)
        rows.append(Cell(label, mm[mkey], unit, "calc", note))
    return Block("reit_snapshot", "REIT valuation (vs peers)", rows, cross_section=xs)


def block_reit_metrics(spec, fund, pack) -> Block:
    s = pack.subject
    rev = fund.get("revenue")
    noi = s.get("noi")
    noi_margin = safe_div(noi, rev)
    # guard against a units mismatch (NOI can't exceed revenue) → flag instead of showing nonsense
    noi_margin_pct = noi_margin * 100 if (noi_margin is not None and noi_margin <= 1.2) else None
    noi_note = ("NOI [bbg] / revenue [filing]" if noi_margin_pct is not None
                else ("NOI > revenue — check units/currency" if noi is not None else ""))
    rows = [
        Cell("NOI margin", noi_margin_pct, "pct", "calc", note=noi_note),
        Cell("Occupancy", s.get("occupancy"), "pct", "filing"),
    ]
    return Block("reit_metrics", "Portfolio metrics", rows)


def block_reit_historical(spec, fund, pack) -> Block:
    """Historical EV/Revenue band from XBRL annual rental revenue — the meaningful without-Bloomberg
    history view for FIBRAs (net income is volatile/near-zero for pass-through vehicles, so a P/E
    band is not used). A true P/FFO band needs Bloomberg FFO history."""
    shares = pack.subject.get("shares_out")
    net_debt = pack.subject.get("net_debt")
    rows: list[Cell] = []
    subj = _reit_subject_inputs(fund, pack)
    series: list[float] = []
    for yr in sorted(pack.history):
        px = pack.history[yr]
        a = fund.annual.get(yr, {})
        mc = px * shares if (px is not None and shares is not None) else None
        ev = (mc + net_debt) if (mc is not None and net_debt is not None) else None
        val = safe_div(ev, a.get("revenue"))
        if val is not None:
            series.append(val)
        rows.append(Cell(f"FY{yr}: EV/Revenue", val, "x", "calc",
                         note=(f"px {px:.1f}; current shares/net debt" if px is not None else "no price")))
    cur = safe_div(subj.ev, fund.get("revenue"))
    _band(rows, "EV/Revenue", series, cur)
    if not series:
        rows.append(Cell("Historical band", None, "x", "calc",
                         note="annual revenue unavailable; supply FFO/NAV history via Bloomberg"))
    return Block("reit_historical", "Historical EV/Revenue (mean-reversion)", rows)


_BUILDERS = {
    "snapshot_multiples": block_snapshot,
    "historical_multiples": block_historical,
    "sum_of_the_parts": block_sotp,
    "replacement_value": block_replacement,
    "financial_analysis": block_financial,
    "profitability": block_profitability,
    "fcf_liquidity": block_fcf_liquidity,
    "temporal_ebit": block_temporal,
    "growth": block_growth,
    "macro_sector": block_macro,
    "bank_snapshot": block_bank_snapshot,
    "bank_returns": block_bank_returns,
    "bank_growth": block_bank_growth,
    "bank_historical": block_bank_historical,
    "reit_snapshot": block_reit_snapshot,
    "reit_metrics": block_reit_metrics,
    "reit_historical": block_reit_historical,
}


def build_model(spec, fund, pack) -> CoverageModel:
    """Assemble the full coverage model for the enabled blocks (in spec order)."""
    model = CoverageModel(
        slug=spec.slug, name=spec.name, currency=spec.currency,
        units=spec.units, current_period=getattr(fund, "current_period", ""),
        template=spec.template,
    )
    for block_id in spec.blocks:
        builder = _BUILDERS.get(block_id)
        if builder is None:
            model.warnings.append(f"unknown block {block_id!r} skipped")
            continue
        try:
            model.blocks.append(builder(spec, fund, pack))
        except Exception as e:  # a block failing must not sink the workbook
            model.warnings.append(f"block {block_id} failed: {e}")

    # Resolved inputs (for the independent math audit). Same assembly the blocks used.
    try:
        if spec.template == "financials":
            model.subject_inputs = _bank_subject_inputs(fund, pack)
            model.peer_inputs = [_bank_peer_inputs(p.slug, pack.peers.get(p.slug, {}))
                                 for p in spec.peers]
        elif spec.template == "reit":
            model.subject_inputs = _reit_subject_inputs(fund, pack)
            model.peer_inputs = [_reit_peer_inputs(p.slug, pack.peers.get(p.slug, {}))
                                 for p in spec.peers]
        else:
            model.subject_inputs = _subject_inputs(fund, pack)
            model.peer_inputs = [_peer_inputs(p.slug, pack.peers.get(p.slug, {}))
                                 for p in spec.peers]
    except Exception as e:
        model.warnings.append(f"resolved-inputs snapshot failed: {e}")

    _poison_impossible_cells(model)
    return model


# Blank-not-wrong guardrail. A margin (share of revenue) or an accounting return (ROE/ROA/ROTE/ROIC)
# outside ±100% is not a real business state — it means the numerator or denominator was extracted at
# the wrong scale or period (e.g. Cultiba's post-divestiture holdco shows a one-time gain of 1.3bn on
# 108mn residual revenue → net margin 1218%). Such a value must NEVER ship as a headline number: we
# blank the cell and annotate why, so the deliverable reads as incomplete-here rather than silently
# wrong. Ratios inside the band are untouched, so no real company loses a valid figure.
_MARGIN_BAND = (-100.0, 100.0)          # a margin is a share of revenue → |x| ≤ 100 for any real co
# REITs: net income carries non-cash fair-value gains on investment property, so a FIBRA's net/EBITDA
# margin can legitimately exceed 100% (Fibra Vía ~105%). Widen to ±300% (kept-with-annotation); beyond
# that a revaluation can't plausibly reach, so it's still a mis-scale worth blanking.
_REIT_MARGIN_BAND = (-100.0, 300.0)
_RETURN_BAND = (-100.0, 100.0)          # ROE/ROA/ROTE/ROIC above 100% ⇒ mis-scaled denominator
# Accounting-return labels matched as a whole word in a cell label (e.g. "ROE", "FY2024: ROIC").
_POISON_RETURN_LABELS = ("ROE", "ROA", "ROTE", "ROIC")
# REIT earnings-based cells distorted by non-cash fair-value gains — kept but annotated (per design).
_REIT_FV_DISTORTED = {"P/E (LTM)", "Net margin", "Net income CAGR 5y", "EBITDA YoY (latest)"}


def _poison_impossible_cells(model: "CoverageModel") -> None:
    """Blank margin/return cells whose value is arithmetically impossible (in place).

    Scans every block; a ``pct`` cell that is a margin or a return ratio and lands outside its
    plausible band is set to ``None`` with an explanatory note, and its label is recorded on
    ``model.warnings`` so the validation report surfaces the redaction. Pure/deterministic."""
    import re
    poisoned: list[str] = []
    # A FIBRA's net income legitimately carries unrealised fair-value gains on investment property, so
    # its net/EBITDA margin (share of rental revenue) can exceed 100% in a revaluation quarter — a real
    # figure, not a mis-scale. For REITs we widen the margin band and ANNOTATE-AND-KEEP (rather than
    # blank); a gross mis-scale still trips the wider cap. Returns (ROE/ROA/…) keep the ±100% band.
    is_reit = getattr(model, "template", "") == "reit"
    margin_band = _REIT_MARGIN_BAND if is_reit else _MARGIN_BAND
    for b in model.blocks:
        for c in b.rows:
            if c.value is None or c.unit != "pct":
                continue
            lab = c.label.lower()
            is_margin = "margin" in lab
            band = None
            if is_margin:
                band = margin_band
            elif any(re.search(rf"\b{r}\b", c.label.upper()) for r in _POISON_RETURN_LABELS):
                band = _RETURN_BAND
            if band is None:
                continue
            lo, hi = band
            if not (lo <= c.value <= hi):
                bad = c.value
                c.value = None
                cap = "±300%" if (is_reit and is_margin) else "±100%"
                c.note = (f"blanked — {bad:,.0f}% outside {cap} (impossible {'margin' if is_margin else 'return'}; "
                          f"source facts mis-scaled or one-off) · {c.note}").rstrip(" ·")
                poisoned.append(f"{b.id}:{c.label}={bad:,.0f}%")
            elif is_reit and is_margin and abs(c.value) > 100.0:
                # kept, but disclose the fair-value distortion so the headline isn't read as operating
                c.note = (f"REIT margin {c.value:.0f}% — inflated by non-cash investment-property "
                          f"fair-value gains in net income · {c.note}").rstrip(" ·")
    # REIT earnings-based metrics are distorted by non-cash investment-property fair-value gains
    # (a FIBRA's net income carries revaluations) — kept per the coverage design, but every such cell
    # is annotated so the per-company sheet carries the caveat (the master matrix marks them with †).
    if is_reit:
        for b in model.blocks:
            for c in b.rows:
                if (c.value is not None and c.label in _REIT_FV_DISTORTED
                        and "fair-value" not in (c.note or "")):
                    c.note = ("FIBRA — distorted by non-cash investment-property fair-value gains "
                              f"(use P/FFO, P/NAV, distribution yield) · {c.note}").rstrip(" ·")

    # Cross-margin sanity: within a block, net margin can't exceed EBITDA margin by a wide gap — net
    # income is EBITDA minus D&A, interest and tax, so a net margin far ABOVE the EBITDA margin means
    # the net-income numerator is mis-scaled (Vitro: net 73.9% on EBITDA 23.4% — both under ±100%, so
    # the band above misses it). A generous buffer keeps genuine one-off gains (a modest excess) intact.
    for b in ([] if is_reit else model.blocks):  # REIT net > EBITDA margin is legit (fair-value gains)
        cells = {c.label: c for c in b.rows if c.unit == "pct" and c.value is not None}
        for nm_label, em_label in _CROSS_MARGIN_PAIRS:
            nm, em = cells.get(nm_label), cells.get(em_label)
            if nm and em and nm.value > em.value + _NET_OVER_EBITDA_BUFFER:
                bad = nm.value
                nm.value = None
                nm.note = (f"blanked — net margin {bad:.0f}% exceeds EBITDA margin {em.value:.0f}% "
                           f"(net income mis-scaled) · {nm.note}").rstrip(" ·")
                poisoned.append(f"{b.id}:{nm_label}={bad:,.0f}%>EBITDA")

    if poisoned:
        model.warnings.append("guardrail blanked impossible values: " + "; ".join(poisoned))


# Net margin may exceed EBITDA margin only by a modest one-off (non-operating gain); a wide excess is
# a mis-scaled net income. Buffer is generous so a real one-off quarter isn't blanked.
_NET_OVER_EBITDA_BUFFER = 20.0
_CROSS_MARGIN_PAIRS = (("Net margin", "EBITDA margin"),)

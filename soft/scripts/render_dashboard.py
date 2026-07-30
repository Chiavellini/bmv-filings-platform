#!/usr/bin/env python3
"""Render a company's Soft Coverage CSV into an analyst-friendly, self-contained HTML one-pager.

    python3 scripts/render_dashboard.py "outputs/Banco del Bajio/csv/banco_del_bajio_coverage.csv" \
            --name "Banco del Bajío" --sector "Financials — Bank"

Design: stat tiles for the headline multiples/returns (each with a subject-vs-peer-median bar), a
historical valuation-band chart (the cheap/rich visual), a returns/growth strip, and a macro row.
Provenance ([filing]/[calc]/market) is a single subtle footnote, NOT a column. Theme-aware
(light/dark). No external libraries — inline SVG + CSS only. Palette: validated dataviz default.
"""
from __future__ import annotations

import argparse
import csv
import html
import re
from pathlib import Path

# --- validated dataviz palette (light / dark handled via CSS vars) -----------------------------
ACCENT = "#2a78d6"        # subject
REF = "#9a9a93"           # peer median / history reference (muted)
GOOD = "#1baf7a"          # cheap / below-median
RICH = "#eb6834"          # rich / above-median

# categorical series slots (dataviz default theme, order = CVD-safety) — theme-aware via CSS vars
SERIES_VARS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"]


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def load_csv(path: Path):
    """Return {block: [(label, value, unit, source, note), ...]} preserving order."""
    blocks: dict[str, list] = {}
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            blocks.setdefault(r["block"], []).append(
                (r["label"], _num(r["value"]), r["unit"], r["source"], r.get("note", "")))
    return blocks


def _find(blocks, block, label):
    for lbl, val, unit, src, note in blocks.get(block, []):
        if lbl == label:
            return val, note, src
    return None, "", ""


def _pct_from_note(note):
    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%\s*vs peer median", note or "")
    return float(m.group(1)) if m else None


def fmt(v, unit):
    if v is None:
        return "—"
    if unit == "x":
        return f"{v:.1f}x"
    if unit == "pct":
        return f"{v:.1f}%"
    if unit == "price":
        return f"${v:,.2f}"
    if unit == "currency":
        return f"{v:,.0f}"
    if unit == "count":
        return f"{v:,.0f}"
    return f"{v:,.1f}"


# --- small SVG helpers -------------------------------------------------------------------------
def bar_vs_median(pct):
    """A tiny centered bar showing subject premium/(discount) to peer median, ±40% clamped."""
    if pct is None:
        return ""
    p = max(-40, min(40, pct))
    w = abs(p) / 40 * 46           # half-width 46px
    color = RICH if p > 0 else GOOD
    x = 50 if p >= 0 else 50 - w
    label = f"{pct:+.0f}% vs peers"
    return (f'<svg class="mbar" viewBox="0 0 100 16" width="100" height="16" aria-label="{label}">'
            f'<line x1="50" y1="1" x2="50" y2="15" stroke="var(--grid)" stroke-width="1"/>'
            f'<rect x="{x:.1f}" y="5" width="{w:.1f}" height="6" rx="2" fill="{color}"/>'
            f'</svg><span class="mbar-lbl">{label}</span>')


def hist_band_svg(years, vals, mean, lo, hi, current, cur_label="current", ylabel="P/BV"):
    """Line of FY points with a ±1σ band + mean line + current marker. The headline cheap/rich read."""
    pts = [(y, v) for y, v in zip(years, vals) if v is not None]
    allv = [v for v in vals if v is not None] + [x for x in (mean, lo, hi, current) if x is not None]
    if len(pts) < 2 or not allv:
        return '<p class="muted">Historical band unavailable.</p>'
    W, H, PL, PR, PT, PB = 640, 220, 44, 90, 16, 28
    vmin, vmax = min(allv), max(allv)
    pad = (vmax - vmin) * 0.15 or 0.1
    vmin, vmax = vmin - pad, vmax + pad
    xs = [p[0] for p in pts]
    xmin, xmax = min(xs), max(xs)

    def X(x):
        return PL + (x - xmin) / (xmax - xmin or 1) * (W - PL - PR)

    def Y(v):
        return PT + (vmax - v) / (vmax - vmin or 1) * (H - PT - PB)

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet" '
             f'role="img" aria-label="{ylabel} history vs mean band">']
    # ±1σ band
    if lo is not None and hi is not None:
        parts.append(f'<rect x="{PL}" y="{Y(hi):.1f}" width="{W-PL-PR:.1f}" '
                     f'height="{Y(lo)-Y(hi):.1f}" fill="{ACCENT}" opacity="0.09"/>')
    # mean line
    if mean is not None:
        parts.append(f'<line x1="{PL}" y1="{Y(mean):.1f}" x2="{W-PR}" y2="{Y(mean):.1f}" '
                     f'stroke="{REF}" stroke-width="1.5" stroke-dasharray="4 3"/>')
        parts.append(f'<text x="{W-PR+6}" y="{Y(mean)+4:.1f}" class="svg-t ref">mean {mean:.1f}x</text>')
    if hi is not None:
        parts.append(f'<text x="{W-PR+6}" y="{Y(hi)+4:.1f}" class="svg-t faint">+1σ {hi:.1f}x</text>')
    if lo is not None:
        parts.append(f'<text x="{W-PR+6}" y="{Y(lo)+4:.1f}" class="svg-t faint">−1σ {lo:.1f}x</text>')
    # history line + points
    d = " ".join(f"{'M' if i==0 else 'L'}{X(x):.1f},{Y(v):.1f}" for i, (x, v) in enumerate(pts))
    parts.append(f'<path d="{d}" fill="none" stroke="{ACCENT}" stroke-width="2"/>')
    for x, v in pts:
        parts.append(f'<circle cx="{X(x):.1f}" cy="{Y(v):.1f}" r="3.5" fill="{ACCENT}"/>')
        parts.append(f'<text x="{X(x):.1f}" y="{H-8}" class="svg-t axis" text-anchor="middle">\'{str(x)[2:]}</text>')
    # current marker (drawn at the right edge)
    if current is not None:
        cx = X(xmax)
        parts.append(f'<circle cx="{cx:.1f}" cy="{Y(current):.1f}" r="5.5" fill="none" '
                     f'stroke="{RICH if (mean and current>mean) else GOOD}" stroke-width="2.5"/>')
        parts.append(f'<text x="{cx:.1f}" y="{Y(current)-10:.1f}" class="svg-t cur" '
                     f'text-anchor="middle">{cur_label} {current:.1f}x</text>')
    parts.append("</svg>")
    return "".join(parts)


def _fy_series(blocks, block, name):
    """Return (years, vals) sorted ascending for rows labeled 'FY{yr}: {name}' in `block`."""
    out = []
    for lbl, val, unit, src, note in blocks.get(block, []):
        m = re.match(rf"FY(\d{{4}}): {re.escape(name)}$", lbl)
        if m and val is not None:
            out.append((int(m.group(1)), val))
    out.sort()
    return [y for y, _ in out], [v for _, v in out]


def _legend(items):
    """Inline swatch legend — identity is never color-alone (dataviz a11y rule). items=[(name,color)]."""
    return ('<div class="legend">' + "".join(
        f'<span class="lg"><i style="background:{c}"></i>{html.escape(n)}</span>'
        for n, c in items) + "</div>")


def margin_trend_svg(series_by_metric, years, unit="pct"):
    """Multi-line trend over FYs (modeled on hist_band_svg). series_by_metric is an ordered
    {name: {year: value}}. Legend shown for ≥2 series. Degrades to a muted dash under 2 points."""
    series = [(n, s) for n, s in series_by_metric.items() if s and len(s) >= 2]
    yrs = sorted(years)
    allv = [v for _, s in series for v in s.values()]
    if len(yrs) < 2 or not series or not allv:
        return '<p class="muted">—</p>'
    W, H, PL, PR, PT, PB = 640, 210, 46, 54, 18, 26
    vmin, vmax = min(allv), max(allv)
    pad = (vmax - vmin) * 0.15 or (abs(vmax) * 0.1 or 1)
    vmin, vmax = vmin - pad, vmax + pad
    xmin, xmax = yrs[0], yrs[-1]

    def X(x):
        return PL + (x - xmin) / (xmax - xmin or 1) * (W - PL - PR)

    def Y(v):
        return PT + (vmax - v) / (vmax - vmin or 1) * (H - PT - PB)

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet" '
             f'role="img" aria-label="trend over fiscal years">']
    parts.append(f'<text x="{PL-6}" y="{Y(vmax)+4:.1f}" class="svg-t axis" text-anchor="end">{fmt(vmax,unit)}</text>')
    parts.append(f'<text x="{PL-6}" y="{Y(vmin)+4:.1f}" class="svg-t axis" text-anchor="end">{fmt(vmin,unit)}</text>')
    for x in yrs:
        parts.append(f'<text x="{X(x):.1f}" y="{H-8}" class="svg-t axis" text-anchor="middle">\'{str(x)[2:]}</text>')
    for i, (name, s) in enumerate(series):
        col = SERIES_VARS[i % len(SERIES_VARS)]
        pts = [(y, s[y]) for y in yrs if y in s]
        d = " ".join(f"{'M' if j==0 else 'L'}{X(x):.1f},{Y(v):.1f}" for j, (x, v) in enumerate(pts))
        parts.append(f'<path d="{d}" fill="none" stroke="{col}" stroke-width="2"/>')
        for x, v in pts:
            parts.append(f'<circle cx="{X(x):.1f}" cy="{Y(v):.1f}" r="3.2" fill="{col}"/>')
    parts.append("</svg>")
    svg = "".join(parts)
    if len(series) >= 2:
        svg += _legend([(n, SERIES_VARS[i % len(SERIES_VARS)]) for i, (n, _) in enumerate(series)])
    return svg


def bars_svg(years, series, unit="currency", sign_color=False):
    """Grouped/paired bars over FYs. series = [(name, vals, colorvar), ...] with vals aligned to
    `years` (None allowed for a gap). sign_color: single series coloured by sign (pos/neg tokens,
    like growth_tile). Zero-anchored baseline. Degrades to a muted dash under 2 years / no data."""
    yrs = list(years)
    have = [v for _, vals, _ in series for v in vals if v is not None]
    if len(yrs) < 2 or not have:
        return '<p class="muted">—</p>'
    W, H, PL, PR, PT, PB = 640, 210, 50, 16, 22, 26
    vmax, vmin = max(have + [0.0]), min(have + [0.0])
    pad = (vmax - vmin) * 0.14 or 1
    vmax += pad
    if vmin < 0:
        vmin -= pad

    def Y(v):
        return PT + (vmax - v) / (vmax - vmin or 1) * (H - PT - PB)

    n = len(series)
    band = (W - PL - PR) / len(yrs)
    gap = band * 0.2
    bw = (band - gap) / n
    zero = Y(0)
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet" '
             f'role="img" aria-label="bars over fiscal years">']
    parts.append(f'<text x="{PL-6}" y="{Y(vmax)+4:.1f}" class="svg-t axis" text-anchor="end">{fmt(vmax,unit)}</text>')
    parts.append(f'<text x="{PL-6}" y="{Y(vmin)+4:.1f}" class="svg-t axis" text-anchor="end">{fmt(vmin,unit)}</text>')
    parts.append(f'<line x1="{PL}" y1="{zero:.1f}" x2="{W-PR}" y2="{zero:.1f}" stroke="var(--grid)" stroke-width="1"/>')
    for xi, y in enumerate(yrs):
        bx0 = PL + xi * band + gap / 2
        for si, (name, vals, col) in enumerate(series):
            v = vals[xi] if xi < len(vals) else None
            if v is None:
                continue
            c = ("var(--pos)" if v >= 0 else "var(--neg)") if sign_color else \
                (col or SERIES_VARS[si % len(SERIES_VARS)])
            x = bx0 + si * bw
            top, bot = Y(max(v, 0)), Y(min(v, 0))
            parts.append(f'<rect x="{x:.1f}" y="{top:.1f}" width="{max(bw-2,1):.1f}" '
                         f'height="{max(bot-top,1):.1f}" rx="2" fill="{c}"/>')
            if n == 1:
                ly = top - 4 if v >= 0 else bot + 12
                parts.append(f'<text x="{x+(bw-2)/2:.1f}" y="{ly:.1f}" class="svg-t axis" '
                             f'text-anchor="middle">{fmt(v,unit)}</text>')
        parts.append(f'<text x="{PL+xi*band+band/2:.1f}" y="{H-8}" class="svg-t axis" '
                     f'text-anchor="middle">\'{str(y)[2:]}</text>')
    parts.append("</svg>")
    svg = "".join(parts)
    if n >= 2:
        svg += _legend([(nm, col or SERIES_VARS[i % len(SERIES_VARS)])
                        for i, (nm, _, col) in enumerate(series)])
    return svg


def analysis_sections(blocks):
    """The industrial deep-analysis sections (profitability/temporal_ebit/growth/fcf_liquidity).
    Each section is emitted only when its source block is present; thin data degrades to a dash."""
    out = []

    # 1 — Margin trend (multi-line)
    if "profitability" in blocks:
        sm, ys = {}, set()
        for disp, metric in [("Gross", "Gross margin"), ("EBIT", "EBIT margin"),
                             ("EBITDA", "EBITDA margin"), ("Net", "Net margin")]:
            yrs, vals = _fy_series(blocks, "profitability", metric)
            if len(yrs) >= 2:
                sm[disp] = dict(zip(yrs, vals))
                ys |= set(yrs)
        chart = margin_trend_svg(sm, sorted(ys)) if sm else '<p class="muted">—</p>'
        out.append(f'<section><h2>Margin trend</h2><div class="chart">{chart}</div></section>')

    # 2 — EBIT & EBITDA (grouped bars)
    if "temporal_ebit" in blocks:
        ey, ev = _fy_series(blocks, "temporal_ebit", "EBIT")
        dy, dv = _fy_series(blocks, "temporal_ebit", "EBITDA")
        yrs = sorted(set(ey) | set(dy))
        em, dm = dict(zip(ey, ev)), dict(zip(dy, dv))
        series = [("EBIT", [em.get(y) for y in yrs], "var(--s1)"),
                  ("EBITDA", [dm.get(y) for y in yrs], "var(--s2)")]
        chart = bars_svg(yrs, series, unit="currency")
        out.append(f'<section><h2>EBIT &amp; EBITDA</h2><div class="chart">{chart}</div></section>')

    # 3 — Growth (signed bars + CAGR tiles)
    if "growth" in blocks:
        gy, gv = _fy_series(blocks, "growth", "Revenue YoY")
        chart = bars_svg(gy, [("Revenue YoY", gv, None)], unit="pct", sign_color=True)
        tiles = "".join(growth_tile(lbl, _find(blocks, blk, lbl)[0]) for blk, lbl in [
            ("growth", "Revenue CAGR 5y"), ("temporal_ebit", "EBITDA CAGR 5y"),
            ("growth", "Net income CAGR 5y")])
        sig, _, _ = _find(blocks, "growth", "Revenue growth stability (σ)")
        tiles += tile("Revenue growth stability (σ)", sig, "pct")
        out.append(f'<section><h2>Growth &amp; CAGRs</h2><div class="chart">{chart}</div>'
                   f'<div class="tiles" style="margin-top:14px">{tiles}</div></section>')

    # 4 & 5 — FCF, liquidity & inventory
    if "fcf_liquidity" in blocks:
        labels = ["FCF yield", "Current ratio", "Quick ratio",
                  "Cash conversion cycle (days)", "Inventory turns"]
        tiles = "".join(tile(l, *_find_v(blocks, "fcf_liquidity", l)) for l in labels)
        out.append(f'<section><h2>FCF &amp; liquidity</h2><div class="tiles">{tiles}</div></section>')
        iy, iv = _fy_series(blocks, "fcf_liquidity", "Inventory days")
        inv = margin_trend_svg({"Inventory days": dict(zip(iy, iv))}, sorted(iy), unit="count")
        out.append(f'<section><h2>Inventory days</h2><div class="chart">{inv}</div></section>')

    return "".join(out)


# --- section builders --------------------------------------------------------------------------
def tile(label, value, unit, note=""):
    # only the peer-median comparison earns a sub-line (a small bar); other notes are formula
    # explanations that clutter the metric — dropped by design.
    sub = bar_vs_median(_pct_from_note(note)) if "vs peer median" in (note or "") else ""
    return (f'<div class="tile"><div class="tile-v">{fmt(value, unit)}</div>'
            f'<div class="tile-l">{html.escape(label)}</div>{sub}</div>')


def growth_tile(label, value):
    """A directional metric: explicit +/- sign, colored by sign, verbose FY suffix stripped."""
    if value is None:
        return tile(label, value, "pct")
    cls = "pos" if value >= 0 else "neg"
    label = re.sub(r"\s*FY\s*\d{4}\s*[→\-–].*$", "", label).strip()   # "…growth FY2024→FY2025" → "…growth"
    return (f'<div class="tile"><div class="tile-v {cls}">{value:+.1f}%</div>'
            f'<div class="tile-l">{html.escape(label)}</div></div>')


def build_html(blocks, name, sector):
    is_bank = "bank_snapshot" in blocks
    snap = "bank_snapshot" if is_bank else "snapshot_multiples"
    hist = "bank_historical" if is_bank else "historical_multiples"
    ret = "bank_returns" if is_bank else "financial_analysis"
    grow = "bank_growth" if is_bank else None

    price, _, _ = _find(blocks, snap, "Price")
    mcap, _, _ = _find(blocks, snap, "Market cap")
    bv, _, _ = _find(blocks, snap, "Book value")

    # headline notion — anchored on the multiple the history band shows (P/BV for banks, P/E for the rest)
    key_label = "P/BV" if is_bank else "P/E (LTM)"
    mult_name = "P/BV" if is_bank else "P/E"
    kv, kv_note, _ = _find(blocks, snap, key_label)
    hmean, _, _ = _find(blocks, hist, f"{mult_name} — historical mean")
    notion = ""
    if kv is not None:
        bits = [f"Trades at <b>{kv:.1f}x {mult_name}</b>"]
        prem = _pct_from_note(kv_note)
        if prem is not None:
            bits.append(f"{abs(prem):.0f}% {'above' if prem>0 else 'below'} peer median")
        if hmean:
            side = "above" if kv > hmean else "below"
            bits.append(f"{side} its 5-yr mean of {hmean:.1f}x")
        notion = " · ".join(bits) + "."

    # valuation tiles
    val_labels = (["P/E (LTM)", "P/BV", "P/TBV", "Dividend yield"] if is_bank
                  else ["P/E (LTM)", "EV/EBITDA", "P/BV", "Dividend yield"])
    val_tiles = "".join(tile(l, *_find_v(blocks, snap, l)) for l in val_labels)

    # returns tiles
    ret_labels = (["ROE", "ROTE", "Net interest margin (NIM)", "CET1 ratio"] if is_bank
                  else ["ROE", "ROIC", "EBITDA margin", "Net margin"])
    ret_tiles = "".join(tile(l, *_find_v(blocks, ret, l)) for l in ret_labels)

    # growth tiles — directional, so show an explicit +/- sign and color by sign. Banks carry a
    # dedicated bank_growth block; industrials keep revenue growth inside financial_analysis.
    grow_block = grow if is_bank else ret
    growth_rows = [(l, v) for (l, v, u, s, n) in blocks.get(grow_block or "", [])
                   if v is not None and "growth" in l.lower()]
    grow_tiles = "".join(growth_tile(l, v) for l, v in growth_rows) or \
        '<p class="muted">—</p>'

    # historical band
    years, pv = [], []
    band_metric = "P/BV" if is_bank else "P/E"     # one series only — the primary valuation multiple
    for lbl, val, unit, src, note in blocks.get(hist, []):
        m = re.match(rf"FY(\d{{4}}): {re.escape(band_metric)}$", lbl)
        if m and val is not None:
            years.append(int(m.group(1)))
            pv.append(val)
    mean, _, _ = _find(blocks, hist, "P/BV — historical mean") if is_bank else _find(blocks, hist, "P/E — historical mean")
    hi, _, _ = _find(blocks, hist, "P/BV — +1σ") if is_bank else _find(blocks, hist, "P/E — +1σ")
    lo, _, _ = _find(blocks, hist, "P/BV — −1σ") if is_bank else _find(blocks, hist, "P/E — −1σ")
    cur_hist = kv if is_bank else _find(blocks, snap, "P/E (LTM)")[0]
    band = hist_band_svg(years, pv, mean, lo, hi, cur_hist, ylabel=("P/BV" if is_bank else "P/E"))

    # deeper financial-analysis sections — industrials only; banks keep their current layout
    extra = "" if is_bank else analysis_sections(blocks)

    # macro strip
    macro = "".join(f'<span class="mac"><b>{fmt(v,u)}</b> {html.escape(l)}</span>'
                    for (l, v, u, s, n) in blocks.get("macro_sector", []) if v is not None)

    subline = " · ".join(x for x in [
        f"Price {fmt(price,'price')}" if price is not None else "",
        f"Mkt cap {fmt(mcap,'currency')} mn" if mcap is not None else "",
        f"Book value {fmt(bv,'currency')} mn" if bv is not None else "",
    ] if x)

    return f"""<div class="wrap">
  <header>
    <div class="eyebrow">Soft Coverage · MXN mn · FY2025</div>
    <h1>{html.escape(name)}</h1>
    <div class="sector">{html.escape(sector)}</div>
    <div class="subline">{subline}</div>
    {f'<p class="notion">{notion}</p>' if notion else ''}
  </header>

  <section>
    <h2>Valuation</h2>
    <div class="tiles">{val_tiles}</div>
  </section>

  <section>
    <h2>Where it trades vs history</h2>
    <div class="chart">{band}</div>
  </section>

  <div class="two">
    <section><h2>Returns &amp; capital</h2><div class="tiles">{ret_tiles}</div></section>
    <section><h2>Growth</h2><div class="tiles">{grow_tiles}</div></section>
  </div>

  {extra}

  {f'<section><h2>Macro backdrop</h2><div class="macro">{macro}</div></section>' if macro else ''}

  <footer>
    Values are real: fundamentals &amp; returns from the company's XBRL filings; multiples use the
    live market price; NIM/CET1 from the CNBV annual report; dividend yield = declared DPS ÷ price;
    macro = latest published Banxico/INEGI figures. Peer comparison is the median of listed peers.
  </footer>
</div>"""


def _find_v(blocks, block, label):
    v, note, _ = _find(blocks, block, label)
    unit = "x"
    for lbl, val, u, s, n in blocks.get(block, []):
        if lbl == label:
            unit = u
            break
    return v, unit, note


STYLE = """
:root{
 --surface:#f7f8fa;--card:#ffffff;--text:#0d1017;--text2:#4a5160;--muted:#8b93a3;
 --grid:#e4e7ec;--line:#e9ecf1;--accent:#1f6fd6;--accent-soft:#eaf1fb;
 --pos:#0b8f5e;--neg:#d1503a;--shadow:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.06);
}
@media (prefers-color-scheme:dark){:root{
 --surface:#0e1116;--card:#171b22;--text:#f2f4f8;--text2:#aeb6c4;--muted:#727c8c;
 --grid:#262c36;--line:#232833;--accent:#4f97ec;--accent-soft:#16233a;
 --pos:#33b985;--neg:#e5765f;--shadow:0 1px 2px rgba(0,0,0,.3);
}}
:root[data-theme="light"]{--surface:#f7f8fa;--card:#ffffff;--text:#0d1017;--text2:#4a5160;
 --muted:#8b93a3;--grid:#e4e7ec;--line:#e9ecf1;--accent:#1f6fd6;--accent-soft:#eaf1fb;
 --pos:#0b8f5e;--neg:#d1503a;--shadow:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.06);}
:root[data-theme="dark"]{--surface:#0e1116;--card:#171b22;--text:#f2f4f8;--text2:#aeb6c4;
 --muted:#727c8c;--grid:#262c36;--line:#232833;--accent:#4f97ec;--accent-soft:#16233a;
 --pos:#33b985;--neg:#e5765f;--shadow:0 1px 2px rgba(0,0,0,.3);}
*{box-sizing:border-box}
body{margin:0;background:var(--surface);color:var(--text);
font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
-webkit-font-smoothing:antialiased;}
.tile-v,.mac b,.svg-t,td,.subline{font-variant-numeric:tabular-nums;}
.wrap{max-width:920px;margin:0 auto;padding:38px 26px 52px;}
.eyebrow{font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--accent);font-weight:700;}
h1{margin:.2em 0 .04em;font-size:32px;line-height:1.08;letter-spacing:-.025em;text-wrap:balance;font-weight:720;}
.sector{color:var(--text2);font-weight:600;font-size:13.5px;letter-spacing:.01em;}
.subline{color:var(--text2);font-size:13.5px;margin-top:8px;}
.notion{margin:16px 0 0;padding:14px 16px;background:var(--accent-soft);
border:1px solid color-mix(in srgb,var(--accent) 22%,transparent);
border-radius:12px;font-size:15.5px;line-height:1.5;color:var(--text);}
.notion b{color:var(--accent);font-weight:700;}
section{margin-top:30px;}
h2{font-size:11.5px;letter-spacing:.11em;text-transform:uppercase;color:var(--muted);
font-weight:700;margin:0 0 13px;border-bottom:1px solid var(--line);padding-bottom:7px;}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:13px;}
.two{display:grid;grid-template-columns:1fr 1fr;gap:26px;}
.two .tiles{grid-template-columns:1fr 1fr;}
.tile{background:var(--card);border:1px solid var(--line);border-radius:13px;
padding:15px 15px 13px;box-shadow:var(--shadow);}
.tile-v{font-size:25px;font-weight:700;letter-spacing:-.02em;line-height:1.1;}
.tile-v.pos{color:var(--pos);} .tile-v.neg{color:var(--neg);}
.tile-l{font-size:11.5px;color:var(--text2);margin-top:4px;font-weight:600;letter-spacing:.01em;}
.tile-sub{display:block;font-size:11.5px;color:var(--muted);margin-top:6px;}
.mbar{display:block;margin-top:9px;}
.mbar-lbl{font-size:11px;color:var(--muted);font-weight:600;}
.chart{background:var(--card);border:1px solid var(--line);border-radius:13px;
padding:16px 10px 10px;box-shadow:var(--shadow);}
.svg-t{font:11px ui-sans-serif,sans-serif;fill:var(--text2);}
.svg-t.axis{fill:var(--muted);}
.svg-t.ref{fill:var(--muted);font-weight:600;}
.svg-t.faint{fill:var(--muted);opacity:.7;}
.svg-t.cur{fill:var(--text);font-weight:700;}
.macro{display:flex;flex-wrap:wrap;gap:10px;}
.mac{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:8px 14px;
font-size:13px;color:var(--text2);box-shadow:var(--shadow);} .mac b{color:var(--text);font-weight:700;}
.muted{color:var(--muted);}
/* categorical series slots — dataviz default theme, stepped for each surface */
:root{--s1:#2a78d6;--s2:#1baf7a;--s3:#eda100;--s4:#eb6834;}
@media(prefers-color-scheme:dark){:root{--s1:#3987e5;--s2:#199e70;--s3:#c98500;--s4:#d95926;}}
:root[data-theme="light"]{--s1:#2a78d6;--s2:#1baf7a;--s3:#eda100;--s4:#eb6834;}
:root[data-theme="dark"]{--s1:#3987e5;--s2:#199e70;--s3:#c98500;--s4:#d95926;}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:10px 4px 2px;}
.lg{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:var(--text2);font-weight:600;}
.lg i{width:11px;height:11px;border-radius:3px;display:inline-block;flex:0 0 auto;}
footer{margin-top:38px;padding-top:15px;border-top:1px solid var(--line);
font-size:11.5px;color:var(--muted);line-height:1.65;}
@media(max-width:680px){.tiles{grid-template-columns:repeat(2,1fr);}.two{grid-template-columns:1fr;}
.wrap{padding:28px 18px 40px;}h1{font-size:27px;}}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--name", required=True)
    ap.add_argument("--sector", default="")
    ap.add_argument("--out", default=None)
    ap.add_argument("--artifact", action="store_true",
                    help="emit <style>+body only (no doctype/html/head/body) for the Artifact skeleton")
    args = ap.parse_args()
    blocks = load_csv(Path(args.csv))
    body = build_html(blocks, args.name, args.sector)
    if args.artifact:
        doc = f"<style>{STYLE}</style>{body}"
    else:
        doc = (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
               f"<meta name=viewport content='width=device-width,initial-scale=1'>"
               f"<title>{html.escape(args.name)} — Soft Coverage</title><style>{STYLE}</style></head>"
               f"<body>{body}</body></html>")
    default = Path(args.csv).parent.parent / (
        f"{Path(args.csv).stem.replace('_coverage','')}_soft_cov{'_artifact' if args.artifact else ''}.html")
    out = Path(args.out) if args.out else default
    out.write_text(doc, encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

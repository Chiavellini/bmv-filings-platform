#!/usr/bin/env python3
"""Build the master Soft-Coverage workbook: one row per company, many metric columns.

    python3 scripts/build_master.py                       # whole universe
    python3 scripts/build_master.py --only walmex,gfnorte
    python3 scripts/build_master.py --only walmex --no-render

For each company it runs the same pipeline as ``build_all.py`` (via ``build_one``, with the same
offline/fast defaults so a batch actually completes), pulls the headline LTM/summary metrics named
in ``docs/ANALYSIS_METRICS.md`` §C, and lays them into ONE worksheet:

  * grouped column bands — Valuation | Profitability | Growth | FCF/Liquidity
  * rows banded by sector (sector sub-header, companies sorted within)
  * money/pct/x/number formats, red negatives, header + sector-band fills
  * the Company cell hyperlinks to that company's rich HTML artifact
  * peer-benchmark 3-colour ``ColorScaleRule`` down each numeric metric column

Outputs ``outputs/_master/soft_coverage_master.{xlsx,csv}``. With ``--render`` (default ON) it also
re-renders each emitting company's ``<slug>_soft_cov.html`` so the hyperlinks resolve to fresh pages.
"""
from __future__ import annotations

import argparse
import csv
import html as _html
import re as _re
import statistics
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openpyxl import Workbook  # noqa: E402
from openpyxl.comments import Comment  # noqa: E402
from openpyxl.formatting.rule import ColorScaleRule  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

from scripts.build_coverage import BuildResult, build_one  # noqa: E402
from scripts.gen_universe import UNIVERSE, slugify  # noqa: E402
from scripts.render_dashboard import STYLE, build_html, load_csv  # noqa: E402
from src.coverage.applicability import rationale, row_applicability  # noqa: E402
from src.coverage.columns import COLKEYS, COLUMNS, FIXED_HEADERS, N_FIXED  # noqa: E402
from src.coverage.peers import plausible  # noqa: E402
from src.coverage.profiles import OFF, profile_flags  # noqa: E402

# --------------------------------------------------------------------------------------------------
# Style constants (reused/adapted from src/excel/segments_sheet.py — kept self-contained here so the
# master builder never imports the parent-repo excel module).
# --------------------------------------------------------------------------------------------------
_FONT = "Calibri"
_WHITE = "FFFFFFFF"
_INK = "FF1F2A44"

FONT_TITLE = Font(name=_FONT, size=14, bold=True, color=_INK)
FONT_BAND = Font(name=_FONT, size=10, bold=True, color=_WHITE)
FONT_HEADER = Font(name=_FONT, size=9, bold=True, color=_WHITE)
FONT_SECTOR = Font(name=_FONT, size=9, bold=True, color=_INK)
FONT_LABEL = Font(name=_FONT, size=10, bold=True, color=_INK)
FONT_VALUE = Font(name=_FONT, size=10, color=_INK)
FONT_LINK = Font(name=_FONT, size=10, bold=True, color="FF1F6FD6", underline="single")

# Band header fills — one accent per group so the four bands read at a glance.
FILL_BAND = {
    "Company": PatternFill("solid", fgColor="FF344054"),
    "Valuation": PatternFill("solid", fgColor="FF1F6FD6"),
    "Profitability": PatternFill("solid", fgColor="FF0B8F5E"),
    "Growth": PatternFill("solid", fgColor="FF8A5AD6"),
    "Leverage & Liquidity": PatternFill("solid", fgColor="FFB4690E"),
    "Capital (banks)": PatternFill("solid", fgColor="FF0E7C86"),
}
FILL_HEADER = PatternFill("solid", fgColor="FF475467")
FILL_SECTOR = PatternFill("solid", fgColor="FFE7ECF3")
# N/A vs GAP cell styling. N/A (structural or no-free-source) → muted grey "N/A" text; GAP (an
# applicable cell we failed to fill — the thing certification forbids) → loud light-red fill.
FONT_NA = Font(name=_FONT, size=9, italic=True, color="FF98A2B3")
FILL_NA = PatternFill("solid", fgColor="FFF2F4F7")
FILL_GAP = PatternFill("solid", fgColor="FFFDE7E9")

BORDER_BOTTOM = Border(bottom=Side(style="thin", color="FFB0B8C4"))
ALIGN_LEFT = Alignment(horizontal="left", vertical="center")
ALIGN_RIGHT = Alignment(horizontal="right", vertical="center")
ALIGN_CENTER = Alignment(horizontal="center", vertical="center")

# Number formats. Percentages are stored as WHOLE numbers (12.3 == 12.3%), matching the model's
# convention — so a literal-"%" format is used, NOT Excel's ×100 percent format. Negatives red.
NUMFMT = {
    "pct": '#,##0.0"%";[Red]\\-#,##0.0"%"',
    "x": '#,##0.00"x";[Red]\\-#,##0.00"x"',
    "count": '#,##0;[Red]\\-#,##0',
    "ratio": '#,##0.00;[Red]\\-#,##0.00',
    "currency": '"$"\\ #,##0.0;[Red]\\("$"\\ #,##0.0\\)',
}

# 3-colour scale endpoints (Excel's classic red-yellow-green).
_RED, _YEL, _GRN = "F8696B", "FFEB84", "63BE7B"


def _color_scale(good: str) -> ColorScaleRule:
    """Green=best. ``good='high'`` → high values green; ``good='low'`` → low values green."""
    lo, hi = (_RED, _GRN) if good == "high" else (_GRN, _RED)
    return ColorScaleRule(start_type="min", start_color=lo,
                          mid_type="percentile", mid_value=50, mid_color=_YEL,
                          end_type="max", end_color=hi)


# Column contract (COLUMNS / FIXED_HEADERS / N_FIXED) now lives in src/coverage/columns.py — the
# single source of truth shared with src/coverage/applicability.py (the N/A map).


# --------------------------------------------------------------------------------------------------
# Roster / metric extraction
# --------------------------------------------------------------------------------------------------
def roster():
    """Yield (slug, name, clave, template, sector) in UNIVERSE (sector) order."""
    for sector, (template, members) in UNIVERSE.items():
        for clave, name in members:
            yield slugify(name), name, clave, template, sector


def _pretty_sector(sector: str) -> str:
    return sector.replace("_", " ").title()


def model_to_map(model) -> dict:
    """{block_id: {label: value}} from a CoverageModel."""
    out: dict[str, dict] = {}
    if model is None:
        return out
    for b in model.blocks:
        d = out.setdefault(b.id, {})
        for c in b.rows:
            d[c.label] = c.value
    return out


def csv_to_map(csv_path: Path) -> dict:
    """{block_id: {label: value}} from a written coverage CSV (values already float|None)."""
    out: dict[str, dict] = {}
    for bid, rows in load_csv(csv_path).items():
        out[bid] = {lbl: val for (lbl, val, _u, _s, _n) in rows}
    return out


def pick(vmap: dict, block_id: str, label: str):
    """Headline metric value, or None when absent (blank cell — never fabricated)."""
    return vmap.get(block_id, {}).get(label)


# Template-alternate sources: banks/REITs emit the same shared metric under a different block
# (and, for yield, a different label). Without this, every bank & FIBRA row is blank because the
# COLUMNS block ids are industrial-only. We only map metrics that are semantically the SAME across
# templates — P/E, dividend/distribution yield, ROE. Template-specific metrics (bank P/BV·NIM·CET1,
# REIT P/FFO·occupancy·cap-rate) are intentionally NOT force-fit into industrial columns.
ALT_SOURCES = {
    ("snapshot_multiples", "P/E (LTM)"): [("bank_snapshot", "P/E (LTM)")],
    ("snapshot_multiples", "Dividend yield"): [("bank_snapshot", "Dividend yield"),
                                               ("reit_snapshot", "Distribution yield")],
    ("financial_analysis", "P/BV"): [("bank_snapshot", "P/BV")],
    ("financial_analysis", "ROE"): [("bank_returns", "ROE")],
}


def _latest_fy_yoy(vmap: dict, metric: str = "Revenue"):
    """The latest fiscal-year YoY for ``metric`` (Revenue or Net income), from the per-FY YoY rows the
    growth block always emits (``FY{year}: {metric} YoY``). Rev/NI CAGR 1y == these (max-year) — so a
    company whose cached CSV predates the explicit ``{metric} CAGR 1y`` row still fills the 1y column
    from data already present, equal to ``block_growth``'s fresh ``*_yoy_vals[-1]``."""
    rx = _re.compile(rf"FY(\d+): {_re.escape(metric)} YoY")
    best_yr, best_val = None, None
    for lbl, val in vmap.get("growth", {}).items():
        m = rx.fullmatch(lbl)
        if m and val is not None and (best_yr is None or int(m.group(1)) > best_yr):
            best_yr, best_val = int(m.group(1)), val
    return best_val


# Rev/NI CAGR 1y derive from the latest FY-YoY row (present even in CSVs predating the 1y columns).
_CAGR_1Y_DERIVE = {
    ("growth", "Revenue CAGR 1y"): "Revenue",
    ("growth", "Net income CAGR 1y"): "Net income",
}


def pick_col(vmap: dict, block_id: str, label: str):
    """pick() with template-alternate fallbacks so bank/REIT rows populate their shared metrics."""
    v = pick(vmap, block_id, label)
    if v is not None:
        return v
    metric = _CAGR_1Y_DERIVE.get((block_id, label))
    if metric is not None:
        d = _latest_fy_yoy(vmap, metric)
        if d is not None:
            return d
    for alt_blk, alt_lbl in ALT_SOURCES.get((block_id, label), ()):
        v = pick(vmap, alt_blk, alt_lbl)
        if v is not None:
            return v
    return None


def cell_state(row: dict, colkey: tuple) -> str:
    """One of 'filled' | 'na' | 'gap' for a (row, column). 'na' = structural or no-free-source
    (excluded from the coverage denominator); 'gap' = applicable but blank (forbidden by certify);
    'filled' = has a value. A row with no ``applic`` map treats every blank as a gap.

    ``na_template`` ALWAYS renders 'na', even when a value was computed — e.g. FIBRA P/E computes
    fine (net income is real, just contaminated by non-cash fair-value gains) but is analytically
    meaningless, so showing the number would be worse than blanking it. This differs from
    ``na_source``, where residual_na's "value always wins" rule still applies (a reduced cell is a
    fill-availability guess, not an analytical judgement, so a value must never be hidden).
    """
    status = row.get("applic", {}).get(colkey, "applicable")
    if status == "na_template":
        return "na"
    if row["values"].get(colkey) is not None:
        return "filled"
    return "na" if status == "na_source" else "gap"


def coverage_counts(rows: list[dict]) -> tuple[int, int, int]:
    """(n_filled, n_applicable, n_gap) across the whole matrix. Coverage% = n_filled / n_applicable
    counts only applicable-free cells — N/A never dilutes it. n_gap is the honest shortfall."""
    n_filled = n_applicable = n_gap = 0
    for row in rows:
        for ck in COLKEYS:
            st = cell_state(row, ck)
            if st == "na":
                continue
            n_applicable += 1
            if st == "filled":
                n_filled += 1
            else:
                n_gap += 1
    return n_filled, n_applicable, n_gap


# --------------------------------------------------------------------------------------------------
# Master-level PLAUSIBILITY gate. The per-company math_audit checks arithmetic *identities* only, so
# a mis-scaled-but-self-consistent value (e.g. a mis-scaled revenue → 1217% net margin) passes and
# would land in the matrix. This blanks any value outside an economically-sane band — an honest
# blank, never a fabricated one — and also protects the peer colour-scales from single garbage cells.
# Multiples reuse peers.plausible (the same bands the engine already applies to peer columns).
# --------------------------------------------------------------------------------------------------
_MULTIPLE_MKEY = {
    "P/E (LTM)": "pe_ltm", "EV/EBITDA": "ev_ebitda",
    "P/BV": "pbv", "P/TBV": "ptbv", "P/FFO": "p_ffo", "P/NAV": "p_nav",
}
_PCT_BAND = {  # margins & returns & pct ratios (values are whole-number percents)
    "EBITDA margin": (-100, 100), "Net margin": (-100, 100),
    "EBIT margin (LTM)": (-100, 100), "NOI margin": (-100, 100),
    "ROE": (-100, 150), "ROIC": (-100, 150), "ROTE": (-100, 150),
    "DuPont — implied ROE": (-100, 150),
    "Dividend yield": (0, 30), "FCF yield": (-50, 50),
    "Net interest margin (NIM)": (0, 30), "Efficiency ratio": (0, 120),
    "Cost of risk": (0, 15),
    "CET1 ratio": (0, 60), "Occupancy": (0, 100), "Cap rate": (0, 30),
    "Loan-to-value (LTV)": (0, 100),
    "Revenue CAGR 1y": (-100, 400), "Net income CAGR 1y": (-100, 400),
    "Revenue CAGR 5y": (-100, 400), "Net income CAGR 5y": (-100, 400),
    "Revenue growth stability (σ)": (0, 400),
    "EBITDA CAGR 5y": (-100, 400), "EBITDA YoY (latest)": (-100, 400),
}
_RATIO_BAND = {  # x / count columns
    "Current ratio": (0, 20), "Quick ratio": (0, 20),
    "Inventory turns": (0, 100), "Cash conversion cycle (days)": (-400, 800),
}


# Margin columns whose ±100% ceiling is widened for REITs: a FIBRA's net income carries non-cash
# investment-property fair-value gains, so its margin can legitimately exceed 100% (mirrors the
# valuation-layer widening in valuation._REIT_MARGIN_BAND — the guard lives in two places, and the
# two must agree). "Net margin" is now na_template for fibras/real_estate (configs/
# metric_applicability.yaml) so this widened band never actually reaches the master matrix — the
# admitted value sits unused in row["values"] — but the guard is kept for consistency with
# valuation.py's identical widening, which DOES still surface in each company's own coverage sheet.
_REIT_WIDE_MARGINS = {"Net margin", "EBITDA margin", "EBIT margin (LTM)", "NOI margin"}

# Muted background for a shown-but-not-comparable value (a loss-maker's negative P/E) — never green.
_NEUTRAL_BG = "rgb(238,240,243)"


def _admit_cell(label: str, unit: str, value, template: str = ""):
    """Return ``value`` (as float) if plausible for its column, else None (blank). Never fabricates."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    # Show every P/E — a loss-maker's NEGATIVE and a near-zero-earnings name's HUGE multiple are the real
    # values (per the user), not blanks. Only a raw scale artifact is rejected (|P/E| ≤ 1000); a
    # structural scale/shares defect is already poisoned upstream by `_guardrail_blank`. The HTML heatmap
    # renders a non-positive/off-scale P/E neutrally so it never reads as the "best" (greenest) cell.
    if label == "P/E (LTM)":
        return v if -1000.0 <= v <= 1000.0 else None
    mkey = _MULTIPLE_MKEY.get(label)
    if mkey is not None:
        return v if plausible(mkey, v) else None
    band = _PCT_BAND.get(label) or _RATIO_BAND.get(label)
    if band is not None:
        lo, hi = band
        if template == "reit" and label in _REIT_WIDE_MARGINS:
            hi = 300  # fair-value-inflated REIT margin — kept (valuation layer already annotated it)
        return v if lo <= v <= hi else None
    return v  # no band defined for this column → pass through


# Valuation multiples whose value depends on the (shares → market cap → EV) chain. When a company
# trips a network-free STRUCTURAL scale/shares defect, these are untrustworthy even if each lands in
# its per-cell band (a wrong-but-plausible number), so the guardrail blanks the whole set.
_SHARES_POISONED = {"P/E (LTM)", "P/E (fwd)", "EV/EBITDA", "EV/Sales",
                    "P/BV", "P/TBV", "P/FFO", "P/NAV", "P/FCF"}


def _guardrail_blank(name: str, slug: str, template: str) -> set[str]:
    """Column labels to force-blank for one company because ``structural_checks`` (network-free) found
    a real scale/shares defect that the per-cell bands miss. This is the ``mktcap == px×shares`` /
    ``net ≤ EBITDA`` / sane-P/S family from the reconciler, applied at BUILD time so a wrong-but-in-band
    multiple never silently enters the matrix. Reuses ``reconcile.structural_checks`` — no new rules."""
    from src.coverage.reconcile import read_coverage_values, structural_checks
    cv = read_coverage_values(name, slug, template)
    if cv is None:
        return set()
    poisoned: set[str] = set()
    for f in structural_checks(name, cv):
        if f.kind != "real":
            continue
        if f.root_cause in ("shares/revenue scale", "shares/price"):
            poisoned |= _SHARES_POISONED          # shares/market-cap basis corrupt → all multiples
        elif f.root_cause == "net_income scale":
            poisoned |= {"P/E (LTM)", "Net margin"}  # net income mis-scaled → NI-derived cells
        elif f.metric in ("EBITDA margin", "Net margin", "Gross margin"):
            poisoned.add(f.metric)                 # impossible margin (also caught by the pct band)
    return poisoned


# --------------------------------------------------------------------------------------------------
# HTML artifact render (so hyperlinks resolve to fresh pages)
# --------------------------------------------------------------------------------------------------
def render_artifact(name: str, slug: str, sector: str) -> Path | None:
    """Re-render outputs/<Name>/<slug>_soft_cov.html from the emitted coverage CSV. None if no CSV."""
    csv_path = ROOT / "outputs" / name / "csv" / f"{slug}_coverage.csv"
    if not csv_path.exists():
        return None
    blocks = load_csv(csv_path)
    body = build_html(blocks, name, _pretty_sector(sector))
    doc = (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
           f"<meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>{_html.escape(name)} — Soft Coverage</title><style>{STYLE}</style></head>"
           f"<body>{body}</body></html>")
    out = ROOT / "outputs" / name / f"{slug}_soft_cov.html"
    out.write_text(doc, encoding="utf-8")
    return out


# --------------------------------------------------------------------------------------------------
# Workbook writer
# --------------------------------------------------------------------------------------------------
def write_workbook(rows: list[dict], xlsx_path: Path) -> tuple[int, int]:
    """rows: [{slug,name,clave,template,sector,values:{(block,label):v}, has_html:bool}] grouped by
    sector (already ordered). Returns (data_rows_written, rows_with_any_metric)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Master"

    n_metrics = len(COLUMNS)
    total_cols = N_FIXED + n_metrics

    # ---- Row 1: band labels (merged over each group) ----------------------------------------
    ws.cell(1, 1, "Soft Coverage — Master").font = FONT_TITLE
    # fixed-columns band
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=N_FIXED)
    bandcell = ws.cell(1, 1)
    bandcell.value = "Soft Coverage — Master"
    bandcell.fill = FILL_BAND["Company"]
    bandcell.font = FONT_BAND
    bandcell.alignment = ALIGN_LEFT
    # metric bands
    col = N_FIXED + 1
    i = 0
    while i < n_metrics:
        band = COLUMNS[i][0]
        j = i
        while j < n_metrics and COLUMNS[j][0] == band:
            j += 1
        start_c, end_c = col + i, col + j - 1
        ws.merge_cells(start_row=1, start_column=start_c, end_row=1, end_column=end_c)
        c = ws.cell(1, start_c, band)
        c.fill = FILL_BAND.get(band, FILL_HEADER)
        c.font = FONT_BAND
        c.alignment = ALIGN_CENTER
        i = j

    # ---- Row 2: column headers --------------------------------------------------------------
    for k, h in enumerate(FIXED_HEADERS, start=1):
        c = ws.cell(2, k, h)
        c.font = FONT_HEADER
        c.fill = FILL_HEADER
        c.alignment = ALIGN_LEFT if k == 1 else ALIGN_CENTER
        c.border = BORDER_BOTTOM
    for k, (_band, _blk, _lbl, header, _unit, _good) in enumerate(COLUMNS):
        c = ws.cell(2, N_FIXED + 1 + k, header)
        c.font = FONT_HEADER
        c.fill = FILL_HEADER
        c.alignment = ALIGN_CENTER
        c.border = BORDER_BOTTOM

    ws.freeze_panes = f"{get_column_letter(N_FIXED + 1)}3"

    # ---- Data rows, banded by sector --------------------------------------------------------
    r = 3
    first_data_row = None
    n_with_data = 0
    # preserve sector order as it appears in `rows`
    seen_sectors: list[str] = []
    by_sector: dict[str, list[dict]] = {}
    for row in rows:
        by_sector.setdefault(row["sector"], []).append(row)
        if row["sector"] not in seen_sectors:
            seen_sectors.append(row["sector"])

    for sector in seen_sectors:
        # sector band row
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=total_cols)
        sc = ws.cell(r, 1, _pretty_sector(sector))
        sc.fill = FILL_SECTOR
        sc.font = FONT_SECTOR
        sc.alignment = ALIGN_LEFT
        r += 1
        for row in sorted(by_sector[sector], key=lambda x: x["name"].lower()):
            if first_data_row is None:
                first_data_row = r
            vals = row["values"]
            # Company (col 1) — hyperlink to the rich artifact, but ONLY when that artifact
            # actually exists on disk. The workbook lives in outputs/_master/; the artifacts are
            # its siblings under outputs/<Name>/<slug>_soft_cov.html — so link up-and-over, and
            # URL-encode because folder names contain spaces and '&' (e.g. "Grupo Mexico",
            # "PE&OLES"). Companies without a rendered page stay plain text (never a dead link).
            cc = ws.cell(r, 1, row["name"])
            cc.alignment = ALIGN_LEFT
            artifact = ROOT / "outputs" / row["name"] / f"{row['slug']}_soft_cov.html"
            if artifact.exists():
                cc.font = FONT_LINK
                cc.hyperlink = quote(f"../{row['name']}/{row['slug']}_soft_cov.html")
            else:
                cc.font = FONT_LABEL
            ws.cell(r, 2, _pretty_sector(sector)).font = FONT_VALUE
            ws.cell(r, 2).alignment = ALIGN_LEFT
            ws.cell(r, 3, row["template"]).font = FONT_VALUE
            ws.cell(r, 3).alignment = ALIGN_LEFT
            ws.cell(r, 4, row["clave"]).font = FONT_VALUE
            ws.cell(r, 4).alignment = ALIGN_LEFT
            any_metric = False
            for k, (_band, blk, lbl, _hdr, unit, _good) in enumerate(COLUMNS):
                v = vals.get((blk, lbl))
                cell = ws.cell(r, N_FIXED + 1 + k)
                state = cell_state(row, (blk, lbl))
                if state == "filled":
                    cell.value = float(v)
                    cell.number_format = NUMFMT.get(unit, NUMFMT["ratio"])
                    cell.font = FONT_VALUE
                    cell.alignment = ALIGN_RIGHT
                    any_metric = True
                elif state == "na":
                    # honest N/A — a string so the numeric colour-scale ignores it.
                    cell.value = "N/A"
                    cell.font = FONT_NA
                    cell.fill = FILL_NA
                    cell.alignment = ALIGN_CENTER
                    reason = rationale(lbl, sector=row["sector"], slug=row["slug"])
                    if reason:
                        cell.comment = Comment(reason, "soft-coverage")
                else:  # gap — applicable but unfilled; make it visually loud.
                    cell.fill = FILL_GAP
            if any_metric:
                n_with_data += 1
            r += 1
    last_data_row = r - 1

    # ---- Peer-benchmark colour scales, one per numeric metric column ------------------------
    if first_data_row is not None and last_data_row >= first_data_row:
        for k, (_band, _blk, _lbl, _hdr, _unit, good) in enumerate(COLUMNS):
            letter = get_column_letter(N_FIXED + 1 + k)
            rng = f"{letter}{first_data_row}:{letter}{last_data_row}"
            ws.conditional_formatting.add(rng, _color_scale(good))

    # ---- Column widths ----------------------------------------------------------------------
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 10
    for k in range(n_metrics):
        ws.column_dimensions[get_column_letter(N_FIXED + 1 + k)].width = 12

    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(xlsx_path)
    data_rows = 0 if first_data_row is None else (last_data_row - first_data_row + 1)
    return data_rows, n_with_data


def write_csv_mirror(rows: list[dict], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    header = FIXED_HEADERS + [c[3] for c in COLUMNS]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        # same sector grouping/sort as the workbook
        seen, by_sector = [], {}
        for row in rows:
            by_sector.setdefault(row["sector"], []).append(row)
            if row["sector"] not in seen:
                seen.append(row["sector"])
        for sector in seen:
            for row in sorted(by_sector[sector], key=lambda x: x["name"].lower()):
                vals = row["values"]
                out = [row["name"], _pretty_sector(sector), row["template"], row["clave"]]
                for (_band, blk, lbl, _hdr, _unit, _good) in COLUMNS:
                    state = cell_state(row, (blk, lbl))
                    # N/A → literal "N/A"; GAP → empty; filled → the value. Lets certify tell a
                    # structural blank apart from a real gap without recomputing applicability.
                    out.append("N/A" if state == "na" else ("" if state == "gap" else vals[(blk, lbl)]))
                w.writerow(out)


# --------------------------------------------------------------------------------------------------
# HTML master page — self-generating, self-contained. Replaces the previously hand-authored artifact
# so every build/refresh reproduces an up-to-date page. Emits body-content only (a <title>, an inline
# <style>, and the .wrap div) — no <doctype>/<html>/<head>/<body> wrappers — so it can be published
# directly as a claude.ai Artifact (which wraps it) and still renders when opened as a local file.
# Theme-aware (light/dark) CSS is carried verbatim from the proven artifact. Deliberately omits the
# masthead (eyebrow/title/explanation/stat-tiles) and the "This session" card.
# --------------------------------------------------------------------------------------------------
MASTER_HTML_STYLE = """
:root{
  --bg:#f7f9fc; --panel:#ffffff; --ink:#1f2a44; --muted:#5b6577; --line:#dbe2ec;
  --rail:#334155; --railink:#eef2f7; --sector:#eef2f8; --accent:#1f6fd6; --shadow:0 1px 2px rgba(20,32,60,.06);
  --blank:#f2f5f9;
}
@media (prefers-color-scheme:dark){
  :root{ --bg:#0f1420; --panel:#151b28; --ink:#e6ebf3; --muted:#93a0b5; --line:#26314a;
    --rail:#1b2436; --railink:#e6ebf3; --sector:#1a2233; --accent:#5aa2f0; --shadow:0 1px 2px rgba(0,0,0,.4);
    --blank:#131a28; }
}
:root[data-theme="light"]{ --bg:#f7f9fc; --panel:#ffffff; --ink:#1f2a44; --muted:#5b6577; --line:#dbe2ec;
  --rail:#334155; --railink:#eef2f7; --sector:#eef2f8; --accent:#1f6fd6; --shadow:0 1px 2px rgba(20,32,60,.06); --blank:#f2f5f9; }
:root[data-theme="dark"]{ --bg:#0f1420; --panel:#151b28; --ink:#e6ebf3; --muted:#93a0b5; --line:#26314a;
  --rail:#1b2436; --railink:#e6ebf3; --sector:#1a2233; --accent:#5aa2f0; --shadow:0 1px 2px rgba(0,0,0,.4); --blank:#131a28; }

*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;line-height:1.45;}
.wrap{max-width:1500px;margin:0 auto;padding:28px clamp(14px,3vw,36px) 64px;}

.legend{display:flex;flex-wrap:wrap;align-items:center;gap:8px 20px;margin:0 2px 14px;font-size:12px;color:var(--muted);}
.legend .grad{display:inline-flex;align-items:center;gap:8px;}
.grad .bar{width:150px;height:11px;border-radius:6px;
  background:linear-gradient(90deg,#f8696b,#ffeb84,#63be7b);border:1px solid var(--line);}
.legend .key{display:inline-flex;align-items:center;gap:6px;}
.legend .sw{width:12px;height:12px;border-radius:3px;border:1px solid var(--line);display:inline-block;}

.tablecard{background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:hidden;}
.scroll{overflow:auto;max-height:78vh;}
table{border-collapse:separate;border-spacing:0;font-size:12px;width:max-content;min-width:100%;}
th,td{white-space:nowrap;}
thead th{position:sticky;z-index:3;top:0;}
th.band{background:var(--acc,#475467);color:#fff;font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;
  font-weight:700;padding:7px 10px;text-align:center;border-right:1px solid rgba(255,255,255,.22);top:0;}
th.sub{top:29px;background:var(--rail);color:var(--railink);font-weight:600;font-size:10.5px;letter-spacing:.04em;
  padding:6px 9px;text-align:right;border-bottom:1px solid var(--line);}
th.rail.band{--acc:#344054;text-align:left;left:0;z-index:5;}
th.rail.sub{text-align:left;left:0;z-index:4;}
th.rail.sub.sector-h{left:150px;}
.rail{position:sticky;left:0;background:var(--rail);color:var(--railink);}

td.v{text-align:right;padding:5px 9px;color:#15203a;font-weight:600;border-bottom:1px solid var(--line);
  border-right:1px solid color-mix(in srgb,var(--line) 55%,transparent);min-width:62px;}
td.v.blank{background:var(--blank);color:transparent;}
td.v.na{background:var(--blank);color:var(--muted);font-weight:500;text-align:center;
  background-image:repeating-linear-gradient(45deg,transparent,transparent 5px,color-mix(in srgb,var(--line) 60%,transparent) 5px,color-mix(in srgb,var(--line) 60%,transparent) 6px);}
td.v.gap{background:color-mix(in srgb,#f8696b 16%,var(--panel));
  outline:1px solid color-mix(in srgb,#f8696b 50%,transparent);outline-offset:-1px;}
th.coname{position:sticky;left:0;z-index:2;background:var(--panel);color:var(--ink);text-align:left;font-weight:650;
  padding:5px 12px 5px 12px;border-bottom:1px solid var(--line);border-right:2px solid var(--line);min-width:150px;max-width:150px;}
td.sect{position:sticky;left:150px;z-index:1;background:var(--panel);color:var(--muted);font-size:11px;text-align:left;
  padding:5px 12px;border-bottom:1px solid var(--line);border-right:2px solid var(--line);min-width:132px;}
tr.co:hover th.coname,tr.co:hover td.sect{background:color-mix(in srgb,var(--accent) 12%,var(--panel));}
tr.co:hover td.v{outline:1px solid color-mix(in srgb,var(--accent) 45%,transparent);outline-offset:-1px;}
tr.secrow td.sec{position:sticky;left:0;z-index:1;background:var(--sector);color:var(--ink);font-weight:750;font-size:11px;
  letter-spacing:.06em;text-transform:uppercase;padding:6px 12px;border-bottom:1px solid var(--line);border-top:1px solid var(--line);}
.sec .seccount{color:var(--muted);font-weight:600;margin-left:8px;}
/* Per-industry metric strip — each sector band labels its applicable metrics; OFF/MUTED de-emphasised. */
tr.secrow td.sechdr{background:var(--sector);color:var(--muted);font-size:9.5px;font-weight:600;text-align:right;
  padding:6px 9px;border-bottom:1px solid var(--line);border-top:1px solid var(--line);letter-spacing:.02em;}
tr.secrow td.sechdr.show{color:var(--ink);font-weight:750;}
tr.secrow td.sechdr.muted{color:var(--muted);font-style:italic;opacity:.72;}
tr.secrow td.sechdr.off{color:var(--muted);opacity:.32;text-decoration:line-through;}
/* Scroll-following sticky metric header: columns outside the in-view industry's profile dim. */
th.sub.dim{opacity:.34;font-weight:500;}

/* Filter panel — client-side, in-memory. Threshold any metric (stackable chips) + industry/sector. */
.filters{display:flex;flex-wrap:wrap;align-items:flex-end;gap:10px 16px;margin:0 2px 14px;padding:12px 14px;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);font-size:12px;}
.filters .fgroup{display:flex;flex-direction:column;gap:4px;}
.filters .fmetric{display:flex;flex-direction:row;align-items:flex-end;gap:6px;}
.filters label{font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:700;}
.filters select,.filters input{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);
  border-radius:6px;padding:5px 7px;}
.filters input[type=number]{width:72px;}
.filters button{font:inherit;font-weight:650;color:var(--railink);background:var(--accent);border:0;border-radius:6px;
  padding:6px 12px;cursor:pointer;}
.filters button#f-reset{background:transparent;color:var(--muted);border:1px solid var(--line);}
.fcount{margin-left:auto;color:var(--muted);font-weight:650;align-self:center;white-space:nowrap;}
.chips{display:flex;flex-wrap:wrap;gap:6px;width:100%;}
.chip{display:inline-flex;align-items:center;gap:2px;background:color-mix(in srgb,var(--accent) 14%,var(--panel));
  border:1px solid color-mix(in srgb,var(--accent) 40%,transparent);color:var(--ink);border-radius:14px;
  padding:3px 4px 3px 11px;font-size:11px;font-weight:600;}
.chip button{background:transparent;color:var(--muted);border:0;padding:0 5px;font-size:13px;cursor:pointer;line-height:1;}
tr.co.hide,tr.secrow.hide{display:none;}

footer{margin-top:18px;color:var(--muted);font-size:11.5px;line-height:1.6;}
footer code{background:color-mix(in srgb,var(--ink) 8%,transparent);padding:1px 5px;border-radius:4px;font-size:11px;}
"""

# Scroll-following sticky header: as each sector band scrolls under the sticky metric header, dim the
# columns outside that industry's profile (data-dim = pipe-joined OFF/MUTED headers on the band row).
# Pure DOM, no external requests → Artifact/CSP-safe. Degrades gracefully (static header) with JS off.
MASTER_MOVING_HEADER_JS = """
(function(){
  var scroll=document.querySelector('.scroll'); if(!scroll) return;
  var thead=scroll.querySelector('thead');
  var subs={}; scroll.querySelectorAll('thead th.sub[data-col]').forEach(function(th){subs[th.getAttribute('data-col')]=th;});
  var secrows=Array.prototype.slice.call(scroll.querySelectorAll('tr.secrow')); if(!secrows.length) return;
  var last=null;
  function currentDim(){
    var srect=scroll.getBoundingClientRect(), headH=thead?thead.getBoundingClientRect().height:0, cur=null;
    for(var i=0;i<secrows.length;i++){
      var s=secrows[i]; if(s.offsetParent===null) continue;
      var r=s.getBoundingClientRect();
      if(r.top-srect.top<=headH+4){cur=s;} else break;
    }
    return cur?(cur.getAttribute('data-dim')||''):'';
  }
  function apply(){
    var dim=currentDim(); if(dim===last) return; last=dim;
    var off={}; dim.split('|').forEach(function(c){if(c)off[c]=1;});
    for(var c in subs){subs[c].classList.toggle('dim',!!off[c]);}
  }
  var ticking=false;
  scroll.addEventListener('scroll',function(){if(!ticking){ticking=true;requestAnimationFrame(function(){apply();ticking=false;});}});
  window.addEventListener('resize',apply); apply();
})();
"""

# Human labels for the Industry filter dropdown (template values are the raw data-template attrs).
_TEMPLATE_LABEL = {"industrial": "Industrial", "financials": "Financials", "reit": "REIT"}

# Client-side filter: industry (template) + sector selects, and stackable per-metric min/max
# thresholds (chips, AND-combined). Hides non-matching companies and any sector band left empty,
# updates a live count, and re-fires the moving-header. Pure DOM, no external requests (CSP-safe).
MASTER_FILTER_JS = """
(function(){
  var tbody=document.querySelector('table tbody'); if(!tbody) return;
  var rows=Array.prototype.slice.call(tbody.querySelectorAll('tr.co')); var total=rows.length;
  var fT=document.getElementById('f-template'), fS=document.getElementById('f-sector'),
      fM=document.getElementById('f-metric'), fMin=document.getElementById('f-min'),
      fMax=document.getElementById('f-max'), add=document.getElementById('f-add'),
      reset=document.getElementById('f-reset'), chipsEl=document.getElementById('f-chips'),
      countEl=document.getElementById('f-count');
  if(!fT||!add) return;
  var thresholds=[];
  function chipText(t){
    var lo=(t.min!==null?'≥ '+t.min:''), hi=(t.max!==null?'≤ '+t.max:'');
    return t.col+' '+lo+((t.min!==null&&t.max!==null)?' · ':'')+hi;
  }
  function renderChips(){
    chipsEl.textContent='';
    thresholds.forEach(function(t,i){
      var c=document.createElement('span'); c.className='chip';
      c.appendChild(document.createTextNode(chipText(t)));
      var x=document.createElement('button'); x.type='button'; x.setAttribute('aria-label','remove'); x.textContent='✕';
      x.onclick=function(){thresholds.splice(i,1);renderChips();apply();};
      c.appendChild(x); chipsEl.appendChild(c);
    });
  }
  function rowVal(row,col){
    var td=row.querySelector('td.v[data-col="'+col.replace(/"/g,'\\\\"')+'"]');
    if(!td||!td.hasAttribute('data-val')) return null;
    var v=parseFloat(td.getAttribute('data-val')); return isNaN(v)?null:v;
  }
  function match(row){
    if(fT.value!=='all'&&row.getAttribute('data-template')!==fT.value) return false;
    if(fS.value!=='all'&&row.getAttribute('data-sector')!==fS.value) return false;
    for(var i=0;i<thresholds.length;i++){
      var t=thresholds[i], v=rowVal(row,t.col);
      if(v===null) return false;                       // no comparable value → excluded
      if(t.min!==null&&v<t.min) return false;
      if(t.max!==null&&v>t.max) return false;
    }
    return true;
  }
  function apply(){
    var shown=0;
    rows.forEach(function(r){var ok=match(r); r.classList.toggle('hide',!ok); if(ok)shown++;});
    Array.prototype.slice.call(tbody.querySelectorAll('tr.secrow')).forEach(function(sec){
      var n=0, el=sec.nextElementSibling;
      while(el&&el.className.indexOf('secrow')<0){
        if(el.className.indexOf('co')>=0&&el.className.indexOf('hide')<0) n++;
        el=el.nextElementSibling;
      }
      sec.classList.toggle('hide',n===0);
      var badge=sec.querySelector('.seccount'); if(badge) badge.textContent=n;
    });
    countEl.textContent=shown+' / '+total+' companies';
    window.dispatchEvent(new Event('resize'));         // moving-header recomputes over visible bands
  }
  add.onclick=function(){
    var mn=fMin.value.trim(), mx=fMax.value.trim(); if(mn===''&&mx==='') return;
    thresholds.push({col:fM.value, min:mn===''?null:parseFloat(mn), max:mx===''?null:parseFloat(mx)});
    fMin.value=''; fMax.value=''; renderChips(); apply();
  };
  reset.onclick=function(){fT.value='all'; fS.value='all'; thresholds=[]; renderChips(); apply();};
  fT.onchange=apply; fS.onchange=apply;
  apply();
})();
"""

# Band accent (one per group), matching FILL_BAND minus the alpha byte — reads at a glance in the head.
BAND_ACCENT = {
    "Valuation": "#1F6FD6", "Profitability": "#0B8F5E",
    "Growth": "#8A5AD6", "Leverage & Liquidity": "#B4690E",
    "Capital (banks)": "#0E7C86", "Capital (financials)": "#0E7C86",
    "Risk (banks)": "#C2410C", "Property (REITs)": "#7C3AED",
}

# Heatmap endpoints — Excel's classic red-yellow-green, matching NUMFMT/_color_scale in the workbook.
_HEAT_RED, _HEAT_YEL, _HEAT_GRN = (248, 105, 107), (255, 235, 132), (99, 190, 123)


def _lerp(c1: tuple, c2: tuple, t: float) -> tuple:
    t = max(0.0, min(1.0, t))
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def _heat_css(value: float, lo: float, med: float, hi: float, good: str) -> str:
    """Per-column red→yellow→green, mirroring the xlsx ColorScaleRule (min / 50th-pctile / max).

    ``good='high'`` → high=green; ``good='low'`` → low=green. Median anchors to yellow."""
    start, end = (_HEAT_RED, _HEAT_GRN) if good == "high" else (_HEAT_GRN, _HEAT_RED)
    if hi is None or hi == lo:
        r, g, b = _HEAT_YEL
    elif value <= med:
        t = 0.0 if med == lo else (value - lo) / (med - lo)
        r, g, b = _lerp(start, _HEAT_YEL, t)
    else:
        t = 1.0 if hi == med else (value - med) / (hi - med)
        r, g, b = _lerp(_HEAT_YEL, end, t)
    return f"rgb({r}, {g}, {b})"


def _fmt_val(value: float, unit: str) -> str:
    if unit == "pct":
        return f"{value:.1f}%"
    if unit == "x":
        return f"{value:.2f}×"
    return f"{value:,.2f}"


def _band_groups() -> list[tuple[str, int]]:
    """[(band, span)] in COLUMNS order — the merged band-header colspans."""
    groups: list[tuple[str, int]] = []
    for band, *_ in COLUMNS:
        if groups and groups[-1][0] == band:
            groups[-1] = (band, groups[-1][1] + 1)
        else:
            groups.append((band, 1))
    return groups


_SECTION_CSS = """
.tsection{margin:26px 0 6px}
.thead-row{display:flex;align-items:baseline;gap:12px;margin:20px 2px 10px;flex-wrap:wrap}
.thead-row h2{font-size:18px;margin:0;color:var(--ink);letter-spacing:-.01em}
.tmeta{font-size:12px;color:var(--muted)}
.tsection[hidden]{display:none}
tr.co[hidden],tr.secrow[hidden]{display:none}
"""

_SECTION_FILTER_JS = """
(function(){
  var ft=document.getElementById('f-template'), fs=document.getElementById('f-sector'),
      fr=document.getElementById('f-reset'), fc=document.getElementById('f-count');
  var secs=[].slice.call(document.querySelectorAll('.tsection'));
  function apply(){
    var t=ft.value, s=fs.value, shown=0;
    secs.forEach(function(sec){
      var vis=(t==='all'||sec.getAttribute('data-template')===t);
      sec.hidden=!vis;
      if(!vis) return;
      var sectorCounts={};
      sec.querySelectorAll('tr.co').forEach(function(r){
        var rv=(s==='all'||r.getAttribute('data-sector')===s);
        r.hidden=!rv;
        if(rv){shown++; var sk=r.getAttribute('data-sector'); sectorCounts[sk]=(sectorCounts[sk]||0)+1;}
      });
      sec.querySelectorAll('tr.secrow').forEach(function(r){
        r.hidden=!sectorCounts[r.getAttribute('data-sector')];
      });
    });
    fc.textContent=shown+' companies';
  }
  ft.addEventListener('change',apply); fs.addEventListener('change',apply);
  fr.addEventListener('click',function(){ft.value='all';fs.value='all';apply();});
  apply();
})();
"""


def _section_keep_columns(trows: list[dict]) -> list[tuple]:
    """The columns this per-industry section renders. A column is COLLAPSED (dropped entirely, not
    shown blank) when it is either (a) ``na_template`` for every company in the template — e.g.
    EV/EBITDA for all banks, CET1 for every industrial — or (b) applicable but with NO value anywhere
    in the section (e.g. NOI margin 0/31). So each section shows only its own metrics that carry at
    least one real number; a sparse-but-present sector metric (NIM 1/14, occupancy 5/31) still shows."""
    keep = []
    for c in COLUMNS:
        ck = (c[1], c[2])
        applicable = any(r["applic"].get(ck) != "na_template" for r in trows)
        if not applicable:
            continue
        if any(cell_state(r, ck) == "filled" for r in trows):
            keep.append(c)
    return keep


def _render_template_section(tkey: str, tlabel: str, trows: list[dict]) -> tuple[str, int, int]:
    """One per-industry table: its own collapsed column set + a per-column heatmap over THIS template's
    rows only. Returns (section_html, n_filled, n_applicable)."""
    keep = _section_keep_columns(trows)

    # per-column heatmap basis, over this template's filled cells only (na/gap excluded)
    colstats: list[tuple] = []
    for (_b, blk, lbl, _h, _u, _g) in keep:
        vals = [r["values"].get((blk, lbl)) for r in trows if cell_state(r, (blk, lbl)) == "filled"]
        vals = [float(v) for v in vals if v is not None]
        if lbl == "P/E (LTM)":
            vals = [v for v in vals if 0 < v <= 100]   # scale on normal P/Es only
        colstats.append((min(vals), statistics.median(vals), max(vals)) if vals else (None, None, None))

    # band header (run-length over the kept columns) + sub header
    band_groups: list[list] = []
    for c in keep:
        if band_groups and band_groups[-1][0] == c[0]:
            band_groups[-1][1] += 1
        else:
            band_groups.append([c[0], 1])
    head_band = ['<th class="rail band" colspan="2">Company</th>']
    for band, span in band_groups:
        head_band.append(f'<th class="band" colspan="{span}" style="--acc:{BAND_ACCENT.get(band, "#475467")}">'
                         f'{_html.escape(band)}</th>')
    head_sub = ['<th class="rail sub">Company</th><th class="rail sub sector-h">Sector</th>']
    for (_b, _blk, _lbl, header, _u, _g) in keep:
        head_sub.append(f'<th class="sub" data-col="{_html.escape(header)}">{_html.escape(header)}</th>')

    # body, banded by sector
    seen: list[str] = []
    by_sector: dict[str, list[dict]] = {}
    for row in trows:
        by_sector.setdefault(row["sector"], []).append(row)
        if row["sector"] not in seen:
            seen.append(row["sector"])
    span_cols = 2 + len(keep)
    n_filled = n_applicable = 0
    body: list[str] = []
    for sector in seen:
        members = sorted(by_sector[sector], key=lambda x: x["name"].lower())
        body.append(f'<tr class="secrow" data-sector="{_html.escape(sector)}">'
                    f'<td class="sec" colspan="{span_cols}">{_html.escape(_pretty_sector(sector))}'
                    f'<span class="seccount">{len(members)}</span></td></tr>')
        for row in members:
            cells = [f'<th class="rail coname">{_html.escape(row["name"])}</th>'
                     f'<td class="rail sect">{_html.escape(_pretty_sector(sector))}</td>']
            for k, (_band, blk, lbl, hdr, unit, good) in enumerate(keep):
                dc = _html.escape(hdr)
                ck = (blk, lbl)
                state = cell_state(row, ck)
                if state != "na":
                    n_applicable += 1
                if state == "na":
                    reason = rationale(lbl, sector=sector, slug=row["slug"])
                    na_attr = f' title="{_html.escape(reason)}"' if reason else ""
                    cells.append(f'<td class="v na"{na_attr} data-col="{dc}">—</td>')
                    continue
                if state == "gap":
                    cells.append(f'<td class="v gap" data-col="{dc}"></td>')
                    continue
                v = float(row["values"][ck])
                n_filled += 1
                lo, med, hi = colstats[k]
                dv = f' data-val="{v:.6g}"'
                if lbl == "P/E (LTM)" and v <= 0:
                    cells.append(f'<td class="v" data-col="{dc}"{dv} style="background:{_NEUTRAL_BG};" '
                                 f'title="negative P/E — loss-maker (not a meaningful multiple)">'
                                 f'{_fmt_val(v, unit)}</td>')
                else:
                    cells.append(f'<td class="v" data-col="{dc}"{dv} '
                                 f'style="background:{_heat_css(v, lo, med, hi, good)};">'
                                 f'{_fmt_val(v, unit)}</td>')
            body.append(f'<tr class="co" data-sector="{_html.escape(sector)}" '
                        f'data-name="{_html.escape(row["name"])}">{"".join(cells)}</tr>')

    fill_pct = (100.0 * n_filled / n_applicable) if n_applicable else 0.0
    heading = (f'<div class="thead-row"><h2>{_html.escape(tlabel)}</h2>'
               f'<span class="tmeta">{len(trows)} companies · {len(keep)} metrics · '
               f'{n_filled:,}/{n_applicable:,} cells filled ({fill_pct:.0f}%)</span></div>')
    table = ('<div class="tablecard"><div class="scroll"><table>\n'
             f'    <thead><tr>{"".join(head_band)}</tr><tr>{"".join(head_sub)}</tr></thead>\n'
             f'    <tbody>{"".join(body)}</tbody></table></div></div>')
    section = f'<section class="tsection" data-template="{_html.escape(tkey)}">{heading}{table}</section>'
    return section, n_filled, n_applicable


def write_html_master(rows: list[dict], html_path: Path, *,
                      title: str = "Soft Coverage — Master Matrix", extra_footer: str = "") -> None:
    """Emit the self-contained master matrix HTML (body-content only) as THREE per-industry sections —
    Industrial, Banks & Financials, REITs — each with only its applicable columns (sector-specific
    metrics included; non-applicable columns collapsed, never shown blank). Blank cells stay honestly
    blank; nothing is fabricated."""
    order = [("industrial", "Industrial"), ("financials", "Banks & Financials"), ("reit", "REITs · FIBRAs")]
    sections: list[str] = []
    present: list[tuple[str, str]] = []
    summ: list[str] = []
    tot_f = tot_a = 0
    for tkey, tlabel in order:
        trows = [r for r in rows if r["template"] == tkey]
        if not trows:
            continue
        html_sec, nf, na = _render_template_section(tkey, tlabel, trows)
        sections.append(html_sec)
        present.append((tkey, tlabel))
        summ.append(f"{tlabel} {nf:,}/{na:,}")
        tot_f += nf
        tot_a += na
    cov = (100.0 * tot_f / tot_a) if tot_a else 0.0

    seen_sectors: list[str] = []
    for r in rows:
        if r["sector"] not in seen_sectors:
            seen_sectors.append(r["sector"])
    tmpl_opts = '<option value="all">All industries</option>' + "".join(
        f'<option value="{t}">{_html.escape(lbl)}</option>' for t, lbl in present)
    sec_opts = '<option value="all">All sectors</option>' + "".join(
        f'<option value="{_html.escape(s)}">{_html.escape(_pretty_sector(s))}</option>' for s in seen_sectors)
    filter_html = (
        '<div class="filters">'
        f'<div class="fgroup"><label>Industry</label><select id="f-template">{tmpl_opts}</select></div>'
        f'<div class="fgroup"><label>Sector</label><select id="f-sector">{sec_opts}</select></div>'
        '<button id="f-reset" type="button">Reset</button>'
        '<span id="f-count" class="fcount"></span>'
        '</div>'
    )

    footer = (
        "<b>Per-industry comparable sets.</b> Each section shows only the metrics that apply to that "
        "industry — a bank's CET1 / NIM / efficiency / cost-of-risk, a REIT's P/FFO / occupancy / NOI — "
        "and COLLAPSES the columns that don't apply (a bank has no EV/EBITDA or FCF; a REIT's "
        "earnings-based P/E is fair-value-distorted). Heatmap is a per-column red→green scale within "
        "each section (green = better; direction per metric — low P/E good, high ROE good). An empty "
        "cell is an honest gap (applicable, no free value yet); '—' is not-applicable to that specific "
        "company. Sector-specific ratios (NIM, efficiency, cost-of-risk, occupancy, NOI) are sparse "
        "today — the free-source ceiling — and shown wherever a value exists. "
        f"Source: <code>outputs/_master/soft_coverage_master.xlsx</code> · {len(rows)} companies · "
        f"cells filled {tot_f:,}/{tot_a:,} ({cov:.0f}%) · " + " · ".join(summ) + "."
        + (f" {extra_footer}" if extra_footer else "")
    )

    doc = (
        f'<title>{_html.escape(title)}</title>\n'
        f'<style>{MASTER_HTML_STYLE}\n{_SECTION_CSS}</style>\n'
        '<div class="wrap">\n'
        f'  {filter_html}\n'
        '  <div class="legend">'
        '<span class="grad"><span>Worse</span><span class="bar"></span><span>Better</span>'
        '<span style="color:var(--ink)">— per-column within each industry</span></span>'
        '<span class="key"><span class="sw" style="background-image:repeating-linear-gradient('
        '45deg,transparent,transparent 3px,var(--line) 3px,var(--line) 4px);background-color:var(--blank)"></span>'
        '— = not applicable to this company</span>'
        '<span class="key"><span class="sw" style="background:color-mix(in srgb,#f8696b 20%,var(--panel));'
        'border-color:#f8696b"></span>blank = applicable, no free value yet</span>'
        '</div>\n'
        + "\n".join(sections) + "\n"
        f'  <footer>{footer}</footer>\n'
        '</div>\n'
        f'<script>{_SECTION_FILTER_JS}</script>\n'
    )
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(doc, encoding="utf-8")


# --------------------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------------------
def _yahoo_fallback_vmap(clave: str, template: str) -> dict:
    """{block: {label: value}} of the market-derived metrics Yahoo computes, for a NO-CORPUS company
    (no BMV XBRL, so no model). Fills P/E, EV/EBITDA, P/BV, ROE, Net margin, Dividend yield. Empty if
    Yahoo has no data for the symbol. Tagged Yahoo (not filing) — honest provenance, never fabricated."""
    from src.download import market_data
    try:
        ks = market_data.fetch_key_stats(clave, verify_ssl=False) or {}
    except Exception:
        ks = {}
    try:
        px = market_data.fetch_prices(clave, [], verify_ssl=False)
    except Exception:
        px = {}
    out: dict = {}

    def put(blk, lbl, v):
        if v is not None:
            out.setdefault(blk, {})[lbl] = v

    put("snapshot_multiples", "P/E (LTM)", ks.get("trailing_pe"))
    put("snapshot_multiples", "EV/EBITDA", ks.get("ev_ebitda"))
    put("financial_analysis", "P/BV", ks.get("price_to_book"))
    put("financial_analysis", "ROE", ks.get("roe"))
    put("financial_analysis", "Net margin", ks.get("profit_margin"))
    dtm, last = px.get("dvd_ttm"), px.get("last")
    dy = (dtm / last * 100.0) if (dtm and last) else ks.get("dividend_yield")
    if dy is None and last:            # a resolved price with no dividend events → honest 0%
        dy = 0.0
    if template == "reit":
        put("reit_snapshot", "Distribution yield", dy)
    else:
        put("snapshot_multiples", "Dividend yield", dy)

    # Yahoo's trailing P/E can disagree with its OWN P/BV × ROE (different EPS basis / one-off period) —
    # e.g. Grupo Bafar P/E 28.1 vs P/BV 3.43 ÷ ROE 31.6% ⇒ 10.9. For a no-corpus row every cell is
    # Yahoo's best-effort, so the non-reconciling P/E is the unreliable one → blank it (blank-not-wrong).
    pe = out.get("snapshot_multiples", {}).get("P/E (LTM)")
    pbv = out.get("financial_analysis", {}).get("P/BV")
    roe = out.get("financial_analysis", {}).get("ROE")
    if pe and pbv and roe and roe > 0 and abs(pe - pbv / (roe / 100.0)) / pe > 0.5:
        out["snapshot_multiples"].pop("P/E (LTM)", None)
    return out


# Companies with NO free price source (verified: no Yahoo listing under any candidate/override symbol) —
# their price-derived cells can never fill from free data. The optional `--core` view drops these so the
# headline coverage reflects the fully-free-serviceable universe; the full 136-name matrix is unchanged
# and the Bloomberg matrix fills these names.
_CORE_EXCLUDE = {
    "esentia", "kamosa", "sigma", "soma", "xfra", "telesites",
    "fibra_exi", "fibra_hipotecaria", "fibra_ideal", "fibra_mx", "fibra_next", "fibra_orion",
    "fibra_sites",
    # Delisted / taken-private / Bloomberg-only — verified NO free Yahoo EQUITY listing under any
    # candidate or dashed-series variant (scripts/probe_symbols.py; the OHLMEX/SANMEXB hits are
    # MUTUALFUND look-alikes the EQUITY guard rejects). Their price cells can never fill from free data.
    "aleatica", "banco_santander_mexico", "elementia_materiales", "fortaleza_materiales",
    "gmexico_transportes", "grupo_lala", "grupo_sanborns", "javer",
}


def build_master(only: set[str] | None, *, no_network: bool, skip_existing: bool,
                 render: bool, limit: int | None, fetch_history: bool = False,
                 core: bool = False) -> None:
    # Same fast/offline posture as build_all's cached-funds path: read cached XBRL filings (skip the
    # slow ~85s archive fetch) while still allowing live prices unless --no-network. facts_only skips
    # the heavy MD&A parse — the headline metrics are all core XBRL/valuation, not prose segments.
    # --fetch-history flips to the LIVE archive path so download_ticker pulls deeper history
    # (≥40 filings) into data/reports/<slug>/xbrl/ — needed to populate the Growth block.
    offline_fundamentals = not fetch_history
    subject_facts_only = True

    companies = [c for c in roster() if (ROOT / "inputs" / f"{c[0]}.md").exists()]
    if only:
        companies = [c for c in companies if c[0] in only]
    if limit:
        companies = companies[:limit]

    print(f"[master] {len(companies)} companies "
          f"({'offline' if no_network else 'cached-funds + live prices'}, "
          f"render={'on' if render else 'off'})")

    rows: list[dict] = []
    n_emitted = 0
    n_blanked = 0
    for slug, name, clave, template, sector in companies:
        csv_path = ROOT / "outputs" / name / "csv" / f"{slug}_coverage.csv"
        vmap: dict = {}
        emitted = False
        if skip_existing and csv_path.exists():
            vmap = csv_to_map(csv_path)
            emitted = True
            print(f"  reuse {slug:24s} (cached csv)")
        else:
            try:
                res: BuildResult = build_one(
                    ROOT / "inputs" / f"{slug}.md", no_network=no_network,
                    offline_fundamentals=offline_fundamentals,
                    subject_facts_only=subject_facts_only)
            except Exception as e:  # never let one company kill the batch
                res = BuildResult(slug, name, template, emitted=False, audit_passed=False,
                                  n_errors=0, n_advisories=0, n_blank=0, has_bbg_pack=False,
                                  n_periods=0, error=f"{type(e).__name__}: {e}")
            emitted = res.emitted
            # Skip a gate-FAILED model — never admit unaudited numbers into the matrix. The company
            # contributes an honest all-blank row instead.
            vmap = model_to_map(res.model) if res.emitted else {}
            mark = "ok " if res.emitted else ("err" if res.error else "blk")
            print(f"  {mark}   {slug:24s} {res.error[:60]}")

        # No-corpus fallback: a company absent from the BMV XBRL archive yields no model. Fill its
        # market-derived cells straight from Yahoo's computed stats so the row isn't blank (network only).
        if not vmap and not no_network:
            yv = _yahoo_fallback_vmap(clave, template)
            if yv:
                vmap = yv
                print(f"  yh    {slug:24s} (yahoo key-stats fallback)")

        # network-free structural guardrail: blank wrong-but-in-band multiples for a company whose
        # shares/market-cap/net-income basis fails a must-hold relationship (reuses the reconciler).
        poisoned = _guardrail_blank(name, slug, template) if emitted else set()
        values = {}
        for (_b, blk, lbl, _h, unit, _g) in COLUMNS:
            raw = pick_col(vmap, blk, lbl)
            adm = _admit_cell(lbl, unit, raw, template)
            if adm is not None and lbl in poisoned:
                adm = None                       # guardrail: poisoned by a structural scale/shares defect
            if raw is not None and adm is None:
                n_blanked += 1
            values[(blk, lbl)] = adm
        # Per-cell applicability (applicable | na_template | na_source) — the free-source N/A map.
        # An applicable-but-blank cell is a GAP; N/A cells are excluded from the coverage denominator.
        applic = row_applicability(template, sector, clave, slug)
        rows.append({"slug": slug, "name": name, "clave": clave, "template": template,
                     "sector": sector, "values": values, "applic": applic, "has_html": False})

        if render and emitted:
            out = render_artifact(name, slug, sector)
            if out is not None:
                n_emitted += 1

    out_dir = ROOT / "outputs" / "_master"

    # CORE is THE matrix (per the user): when --core, the core set (drops the verified no-free-price
    # names) is written to the PRIMARY soft_coverage_master.{xlsx,csv,html} — the file certify /
    # refresh_daily read — and the full 136-universe build is demoted to *_full.{csv,html}. Without
    # --core the full universe stays primary (backward-compatible).
    if core and not only:
        core_rows = [r for r in rows if r["slug"] not in _CORE_EXCLUDE]
        excluded = sorted(r["name"] for r in rows if r["slug"] in _CORE_EXCLUDE)
        note = (f"<b>Core matrix</b> — the {len(core_rows)} names with a free price source; excludes "
                f"{len(excluded)} Bloomberg-only names (filled by the more-complete matrix): "
                f"{', '.join(excluded)}.")
        # full → demoted secondary
        write_csv_mirror(rows, out_dir / "soft_coverage_master_full.csv")
        write_html_master(rows, out_dir / "soft_coverage_master_full.html",
                          title="Soft Coverage — Full Universe (136)")
        # core → primary
        primary_rows, primary_note = core_rows, note
        print(f"[master] full universe → soft_coverage_master_full.{{csv,html}} ({len(rows)} names)")
    else:
        primary_rows, primary_note = rows, ""

    xlsx_path = out_dir / "soft_coverage_master.xlsx"
    csv_path = out_dir / "soft_coverage_master.csv"
    html_path = out_dir / "soft_coverage_master.html"
    data_rows, n_with_data = write_workbook(primary_rows, xlsx_path)
    write_csv_mirror(primary_rows, csv_path)
    write_html_master(primary_rows, html_path,
                      title="Soft Coverage — Core Matrix" if primary_note else "Soft Coverage — Master Matrix",
                      extra_footer=primary_note)

    n_filled, n_applicable, n_gap = coverage_counts(primary_rows)
    cov_pct = (100.0 * n_filled / n_applicable) if n_applicable else 0.0
    print(f"[master] {len(primary_rows)} companies (primary{'=core' if primary_note else ''}) · "
          f"{data_rows} rows · {n_with_data} with data · {n_blanked} cells blanked as implausible")
    print(f"[master] applicable-free {n_applicable} · filled {n_filled} ({cov_pct:.1f}%) · "
          f"GAP {n_gap} · (N/A excluded from denominator)")
    print(f"[master] wrote {csv_path}")
    print(f"[master] wrote {html_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=None, help="comma-separated slugs (default: whole universe)")
    ap.add_argument("--no-network", action="store_true",
                    help="fully offline: cached filings, no live prices/macro")
    ap.add_argument("--skip-existing", action="store_true",
                    help="reuse an existing coverage CSV instead of rebuilding the company")
    ap.add_argument("--render", action=argparse.BooleanOptionalAction, default=True,
                    help="also re-render each emitting company's HTML artifact (default ON)")
    ap.add_argument("--limit", type=int, default=None, help="cap the company count (for testing)")
    ap.add_argument("--fetch-history", action="store_true",
                    help="LIVE archive fetch: pull deeper XBRL history (≥40 filings/company) so the "
                         "Growth block populates; caches under data/reports/<slug>/xbrl/ (network)")
    ap.add_argument("--core", action="store_true",
                    help="also emit soft_coverage_master_core.{csv,html} excluding the verified "
                         "no-free-price names (cleaner headline; full matrix unchanged)")
    args = ap.parse_args()

    only = set(args.only.split(",")) if args.only else None
    build_master(only, no_network=args.no_network, skip_existing=args.skip_existing,
                 render=args.render, limit=args.limit, fetch_history=args.fetch_history,
                 core=args.core)


if __name__ == "__main__":
    main()

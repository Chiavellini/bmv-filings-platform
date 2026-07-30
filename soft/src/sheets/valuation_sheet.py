"""Render Sheet 1 (Fundamental Valuation Metrics) from a :class:`CoverageModel`.

Reuses the vendored style vocabulary from ``src.excel.segments_sheet`` (blue hard-input font,
grey-italic derived font, section font, source-tag fills). The snapshot block writes the
subject's headline multiples as **live Excel formulas** over labelled input cells, so editing a
price re-prices the sheet; peers and other blocks render computed values with a source tag.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

from src.coverage.peers import MULTIPLES, company_multiples
from src.excel.segments_sheet import (
    _BLUE,
    _GREY,
    FONT_LABEL,
    FONT_SECTION,
    FONT_SUB,
    FONT_TITLE,
    FONT_VALUE,
)

# Source-tag → font (hard inputs blue, calc grey-italic, macro/filing blue-ish).
_FONT_CALC = Font(name="Calibri", size=9, italic=True, color=_GREY)
_FONT_INPUT = FONT_VALUE
_ALIGN_R = Alignment(horizontal="right")
_ALIGN_L = Alignment(horizontal="left")

_NUMFMT = {
    "x": '#,##0.0"x"',
    "pct": '#,##0.0"%"',
    "currency": '#,##0',
    "price": '#,##0.00',
    "count": '#,##0',
    "ratio": '#,##0.00',
    "index": '#,##0.00',
}


def _fmt(unit: str) -> str:
    return _NUMFMT.get(unit, "#,##0.00")


def _put(ws, row, col, value, *, font=None, numfmt=None, align=None):
    c = ws.cell(row=row, column=col, value=value)
    if font is not None:
        c.font = font
    if numfmt is not None:
        c.number_format = numfmt
    if align is not None:
        c.alignment = align
    return c


def _render_snapshot(ws, block, spec, fund, pack, row: int) -> int:
    """Snapshot block with a subject input mini-table, live multiples, and peer columns."""
    from src.coverage.valuation import _subject_inputs, _peer_inputs  # local import (no cycle)

    subj = _subject_inputs(fund, pack)

    # --- subject input cells (referenced by the live formulas) ---
    inputs = [
        ("Price", subj.px_last, "price", "bbg"),
        ("Shares out (mn)", subj.shares_out, "count", "bbg"),
        ("Net debt", subj.net_debt, "currency", "bbg"),
        ("Minority interest", subj.minority_interest, "currency", "bbg"),
        ("EBITDA (LTM)", subj.ebitda, "currency", "filing"),
        ("Net income (LTM)", subj.net_income, "currency", "filing"),
        ("Revenue (LTM)", subj.sales, "currency", "filing"),
        ("Book value", subj.equity, "currency", "bbg"),
        ("FCF (LTM)", subj.fcf, "currency", "calc"),
        ("EPS (fwd)", subj.eps_ntm, "price", "bbg"),
    ]
    ref: dict[str, str] = {}  # label -> cell coord for formulas
    _put(ws, row, 1, "Inputs", font=FONT_SUB)
    row += 1
    for label, val, unit, src in inputs:
        _put(ws, row, 1, label, font=FONT_LABEL, align=_ALIGN_L)
        cell = _put(ws, row, 2, val, font=_FONT_INPUT, numfmt=_fmt(unit), align=_ALIGN_R)
        _put(ws, row, 7, f"[{src}]", font=_FONT_CALC, align=_ALIGN_L)
        ref[label] = cell.coordinate
        row += 1

    # market cap + EV as formulas
    _put(ws, row, 1, "Market cap", font=FONT_LABEL, align=_ALIGN_L)
    mc_cell = _put(ws, row, 2, f"={ref['Price']}*{ref['Shares out (mn)']}",
                   font=_FONT_CALC, numfmt=_fmt("currency"), align=_ALIGN_R)
    _put(ws, row, 7, "[calc]", font=_FONT_CALC)
    row += 1
    _put(ws, row, 1, "Enterprise value", font=FONT_LABEL, align=_ALIGN_L)
    ev_cell = _put(ws, row, 2,
                   f"={mc_cell.coordinate}+{ref['Net debt']}+{ref['Minority interest']}",
                   font=_FONT_CALC, numfmt=_fmt("currency"), align=_ALIGN_R)
    _put(ws, row, 7, "[calc]", font=_FONT_CALC)
    row += 2

    # --- multiples table: label | subject(live) | peer... | peer median ---
    peers = [_peer_inputs(p.slug, pack.peers.get(p.slug, {})) for p in spec.peers]
    header_row = row
    _put(ws, header_row, 1, "Multiple", font=FONT_LABEL)
    _put(ws, header_row, 2, spec.name, font=FONT_LABEL, align=_ALIGN_R)
    for i, p in enumerate(spec.peers):
        _put(ws, header_row, 3 + i, p.name, font=FONT_LABEL, align=_ALIGN_R)
    med_col = 3 + len(spec.peers)
    _put(ws, header_row, med_col, "Peer median", font=FONT_LABEL, align=_ALIGN_R)
    row += 1

    # live subject formulas per multiple
    subj_formula = {
        "pe_ltm": f"={mc_cell.coordinate}/{ref['Net income (LTM)']}",
        "pe_fwd": f"={ref['Price']}/{ref['EPS (fwd)']}",
        "ev_ebitda": f"={ev_cell.coordinate}/{ref['EBITDA (LTM)']}",
        "ev_sales": f"={ev_cell.coordinate}/{ref['Revenue (LTM)']}",
        "pbv": f"={mc_cell.coordinate}/{ref['Book value']}",
        "pfcf": f"={mc_cell.coordinate}/{ref['FCF (LTM)']}",
        "dvd_yield": None,  # given, not derivable from the input cells
    }
    for mkey, label, unit in MULTIPLES:
        _put(ws, row, 1, label, font=FONT_LABEL, align=_ALIGN_L)
        if subj_formula.get(mkey):
            _put(ws, row, 2, subj_formula[mkey], font=_FONT_CALC, numfmt=_fmt(unit), align=_ALIGN_R)
        else:
            _put(ws, row, 2, pack.subject.get("dvd_yield"), font=_FONT_INPUT,
                 numfmt=_fmt(unit), align=_ALIGN_R)
        peer_cells = []
        for i, p in enumerate(peers):
            v = company_multiples(p)[mkey]
            col = 3 + i
            _put(ws, row, col, v, font=_FONT_CALC, numfmt=_fmt(unit), align=_ALIGN_R)
            if v is not None:
                peer_cells.append(get_column_letter(col) + str(row))
        if peer_cells:
            _put(ws, row, med_col, f"=MEDIAN({','.join(peer_cells)})",
                 font=_FONT_CALC, numfmt=_fmt(unit), align=_ALIGN_R)
        row += 1
    return row + 1


def _render_generic(ws, block, row: int) -> int:
    for cell in block.rows:
        _put(ws, row, 1, cell.label, font=FONT_LABEL, align=_ALIGN_L)
        font = _FONT_INPUT if cell.source in ("bbg", "filing", "macro") else _FONT_CALC
        _put(ws, row, 2, cell.value, font=font, numfmt=_fmt(cell.unit), align=_ALIGN_R)
        _put(ws, row, 7, f"[{cell.source}]", font=_FONT_CALC, align=_ALIGN_L)
        if cell.note:
            _put(ws, row, 8, cell.note, font=_FONT_CALC, align=_ALIGN_L)
        row += 1
    return row + 1


def _render_cross_section_block(ws, block, spec, row: int) -> int:
    """Render a block with an attached peer cross-section (e.g. bank_snapshot): scalar rows
    first, then a Multiple | subject | peers… | median table (computed values, no formulas)."""
    xs = block.cross_section
    # scalar (non-multiple) rows render generically in col A/B
    multiple_labels = {label for _k, label, _u in _bank_multiple_defs(xs)}
    for cell in block.rows:
        if cell.label in multiple_labels:
            continue
        _put(ws, row, 1, cell.label, font=FONT_LABEL, align=_ALIGN_L)
        font = _FONT_INPUT if cell.source in ("bbg", "filing", "macro") else _FONT_CALC
        _put(ws, row, 2, cell.value, font=font, numfmt=_fmt(cell.unit), align=_ALIGN_R)
        _put(ws, row, 7, f"[{cell.source}]", font=_FONT_CALC, align=_ALIGN_L)
        row += 1
    row += 1

    # peer table
    _put(ws, row, 1, "Multiple", font=FONT_LABEL)
    _put(ws, row, 2, xs.subject, font=FONT_LABEL, align=_ALIGN_R)
    peer_names = [n for n in xs.order if n != xs.subject]
    for i, n in enumerate(peer_names):
        _put(ws, row, 3 + i, n, font=FONT_LABEL, align=_ALIGN_R)
    med_col = 3 + len(peer_names)
    _put(ws, row, med_col, "Peer median", font=FONT_LABEL, align=_ALIGN_R)
    row += 1
    for mkey, label, unit in _bank_multiple_defs(xs):
        _put(ws, row, 1, label, font=FONT_LABEL, align=_ALIGN_L)
        _put(ws, row, 2, xs.multiples[mkey].get(xs.subject), font=_FONT_CALC,
             numfmt=_fmt(unit), align=_ALIGN_R)
        for i, n in enumerate(peer_names):
            _put(ws, row, 3 + i, xs.multiples[mkey].get(n), font=_FONT_CALC,
                 numfmt=_fmt(unit), align=_ALIGN_R)
        _put(ws, row, med_col, xs.median.get(mkey), font=_FONT_CALC, numfmt=_fmt(unit), align=_ALIGN_R)
        row += 1
    return row + 1


def _bank_multiple_defs(xs):
    """The (key,label,unit) triples present in this cross-section (bank or REIT set)."""
    from src.coverage.peers import MULTIPLES_BANK, MULTIPLES_REIT
    defs = list(MULTIPLES_BANK) + [d for d in MULTIPLES_REIT if d not in MULTIPLES_BANK]
    return [(k, l, u) for (k, l, u) in defs if k in xs.multiples]


def build_valuation_workbook(model, spec, fund, pack, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Valuation"

    # header
    _put(ws, 1, 1, f"{model.name} — Soft Coverage", font=FONT_TITLE)
    _put(ws, 2, 1, f"Sheet 1: Fundamental Valuation Metrics", font=FONT_SUB)
    subtitle = f"{spec.ticker or ''}   {model.currency} {model.units}   as of {model.current_period}"
    _put(ws, 3, 1, subtitle.strip(), font=Font(name="Calibri", size=9, italic=True, color=_GREY))
    row = 5

    for block in model.blocks:
        _put(ws, row, 1, block.title, font=FONT_SECTION)
        row += 1
        if block.id == "snapshot_multiples":
            row = _render_snapshot(ws, block, spec, fund, pack, row)
        elif block.cross_section is not None:
            row = _render_cross_section_block(ws, block, spec, row)
        else:
            row = _render_generic(ws, block, row)

    if model.warnings:
        _put(ws, row, 1, "Notes / warnings", font=FONT_SUB)
        row += 1
        for w in model.warnings:
            _put(ws, row, 1, f"• {w}", font=_FONT_CALC)
            row += 1

    # column widths
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 16
    for i in range(len(spec.peers)):
        ws.column_dimensions[get_column_letter(3 + i)].width = 14
    ws.column_dimensions[get_column_letter(3 + len(spec.peers))].width = 14
    ws.column_dimensions["H"].width = 40
    ws.freeze_panes = "A5"

    wb.save(out)
    return out

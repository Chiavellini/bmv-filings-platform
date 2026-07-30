"""
soriana.py — deterministic recovery of Soriana's consolidated income statement.

Post-2024 Soriana PDFs render the income-statement table so pdfplumber emits one
character per token. The parsed markdown therefore shows char-spaced labels
("In g r e s o s T o t a le s") and the numbers as a per-column block of one
number per line, in fixed statement order. Q4 reports carry the quarterly column
first (page N) and the accumulated/FY column on the next page, so the *first*
income-statement number column is always the quarter we want.

This module de-spaces the text, finds the income-statement number column, and
maps it onto the statement's fixed line order. The order is stable across the
Spanish and English templates for every line we need:

    pos 0  Ingresos Totales / Net Sales                  -> revenue
    pos 1  Costo de Ventas / Cost of Sales               -> cogs
    pos 2  Utilidad Bruta / Gross Income                 -> gross_profit
    pos 3  Gastos de Operación / Operating Expenses      -> operating_expense
    pos 6  Util. de Operación en Efectivo / EBITDA       -> ebitda
    pos 7  Depreciación / Depreciation & Amortization    -> depreciation
    pos 8  Utilidad de Operación / Operating Income      -> operating_income
    pos 12 Costo Financiero Neto / Comprehensive Financ. -> interest_expense (abs)
    pos 14 Resultado Antes de Imp. / Earnings Before Tax -> ebt
    pos 15 Provisión para Impuestos / Tax Provision      -> tax_expense
    pos 16 Utilidad Neta Consolidada / Net Income        -> net_income

A candidate column is only accepted if it satisfies the accounting identities
(gross = revenue − cogs, ebitda = operating_income + depreciation,
net_income = ebt − tax) within tolerance, so a mis-aligned parse self-rejects
(yielding an honest MISS rather than a wrong value). Figures are in millions of
pesos already, so no scaling is applied.
"""

from __future__ import annotations

import re

from src.extract.extract_metrics import MetricRow, parse_number
from src.model.financial_model import MetricDef

# Fixed statement position -> canonical metric key (identical for ES and EN).
# Positions 4,5,9,10,13,17,18,19 are the extended income-statement lines that
# parse only in the char-spaced number-block era (~2023+); the older clean-row
# fallback recovers just the 11 core lines.
_POS_TO_KEY: dict[int, str] = {
    0: "revenue",
    1: "cogs",
    2: "gross_profit",
    3: "operating_expense",
    4: "income_before_other",
    5: "other_income_expense",
    6: "ebitda",
    7: "depreciation",
    8: "operating_income",
    9: "interest_income_net",
    10: "fx_gain_loss",
    12: "interest_expense",
    13: "minority_interest",
    14: "ebt",
    15: "tax_expense",
    16: "net_income",
    17: "controlling_interest",
    18: "noncontrolling_interest",
    19: "cash_net_profit",
}

_MIN_ROWS = 17           # need through Net Income (pos 16)
_NUM_RE = re.compile(r"^\(?-?\$?[\d.,]+\)?$")


def _despace(line: str) -> str:
    """Collapse single-space-separated characters ("4 0 ,4 4 4" -> "40,444").

    Only single spaces flanked by non-space characters are removed, so the
    column gaps (multiple spaces) and ordinary indentation are preserved.
    """
    return re.sub(r"(?<=\S) (?=\S)", "", line.strip())


def _is_number_line(despaced: str) -> bool:
    return bool(_NUM_RE.match(despaced)) and parse_number(despaced) is not None


def _number_blocks(text: str) -> list[list[float]]:
    """Return every run of >= _MIN_ROWS consecutive number-only lines, in order."""
    blocks: list[list[float]] = []
    run: list[float] = []
    for raw in text.splitlines():
        ds = _despace(raw)
        if ds and _is_number_line(ds):
            run.append(parse_number(ds))
        else:
            if len(run) >= _MIN_ROWS:
                blocks.append(run)
            run = []
    if len(run) >= _MIN_ROWS:
        blocks.append(run)
    return blocks


def _close(a: float | None, b: float | None, *, rel: float = 0.01, floor: float = 0.5) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(floor, rel * max(abs(a), abs(b)))


def _column_is_consistent(col: list[float]) -> bool:
    """True if the column is the money column AND satisfies the P&L identities.

    The percentage columns (revenue = 100, ...) also satisfy the identities
    proportionally, so we additionally require revenue to be a real money
    magnitude — Soriana quarterly revenue is tens of thousands of millions, never
    ~100 — which rejects the "%" and "Var %" columns.
    """
    rev, cogs, gross = col[0], col[1], col[2]
    ebitda, da, oi = col[6], col[7], col[8]
    ebt, tax, ni = col[14], col[15], col[16]
    if rev is None or rev < 1000:
        return False
    checks = [
        _close(gross, rev - cogs),
        _close(ebitda, oi + da),
        _close(ni, ebt - tax),
    ]
    # Require all three identities to hold — a mis-aligned column will fail one.
    return all(checks)


# Bilingual row labels for the clean-row fallback (older periods parse the income
# statement as "Label  current  %  prior  %  var" on one line). Operating Income's
# label must not swallow "Utilidad de Operación en Efectivo" (that's EBITDA).
_ROW_LABELS: dict[str, str] = {
    "revenue": r"(?:Net Sales|Ingresos\s+Totales|Total\s+Income)",
    "cogs": r"(?:Cost\s+of\s+Sales|Costo\s+de\s+Ventas)",
    "gross_profit": r"(?:Gross\s+(?:Income|Profit)|Utilidad\s+Bruta)",
    "operating_expense": r"(?:Operating\s+Expenses|Gastos\s+de\s+Operaci[óo]n)",
    "ebitda": r"(?:EBITDA|Utilidad\s+de\s+Operaci[óo]n\s+en\s+Efectivo)",
    "depreciation": r"(?:Depreciation(?:\s*&|\s+and)?\s*Amortization|Depreciaci[óo]n\s+y\s+[Aa]mortizaci[óo]n)",
    "operating_income": r"(?:Operating\s+Income|Utilidad\s+de\s+Operaci[óo]n(?!\s+en))",
    # EN-only: Spanish editions print Gastos/Productos Financieros as two rows.
    "interest_income_net": r"Interest\s+Income\s+and\s+\(?Expenses?\)?,?\s*Net",
    "interest_expense": r"(?:Comprehensive\s+Financing(?:\s+Income)?|Costo\s+Financiero\s+Neto)",
    "ebt": r"(?:Earnings\s+Before\s+Tax|Resultado\s+Antes\s+de\s+Impuestos)",
    "tax_expense": r"(?:Tax\s+Provision|Provisi[óo]n\s+para\s+Impuestos)",
    "net_income": r"(?:Net\s+Income|Utilidad\s+Neta\s+Consolidada)",
}


# Must require the "Consolidated/Consolidado" qualifier: the 2019 IFRS-16
# sections mention a bare "Estado de Resultados" in prose ("Impacto en el
# Estado de Resultados") before their impact table.
_STATEMENT_HEADER_RE = re.compile(
    r"Consolidated\s+Statements?\s+of\s+Income|Estados?\s+de\s+Resultados\s+Consolidados?",
    re.IGNORECASE,
)


def _row_anchored_column(text: str) -> dict[str, float]:
    """Clean-row fallback: 'Label  current  %  prior …' → current (first number).

    Uses the FIRST occurrence of each label (the quarterly table precedes the
    accumulated/FY one). Operates on RAW lines (clean rows have normal spacing).
    Search starts at the income-statement header when one exists: the 2019
    reports print an "Effects of the IFRS 16" impact table BEFORE the P&L whose
    "Operating Expenses 171.5 0.4" row otherwise wins the first-match rule.
    """
    header = _STATEMENT_HEADER_RE.search(text)
    if header:
        text = text[header.start():]
    out: dict[str, float] = {}
    for key, lab in _ROW_LABELS.items():
        # Allow trailing label words before the first figure (e.g. "Earnings
        # Before Tax & Profit Sharing 1,207"); the gap excludes digits. The value
        # must be FOLLOWED by another number/paren (the % or prior-year column),
        # which distinguishes a table row ("9,738 23.3") from a prose mention
        # ("Gross Profit reached $9.738 billion").
        m = re.search(rf"^\s*{lab}[^\d\n]*?(\(?\$?-?[\d,]+(?:\.\d+)?\)?)\s+[\d(]",
                      text, re.IGNORECASE | re.MULTILINE)
        if m:
            v = parse_number(m.group(1))
            if v is not None:
                out[key] = abs(v) if key == "interest_expense" else v
    return out


def extract_soriana_income_statement(
    text: str, metric_defs: list[MetricDef], period: str | None = None
) -> dict[str, MetricRow]:
    """Recover the quarterly P&L from a (possibly char-spaced) Soriana report.

    Returns ``{metric_key: MetricRow}`` for the current-quarter column, or ``{}``
    if no internally-consistent income-statement column is found.
    """
    if not text:
        return {}
    units = {m.key: m.unit for m in metric_defs}

    # Primary: char-spaced number-column block, mapped by fixed position.
    values_by_key: dict[str, float] = {}
    for block in _number_blocks(text):
        candidate = block[:20] if len(block) >= 20 else block
        if len(candidate) >= _MIN_ROWS and _column_is_consistent(candidate):
            for pos, key in _POS_TO_KEY.items():
                if pos < len(candidate) and candidate[pos] is not None:
                    v = candidate[pos]
                    values_by_key[key] = abs(v) if key == "interest_expense" else v
            break

    # Fallback: clean-row layout (older periods). Only when block mode found
    # nothing, and only if the recovered column is internally consistent — so a
    # stray FY/accumulated table cannot pass without matching the identities.
    if not values_by_key:
        row = _row_anchored_column(text)
        ok = (
            {"revenue", "cogs", "gross_profit"} <= row.keys()
            and _close(row["gross_profit"], row["revenue"] - row["cogs"])
            and row["revenue"] > 1000
        )
        if ok:
            values_by_key = row

    if not values_by_key:
        return {}

    return {
        key: MetricRow(
            metric=key, label_es=key, current=float(val), prior=None,
            var_pct=None, unit=units.get(key, "currency"),
            source_line="[statement] soriana income statement",
        )
        for key, val in values_by_key.items()
    }


# ---------------------------------------------------------------------------
# Operational KPIs: per-format store units & sales-floor area, totals, Sodimac
# ---------------------------------------------------------------------------
# The "Operational Information" table lists, per store format, the current-period
# unit count and sales-floor area (m²). Layouts drift over a decade (Spanish
# "Soriana Híper" in 2016; a two-area-column layout in 2019–21; a units/area/%
# layout in 2022+), but a robust invariant holds on every clean-row layout:
#   units = the FIRST integer on the line; sales-floor area = the LARGEST number.
# (Store counts are < ~2,000; sales-floor areas are > 100,000 m².) The 2024 and
# 2025-1T..3T reports OCR-parse the table vertically char-by-char and are not
# recoverable here — those periods yield no store KPIs (honest MISS).
_FORMAT_LABELS: dict[str, str] = {
    # Labels drift over the decade: "Hiper"/"Soriana Híper"/"Hipermercados";
    # "Mercado"/"Soriana Mercado"; etc. Allow the optional "Soriana " prefix and
    # the "-mercados" suffix on Hiper.
    "hiper":    r"(?:Soriana\s+)?H[íi]per(?:mercados)?",
    "super":    r"(?:Soriana\s+)?S[úu]per",
    "mercado":  r"(?:Soriana\s+)?Mercado",
    "express":  r"(?:Soriana\s+)?Express",
    "cityclub": r"(?:Soriana\s+)?City\s*Club",
    # "Total" must be the ops-table total, never a balance-sheet line
    # ("Total Current Asset", "Total Activo", "Total Pasivo y Capital"…).
    "total":    r"Total(?!\s+(?:Current|Assets?|Liabilit|Stockholder|Activo|Pasivo|Capital|Patrimonio|Ingresos|Costo))",
    "sodimac":  r"Sodimac",
}

# canonical format -> (units_key, area_key)
_FORMAT_KEYS: dict[str, tuple[str, str]] = {
    "hiper":    ("units_hiper", "area_hiper"),
    "super":    ("units_super", "area_super"),
    "mercado":  ("units_mercado", "area_mercado"),
    "express":  ("units_express", "area_express"),
    "cityclub": ("units_cityclub", "area_cityclub"),
    "total":    ("total_units", "total_sales_floor"),
    "sodimac":  ("sodimac_units", "sodimac_area"),
}

_INT_RE = re.compile(r"\d[\d,]*")
# Caption that anchors the operational-information / store-format table page.
_OPS_CAPTION = re.compile(r"store\s+format|operational\s+information|piso\s+de\s+venta|sales-?floor",
                          re.IGNORECASE)
_PDF_GAP = 5.0   # x-gap (pt) above which two chars belong to different columns


def _line_numbers(line: str) -> list[int]:
    out: list[int] = []
    for tok in _INT_RE.findall(line):
        v = tok.replace(",", "")
        if v.isdigit():
            out.append(int(v))
    return out


def _reconstruct_lines(chars: list[dict]) -> list[str]:
    """Rebuild text lines from PDF chars, inserting a space on large x-gaps.

    Groups chars by rounded ``top`` (one visual row) and reads left→right by
    ``x0``; a gap wider than ``_PDF_GAP`` between glyphs marks a column break, so
    "Hiper 372 368 2,674,367" survives instead of collapsing to one token. Works
    for both upright rows and Soriana's 90°-rotated ops table (whose reading line
    is also constant-``top`` once the rotated glyphs are isolated by the caller).
    """
    from collections import defaultdict
    rows: dict[int, list[dict]] = defaultdict(list)
    for c in chars:
        rows[round(c["top"])].append(c)
    lines: list[str] = []
    for top in sorted(rows):
        seq = sorted(rows[top], key=lambda c: c["x0"])
        buf, prev_x1 = [], None
        for c in seq:
            if prev_x1 is not None and c["x0"] - prev_x1 > _PDF_GAP:
                buf.append(" ")
            buf.append(c["text"])
            prev_x1 = c["x1"]
        line = "".join(buf).strip()
        if line:
            lines.append(line)
    return lines


def _ops_region(lines: list[str]) -> list[str]:
    """Slice the markdown to the operational-information table window.

    Anchors on the caption ("comparative table … by store format" / "Operational
    Information") and returns the following ~18 lines (through the footnote), so a
    balance-sheet "Total …" elsewhere in the report can't be mistaken for the
    store total. Falls back to all lines if no caption is found.
    """
    anchor = re.compile(r"comparative\s+table|operational\s+information|store\s+format",
                        re.IGNORECASE)
    for i, ln in enumerate(lines):
        if anchor.search(ln):
            return lines[i: i + 18]
    return lines


def _pdf_store_lines(pdf_path) -> list[str]:
    """Reconstructed ops-table lines from the PDF (upright + rotated), or []."""
    try:
        import pdfplumber
    except Exception:
        return []
    out: list[str] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if not _OPS_CAPTION.search(text):
                    continue
                chars = page.chars
                upright = [c for c in chars if c.get("upright") is not False]
                rotated = [c for c in chars if c.get("upright") is False]
                out.extend(_reconstruct_lines(upright))
                out.extend(_reconstruct_lines(rotated))
    except Exception:
        return out
    return out


def extract_soriana_stores(
    text: str, metric_defs: list[MetricDef], period: str | None = None, pdf_path=None
) -> dict[str, MetricRow]:
    """Recover per-format store units & sales-floor area from the ops table.

    Tries the markdown text first; for the periods whose table OCR-parses into
    rotated/diagonal char streams (2024–25) it reconstructs the table from the
    sibling PDF by page geometry. Per row: units = first integer < 2,000 (current
    column), sales-floor area = first integer > 10,000 (current column precedes
    the prior-year column in every layout).
    """
    units = {m.key: m.unit for m in metric_defs}
    defs_keys = set(units)

    def _parse(lines: list[str]) -> dict[str, tuple[int, int]]:
        found: dict[str, tuple[int, int]] = {}
        for fmt, lab in _FORMAT_LABELS.items():
            if fmt in found:
                continue
            pat = re.compile(rf"^\s*{lab}\s+\d", re.IGNORECASE)
            for ln in lines:
                if not pat.match(ln):
                    continue
                ns = _line_numbers(ln)
                u = next((n for n in ns if n < 2000), None)
                ai = next((i for i, n in enumerate(ns) if n > 10000), None)
                if u is None or ai is None or ns[ai] == u:
                    continue
                area = ns[ai]
                # OCR split: a lone thousands digit gets separated from a 5-digit
                # fragment ("259,424" → "2 59,424"). It sits AFTER units+prior
                # (area index ≥ 3) as a single digit; merge it back. Sodimac's
                # genuine 5-digit area is at index 2 (units, prior, area) → untouched.
                if area < 100000 and ai >= 3 and ns[ai - 1] < 10:
                    area = ns[ai - 1] * 100000 + area
                found[fmt] = (u, area)
                break
        return found

    parsed = _parse(_ops_region(text.splitlines())) if text else {}
    # If the markdown is missing rows (rotated/garbled table), recover from the PDF.
    if pdf_path and len(parsed) < len(_FORMAT_KEYS):
        for fmt, ua in _parse(_pdf_store_lines(pdf_path)).items():
            parsed.setdefault(fmt, ua)

    out: dict[str, MetricRow] = {}
    for fmt, (u, area) in parsed.items():
        ukey, akey = _FORMAT_KEYS[fmt]
        for key, val in ((ukey, u), (akey, area)):
            if key in defs_keys:
                out[key] = MetricRow(
                    metric=key, label_es=key, current=float(val), prior=None,
                    var_pct=None, unit=units.get(key, "count"),
                    source_line=f"[statement] soriana ops table ({fmt})",
                )
    return out


# Same-store-sales (SSS) for the quarter, stated in prose. English reports phrase
# it "Same-Store-Sales for the quarter resulted in X%" / "SSS was X%"; the
# quarterly figure precedes the "total stores" / cumulative figure in the sentence.
_SSS_PATTERNS = [
    re.compile(r"Same[- ]Store[- ]Sales\s+for\s+the\s+quarter\s+(?:resulted\s+in|was)\s+(-?[\d.]+)\s*%", re.IGNORECASE),
    re.compile(r"\bSSS\s+(?:for\s+the\s+quarter\s+)?(?:was|of|resulted\s+in)\s+(-?[\d.]+)\s*%", re.IGNORECASE),
    re.compile(r"Same[- ]Store[- ]Sales[^.%]{0,40}?(-?[\d.]+)\s*%", re.IGNORECASE),
]


def extract_soriana_sss(
    text: str, metric_defs: list[MetricDef], period: str | None = None
) -> dict[str, MetricRow]:
    """Recover the quarterly same-store-sales % from prose (when disclosed)."""
    if not text or "sss" not in {m.key for m in metric_defs}:
        return {}
    for pat in _SSS_PATTERNS:
        m = pat.search(text)
        if m:
            v = parse_number(m.group(1))
            if v is not None and -50 <= v <= 50:
                return {"sss": MetricRow(
                    metric="sss", label_es="Same-Store Sales", current=float(v),
                    prior=None, var_pct=None, unit="pct",
                    source_line="[search] soriana SSS prose",
                )}
    return {}


# Quarterly capex prose — every pattern carries an explicit quarter cue so the
# annual headline and the (negative, cumulative) cash-flow outflow can't match.
# (regex, multiplier). million → ×1, billion → ×1000 (model unit = MXN millions).
_CAPEX_Q_PATTERNS: list[tuple[str, float]] = [
    (r'capex\s+invested\s+during\s+the\s+quarter\s+was\s+\$?\s*([\d.]+)\s+billion', 1000.0),
    (r'capex\s+invested\s+during\s+the\s+quarter\s+was\s+\$?\s*([\d.,]+)\s+million', 1.0),
    (r'[Cc]apex\s+of\s+\$?\s*([\d.,]+)\s+billion\s+pesos?\s+in\s+the\s+quarter', 1000.0),
    (r'[Cc]apex\s+of\s+\$?\s*([\d.,]+)\s+million\s+pesos?\s+in\s+the\s+quarter', 1.0),
    (r'\d\s*[QT]\s*20\d\d\s*,?\s*CAPEX\s+is\s+\$?\s*([\d.]+)\s+billion', 1000.0),
    (r'\d\s*[QT]\s*20\d\d\s*,?\s*CAPEX\s+is\s+\$?\s*([\d.,]+)\s+million', 1.0),
    (r'CAPEX\s+invertido\s+en\s+el\s+\d[TQ]\d{2}[^$]{0,60}?\$?\s*([\d.,]+)\s*millones', 1.0),
]


def extract_soriana_capex(
    text: str, metric_defs: list[MetricDef], period: str | None = None
) -> dict[str, MetricRow]:
    """Quarterly capex from explicit prose only (never the cumulative cash-flow).

    Tagged ``[search]`` so the config can pin ``capex`` to this source and exclude
    the base prose patterns that otherwise grab the negative cash-flow outflow.
    """
    if not text or "capex" not in {m.key for m in metric_defs}:
        return {}
    for rx, mult in _CAPEX_Q_PATTERNS:
        m = re.search(rx, text)
        if m:
            v = parse_number(m.group(1))
            if v is not None and v > 0:
                return {"capex": MetricRow(
                    metric="capex", label_es="Capex (quarter)", current=float(v) * mult,
                    prior=None, var_pct=None, unit="currency",
                    source_line="[qcapex] soriana quarterly capex prose",
                )}
    return {}


def extract_soriana(
    text: str, metric_defs: list[MetricDef], period: str | None = None, pdf_path=None
) -> dict[str, MetricRow]:
    """Unified Soriana extractor: income statement + store KPIs + SSS + capex."""
    out: dict[str, MetricRow] = {}
    out.update(extract_soriana_income_statement(text, metric_defs, period))
    out.update(extract_soriana_stores(text, metric_defs, period, pdf_path=pdf_path))
    out.update(extract_soriana_sss(text, metric_defs, period))
    out.update(extract_soriana_capex(text, metric_defs, period))
    return out

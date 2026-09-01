"""KIMBER (Kimberly-Clark de México) — quarterly results table + balance Total.

The KCM release prints the SAME row labels twice: "RESULTADOS FINANCIEROS
TRIMESTRALES" (the quarter) is followed two pages later by "RESULTADOS
FINANCIEROS ACUMULADOS" (year-to-date) — VENTAS NETAS, UTILIDAD BRUTA,
UTILIDAD DE OPERACIÓN, UTILIDAD NETA, EBITDA appear in both. This is the
`skip_annual_rows` concern the config flags for the search tier: any label
match that is not table-aware risks answering the YTD figure. Here rows are
read only inside the TRIMESTRALES window, and the window closes the moment
the ACUMULADOS (or any other) heading appears.

The column order is confirmed from the table's own header ("2T’26  2T’25
VARIACIÓN" — current year first): both quarter tokens are parsed and the
value columns are mapped by YEAR, not by position. No readable header means
no rows — an honest miss the other tiers can fill, never a guess.

`total_assets` comes from POSICIÓN FINANCIERA, whose Activos block closes
with a bare "Total  $ 56,905  $ 47,778" row (no "de activos" label). The
liabilities+equity block ends with the identical balancing Total; the read
requires both and their equality — the balance identity is the cross-check.
The prior column is the prior-year June 30 balance, as printed.
"""
from __future__ import annotations

import re

from src.extract.custom_registry import STATEMENT_TIER
from src.extract.extract_metrics import MetricRow
from src.extract.segment_tables import (NUMERIC_TAIL_RE, collapse,
                                        document_guard, emit, norm, numbers)
from src.model.financial_model import MetricDef

_QUARTERLY_RE = re.compile(
    r"^(?:RESULTADOS\s+FINANCIEROS\s+TRIMESTRALES|QUARTERLY\s+FINANCIAL\s+RESULTS)\b"
)
# Any other RESULTADOS/POSICIÓN heading ends the quarterly window.
_HEADING_RE = re.compile(
    r"^RESULTADOS\s+FINANCIEROS\b|^POSICION\s+FINANCIERA\b|"
    r"^YTD\s+FINANCIAL\s+RESULTS\b|^FINANCIAL\s+POSITION\b"
)

# "2T’26" / "4T'25" / "2T26" — quarter tokens in the table's own header row.
_QTOKEN_RE = re.compile(r"\b([1-4])\s*[QT]\s*[’'`]?\s*(\d{2})\b")

_ROWS = (
    (re.compile(r"^(?:VENTAS\s+NETAS|NET\s+SALES)\b"), "revenue"),
    (re.compile(r"^(?:UTILIDAD\s+BRUTA|GROSS\s+PROFIT)\b"), "gross_profit"),
    (re.compile(r"^(?:UTILIDAD\s+DE\s+OPERACION|OPERATING\s+PROFIT)\b"),
     "operating_income"),
    # negative lookahead: "UTILIDAD NETA POR ACCIÓN" must not bind net_income
    (re.compile(r"^(?:UTILIDAD\s+NETA(?!\s+POR)|NET\s+INCOME)\b"), "net_income"),
    (re.compile(r"^EBITDA\b"), "ebitda"),
)
# 4Q25 prints 11 furniture lines between the heading and the header row and the
# EBITDA row 26 lines in; the ACUMULADOS heading still closes the window early.
_QUARTERLY_WINDOW = 30

_POSITION_RE = re.compile(r"^(?:POSICION\s+FINANCIERA|FINANCIAL\s+POSITION)\b")
_TOTAL_RE = re.compile(r"^TOTAL\b")
_POSITION_WINDOW = 45

_VOLUME_RE = re.compile(
    r"\bTOTAL\s+VOLUME\s+(?:WAS\s+)?(?:UP|INCREASED|ROSE)\s+([\d.]+)\s*%|"
    r"\bVOLUMEN\s+TOTAL\s+(?:AUMENTO|CRECIO)\s+([\d.]+)\s*%"
)
_VOLUME_DOWN_RE = re.compile(
    r"\bTOTAL\s+VOLUME\s+(?:WAS\s+)?(?:DOWN|DECREASED|DECLINED)\s+([\d.]+)\s*%|"
    r"\bVOLUMEN\s+TOTAL\s+(?:DISMINUYO|DECRECIO)\s+([\d.]+)\s*%"
)


def extract_kimber(
    text: str,
    metric_defs: list[MetricDef],
    period: str | None = None,
    pdf_path=None,
) -> dict[str, MetricRow]:
    """Quarter-table consolidated metrics + POSICIÓN FINANCIERA total assets."""
    if not text or not document_guard(text, "KIMBERLY-CLARK", "KIMBERLY CLARK"):
        return {}

    lines = [norm(collapse(raw)) for raw in text.splitlines()]
    defs = {m.key: m for m in metric_defs}
    found: dict[str, MetricRow] = {}

    for start, line in enumerate(lines):
        if _QUARTERLY_RE.match(line):
            _read_quarter_table(lines[start + 1:start + 1 + _QUARTERLY_WINDOW],
                                defs, found)
        elif _POSITION_RE.match(line):
            _read_position(lines[start + 1:start + 1 + _POSITION_WINDOW],
                           defs, found)

    # KCM states the consolidated volume bridge in the quarterly earnings call,
    # cached beside the short release as a period-specific supplemental material.
    for line in lines:
        m = _VOLUME_RE.search(line)
        if m:
            value = float(next(g for g in m.groups() if g is not None))
            emit(found, defs, "total_volume_yoy", current=value,
                 line=line, tag=STATEMENT_TIER)
            break
        m = _VOLUME_DOWN_RE.search(line)
        if m:
            value = -float(next(g for g in m.groups() if g is not None))
            emit(found, defs, "total_volume_yoy", current=value,
                 line=line, tag=STATEMENT_TIER)
            break

    revenue = found.get("revenue")
    if revenue and revenue.current is not None and revenue.prior not in (None, 0):
        growth = (revenue.current / revenue.prior - 1.0) * 100.0
        emit(found, defs, "sales_yoy_exact", current=growth,
             line=(f"VENTAS NETAS exact bridge {revenue.current:g} / "
                   f"{revenue.prior:g} - 1"), tag=STATEMENT_TIER)
    return found


def _read_quarter_table(window: list[str], defs, found: dict) -> None:
    """Rows below the TRIMESTRALES heading, columns mapped via the header years."""
    current_first: bool | None = None
    for line in window:
        if _HEADING_RE.match(line):
            return                      # ACUMULADOS (or the balance) starts here
        if current_first is None:
            tokens = _QTOKEN_RE.findall(line)
            if len(tokens) >= 2:
                # Same quarter, different years: the later year is the current
                # column. (2T’26 before 2T’25 → current first.)
                current_first = int(tokens[0][1]) >= int(tokens[1][1])
            continue                    # no rows before the header is read
        for row_re, key in _ROWS:
            if key in found:
                continue
            m = row_re.match(line)
            if not m:
                continue
            tail = line[m.end():].strip()
            if not NUMERIC_TAIL_RE.match(tail):
                continue
            # "$14,448 $14,070 3%" — the variación column carries % and drops.
            values = numbers(tail, drop_pct=True)
            if len(values) < 2:
                continue
            current, prior = (values[0], values[1]) if current_first \
                else (values[1], values[0])
            emit(found, defs, key, current=current, prior=prior,
                 line=line, tag=STATEMENT_TIER)
            break


def _read_position(window: list[str], defs, found: dict) -> None:
    """The two balancing `Total` rows of POSICIÓN FINANCIERA -> total_assets."""
    if "total_assets" in found:
        return
    totals: list[tuple[float, float]] = []
    for line in window:
        m = _TOTAL_RE.match(line)
        if not m:
            continue
        values = numbers(line[m.end():].strip(), drop_pct=True)
        if len(values) >= 2:
            totals.append((values[0], values[1]))
        if len(totals) == 2:
            break
    # Assets block total and liabilities+equity total must balance exactly.
    if len(totals) == 2 and totals[0] == totals[1]:
        emit(found, defs, "total_assets",
             current=totals[0][0], prior=totals[0][1],
             line=f"POSICION FINANCIERA Total {totals[0][0]:,.0f}",
             tag=STATEMENT_TIER)

"""AC (Arca Continental) — total consolidated net income from statement components.

Why a dedicated extractor: AC's release NEVER prints total consolidated net
income. The consolidated income statement ends (2026-2T.md:530-537):

    Earnings Before Taxes 9,102 9,751 -649 -6.7 ...
    Profit Taxes       -2,936 -3,100 164 -5.3 ...
    Non-controlling interest -1,215 -1,184 -31 2.6 ...
       Net Profit      4,952 5,467 -516  -9.4 ...

"Net Profit" is the MAJORITY-only figure (NCI is deducted as an expense line
above it), while the GT model tracks the TOTAL including non-controlling
interest — which only exists as EBT − |taxes| (2Q26: 9,102 − 2,936 = 6,166).
The generic search tier grabbed the printed 4,952 and tripped the step-2.1
VALIDATION_BREACH every quarter.

The derivation is emitted only when its independent counterpart, Net Profit +
|NCI| (4,952 + 1,215 = 6,167), agrees within 0.5% — a misparsed row then yields
an honest MISS instead of a confident wrong value.

Layouts handled (recent-quarter corpus): the English release (2025-3T, 2026-2T,
"Consolidated Income Statement" / "Earnings Before Taxes") and the Spanish
SIFIC bundle (2026-1T, "Estado Consolidado de Resultados" / "Utilidad antes de
impuestos"). The 2025-2T Spanish filing wraps the EBT label across three lines
("Utilidad antes de" / numbers / "impuestos") — that row cannot be latched, so
the extractor returns {} there and the generic cascade keeps serving it.

Both the segment matrix ("Earnings Before Taxes 5,161 3,288 ... 9,510"), the
cash-flow statement (YTD "Earnings Before Taxes 26,683 25,367") and the balance
sheet's equity "Net Profit" row print the same labels with the WRONG columns,
so rows are only read inside a bounded window latched to the consolidated
income-statement heading.
"""
from __future__ import annotations

import re

from src.extract.custom_registry import STATEMENT_TIER
from src.extract.segment_tables import (NUMERIC_TAIL_RE, collapse,
                                        document_guard, emit, norm, numbers)
from src.extract.extract_metrics import MetricRow
from src.model.financial_model import MetricDef

# Section heading of the consolidated income statement (normed + collapsed).
# EN: "Consolidated           Income        Statement"; ES SIFIC bundle:
# "Estado      Consolidado          de   Resultados".
_SECTION_RE = re.compile(
    r"CONSOLIDATED\s+INCOME\s+STATEMENT|ESTADO\s+CONSOLIDADO\s+DE\s+RESULTADOS")

# Rows are only trusted this many lines past the heading — far enough to cross
# the SIFIC page break inside the statement (2026-1T), short enough never to
# reach the balance sheet / cash flow / segment matrices that reuse the labels.
_WINDOW_LINES = 90

# The two derivations (EBT − |taxes| vs Net Profit + |NCI|) must agree within
# this relative tolerance or nothing is emitted (rounding of printed millions
# makes them differ by ±1: 6,166 vs 6,167 in 2Q26).
_XCHECK_TOL = 0.005

_ROW_RES = {
    "ebt": re.compile(
        r"^EARNINGS\s+BEFORE\s+TAXES\b|^UTILIDAD\s+ANTES\s+DE\s+IMPUESTOS\b"),
    "taxes": re.compile(
        r"^PROFIT\s+TAXES\b|^IMPUESTOS?\s+A\s+LA\s+UTILIDAD\b"),
    "nci": re.compile(
        r"^NON-?CONTROLLING\s+INTEREST\b|^PARTICIPACION\s+NO\s+CONTROLADORA\b"),
    "net_majority": re.compile(
        r"^NET\s+PROFIT\b|^UTILIDAD\s+NETA\b"),
}


def extract_ac(
    text: str,
    metric_defs: list[MetricDef],
    period: str | None = None,
    pdf_path=None,
) -> dict[str, MetricRow]:
    """Emit net_income = EBT − |taxes| from AC's consolidated income statement."""
    if not text or not document_guard(text, "ARCA CONTINENTAL"):
        return {}

    defs = {m.key: m for m in metric_defs}
    found: dict[str, MetricRow] = {}
    _extract_segment_actuals(text.splitlines(), defs, found, period)

    rows = _statement_rows(text.splitlines())
    if rows is None:
        return found

    ebt, taxes = rows["ebt"], rows["taxes"]
    nci, net = rows["nci"], rows["net_majority"]

    current = ebt[0] - abs(taxes[0])
    crosscheck = net[0] + abs(nci[0])
    if current <= 0 or abs(current - crosscheck) > _XCHECK_TOL * abs(current):
        return found

    # Prior-year column, same derivation — kept only if it cross-checks too.
    prior = ebt[1] - abs(taxes[1])
    if abs(prior - (net[1] + abs(nci[1]))) > _XCHECK_TOL * max(abs(prior), 1e-9):
        prior = None

    emit(found, defs, "net_income",
         current=current, prior=prior,
         line=(f"derived: EBT {ebt[0]:,.0f} - taxes {abs(taxes[0]):,.0f} = "
               f"{current:,.0f} (check: Net Profit {net[0]:,.0f} + NCI "
               f"{abs(nci[0]):,.0f} = {crosscheck:,.0f})"),
         tag=STATEMENT_TIER)
    return found


_MATRIX_KEYS = {
    "sales": (
        "ac_sales_mexico", "ac_sales_us", "ac_sales_peru",
        "ac_sales_argentina", "ac_sales_ecuador", "ac_sales_other",
        "ac_sales_eliminations", "ac_sales_total",
    ),
    "operating_income": (
        "ac_ebit_mexico", "ac_ebit_us", "ac_ebit_peru",
        "ac_ebit_argentina", "ac_ebit_ecuador", "ac_ebit_other",
        "ac_ebit_eliminations", "ac_ebit_total",
    ),
    "ebitda": (
        "ac_ebitda_bev_mexico", "ac_ebitda_bev_us", "ac_ebitda_peru",
        "ac_ebitda_argentina", "ac_ebitda_ecuador", "ac_ebitda_other",
        "ac_ebitda_eliminations", "ac_ebitda_total",
    ),
}

_REGION_KEYS = {
    "mexico": {
        "sales": "revenue_mexico", "ebitda": "ac_ebitda_mexico",
        "sparkling": "ac_volume_mexico_sparkling", "water": "ac_volume_mexico_water",
        "stills": "ac_volume_mexico_stills", "jug": "ac_volume_mexico_jug",
    },
    "us": {
        "sales": "revenue_us", "ebitda": "ac_ebitda_us",
        "sparkling": "ac_volume_us_sparkling", "water": "ac_volume_us_water",
        "stills": "ac_volume_us_stills",
    },
    "southam": {
        "sales": "revenue_south_america", "ebitda": "ac_ebitda_southam",
        "sparkling": "ac_volume_southam_sparkling", "water": "ac_volume_southam_water",
        "stills": "ac_volume_southam_stills", "jug": "ac_volume_southam_jug",
    },
}


def _extract_segment_actuals(lines: list[str], defs: dict[str, MetricDef],
                             found: dict[str, MetricRow], period: str | None) -> None:
    matrix = _segment_matrix(lines, period)
    for row_name, keys in _MATRIX_KEYS.items():
        vals = matrix.get(row_name)
        if vals is None:
            continue
        for key, value in zip(keys, vals):
            emit(found, defs, key, current=value, line=f"AC segment matrix {row_name}",
                 tag=STATEMENT_TIER)

    volume = matrix.get("volume")
    if volume is not None and len(volume) >= 5:
        for key, value in zip(("ac_volume_peru", "ac_volume_argentina",
                               "ac_volume_ecuador"), volume[2:5]):
            emit(found, defs, key, current=value, line="AC segment matrix volume",
                 tag=STATEMENT_TIER)

    for region, rows in _region_tables(lines, period).items():
        for row_name, pair in rows.items():
            key = _REGION_KEYS[region].get(row_name)
            if key is None:
                continue
            emit(found, defs, key, current=pair[0], prior=pair[1],
                 line=f"AC {region} table {row_name}", tag=STATEMENT_TIER)


def _period_tokens(period: str | None) -> tuple[str, ...]:
    m = re.search(r"([1-4])[QT](\d{2})", period or "", re.I)
    if not m:
        return ()
    q, yy = m.groups()
    return (f"{q}Q{yy}", f"{q}T{yy}")


def _segment_matrix(lines: list[str], period: str | None) -> dict[str, list[float]]:
    tokens = _period_tokens(period)
    starts: list[int] = []
    for idx, raw in enumerate(lines):
        line = norm(collapse(raw))
        if not re.search(r"INFORMATION BY SEGMENTS|INFORMACION POR SEGMENTOS", line):
            continue
        if tokens and not any(token in line.replace(" ", "") for token in tokens):
            continue
        if re.search(r"JAN|ENE|JUN|6M", line):
            continue
        starts.append(idx)
    for start in starts:
        window = lines[start:start + 55]
        out: dict[str, list[float]] = {}
        for idx, raw in enumerate(window):
            line = norm(collapse(raw))
            # The quarterly and YTD matrices are adjacent in modern releases.
            # Never let the second heading overwrite the already parsed quarter.
            if idx and re.search(
                r"INFORMATION BY SEGMENTS|INFORMACION POR SEGMENTOS", line
            ):
                break
            vals = numbers(line, drop_pct=True)
            if re.match(r"^VOLUME BY SEGMENT\b", line) and len(vals) >= 6:
                out["volume"] = vals
            elif re.match(r"^SALES BY SEGMENT\b", line) and len(vals) >= 8:
                out["sales"] = vals[:8]
            elif re.match(r"^OPERATING INCOME\b", line) and len(vals) >= 8:
                out["operating_income"] = vals[:8]
            elif re.match(r"^EBITDA\b", line) and "MARGIN" not in line and len(vals) >= 8:
                out["ebitda"] = vals[:8]

            # Spanish SIFIC flattens a wrapped row as label-prefix / values /
            # label-suffix.  The numeric line is therefore the only stable cell.
            if not vals or idx == 0:
                continue
            prev = norm(collapse(window[idx - 1]))
            nxt = norm(collapse(window[idx + 1])) if idx + 1 < len(window) else ""
            if prev.startswith("VOLUMEN POR") and "SEGMENTO" in nxt and len(vals) >= 6:
                out["volume"] = vals
            elif prev.startswith("INGRESOS DEL") and "SEGMENTO" in nxt and len(vals) >= 8:
                out["sales"] = vals[:8]
            elif prev.startswith("UTILIDAD DE") and "OPERACION" in nxt and len(vals) >= 8:
                out["operating_income"] = vals[:8]
            elif prev == "FLUJO" and nxt.startswith("OPERATIVO") and len(vals) >= 8:
                out["ebitda"] = vals[:8]
        if {"volume", "sales", "operating_income", "ebitda"} <= set(out):
            return out
    return {}


def _region_tables(lines: list[str], period: str | None
                   ) -> dict[str, dict[str, tuple[float, float | None]]]:
    headings = {
        "mexico": re.compile(r"TABLE\s+3:\s+MEXICO\s+DATA|TABLA\s+3:\s+CIFRAS\s+PARA\s+MEXICO"),
        "us": re.compile(r"TABLE\s+4:\s+UNITED\s+STATES\s+DATA|TABLA\s+4:\s+CIFRAS\s+PARA\s+ESTADOS\s+UNIDOS"),
        "southam": re.compile(r"TABLE\s+5:\s+SOUTH\s+AMERICA\s+DATA|TABLA\s+5:\s+CIFRAS\s+PARA\s+SUDAMERICA"),
    }
    row_patterns = (
        ("sparkling", re.compile(r"^SPARKLING TOTAL VOLUME\b|^TOTAL REFRESCOS\b")),
        ("water", re.compile(r"^WATER\b|^AGUA\b")),
        ("stills", re.compile(r"^STILL BEVERAGES\b|^NO CARBONATADOS\b")),
        ("jug", re.compile(r"^JUG\b|^GARRAFON\b")),
        ("sales", re.compile(r"^NET SALES\b|^VENTAS NETAS\b")),
        ("ebitda", re.compile(r"^EBITDA\b")),
    )
    out: dict[str, dict[str, tuple[float, float | None]]] = {}
    for region, heading in headings.items():
        starts = [idx for idx, raw in enumerate(lines)
                  if heading.search(norm(collapse(raw)))]
        for start in starts:
            got: dict[str, tuple[float, float | None]] = {}
            for idx, raw in enumerate(lines[start + 1:start + 70], start + 1):
                line = norm(collapse(raw))
                # Regional tables are consecutive. Bound each read to its own
                # heading so a row absent in one region cannot be borrowed from
                # the next (for example, US has no jug-volume row).
                if any(pattern.search(line) for pattern in headings.values()):
                    break
                for name, pattern in row_patterns:
                    if name in got or not pattern.match(line):
                        continue
                    vals = numbers(line, drop_pct=True)
                    # Mexico 2Q26 puts growth percentages on the Net Sales /
                    # EBITDA label line and peso values on the next line.
                    if name in ("sales", "ebitda") and (not vals or max(map(abs, vals)) < 1000):
                        for nxt in lines[idx + 1:idx + 4]:
                            candidate = numbers(norm(collapse(nxt)), drop_pct=True)
                            if len(candidate) >= 2 and max(map(abs, candidate[:2])) >= 1000:
                                vals = candidate
                                break
                    if len(vals) >= 2:
                        got[name] = (vals[0], vals[1])
                    break
            if {"sparkling", "water", "stills", "sales", "ebitda"} <= set(got):
                out[region] = got
                break
    return out


def _statement_rows(lines: list[str]) -> dict[str, tuple[float, float]] | None:
    """(current, prior) per component row, read inside the latched section.

    Tries each occurrence of the section heading; the first window that yields
    all four rows wins. Every row must carry at least two numeric columns
    (current quarter, prior-year quarter) — a wrapped label or a collapsed
    column leaves the set incomplete and the whole read is refused.
    """
    starts = [i for i, raw in enumerate(lines)
              if _SECTION_RE.search(norm(collapse(raw)))]
    for start in starts:
        got: dict[str, tuple[float, float]] = {}
        for raw in lines[start + 1:start + 1 + _WINDOW_LINES]:
            line = norm(collapse(raw))
            if not line:
                continue
            for name, row_re in _ROW_RES.items():
                if name in got:
                    continue
                m = row_re.match(line)
                if not m:
                    continue
                rest = line[m.end():].strip()
                if not NUMERIC_TAIL_RE.match(rest):
                    continue
                vals = numbers(rest)
                if len(vals) >= 2:
                    got[name] = (vals[0], vals[1])
                break
            if len(got) == len(_ROW_RES):
                return got
    return None

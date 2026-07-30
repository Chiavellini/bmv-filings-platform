"""Grupo México — deterministic extractor over the year-comparison P&L blocks.

Grupo México's Spanish BMV *reporte trimestral* prints, for the consolidated group
and for each division (Minera / Transportes / Infraestructura), a profit-and-loss
block headed ``(Miles de Dólares)  YYYY  YYYY  US$000  %`` — two value columns (the
two fiscal years), an absolute variance, and a % variance, repeated for the
standalone quarter and then the year-to-date (accumulated). We take the FIRST four
numeric columns of each row, i.e. the standalone quarter (this also drops the FY
columns in a Q4 report, which trail after the quarter). Figures print in USD
thousands → divide by 1000 for the model's US$-millions convention.

Column orientation flips by era: 2018 prints the PRIOR year first, 2020+ prints the
CURRENT year first. We orient each block by matching the header's two year tokens to
the report's own year; a Δ%-growth-consistency check is the fallback when the header
years are unreadable.

Each division block is anchored by the nearest preceding ``División X`` header; a
block with no division header above it is the consolidated group. Values are tagged
``[statement]`` so the config's ``tier_precedence`` can rank them (XBRL still wins
for the pure IFRS consolidated lines it covers from 2021-2T on; this extractor is the
only source for EBITDA and for every per-division line, in all eras).
"""
from __future__ import annotations

import re

from src.extract.extract_metrics import MetricRow
from src.extract.statement_utils import repair_split_digits
from src.extract.table_periods import (
    PeriodKey,
    current_column_in_header_line,
    orient_by_growth,
)


# Division section headers — appear a few lines above each division's P&L block.
_DIVISIONS = (
    ("minera", re.compile(r"Divisi[oó]n\s+Minera", re.I)),
    ("transportes", re.compile(r"Divisi[oó]n\s+Transporte", re.I)),
    ("infraestructura", re.compile(r"Divisi[oó]n\s+Infraestructura", re.I)),
)

# Year-comparison block header: "(Miles de Dólares)  2025  2024  US$000  %".
# The two 4-digit years disambiguate this P&L format from the standardized
# "Trimestre / Acumulado" statements (which carry no year columns).
_BLOCK_HDR = re.compile(r"\(?\s*Miles de D[oó]lares\s*\)?\s+(\d{4})\s+(\d{4})", re.I)

# Metric rows inside a block → (consolidated_key, division_stub). A None stub means
# the row is only captured at the consolidated level (divisions want net sales +
# EBITDA only, per the config's custom_metrics).
_ROWS = (
    (re.compile(r"^\s*Ventas\b(?!\s*\(\$?\s*US)", re.I), "revenue", "ns"),
    (re.compile(r"^\s*Costo de Ventas\b", re.I), "cogs", None),
    (re.compile(r"^\s*Utilidad de Operaci[oó]n\b", re.I), "operating_income", None),
    (re.compile(r"^\s*EBITDA\b", re.I), "ebitda", "ebitda"),
    (re.compile(r"^\s*Utilidad Neta\b", re.I), "net_income", None),
)

_NUM = re.compile(r"[\d,]+(?:\.\d+)?")
_THOUSANDS_TO_MILLIONS = 1.0 / 1000.0

# The standardized consolidated income statement ("ESTADO DE RESULTADOS", present in
# every report) prints Trimestre [current, prior, Δ] then Acumulado [current, prior,
# Δ] — always CURRENT-first. It is the universal source for the consolidated P&L in
# every era (including quarters and Q4 reports that carry no page-2 year-comparison
# table), and the only source for consolidated EBITDA (XBRL doesn't tag it).
# The consolidated statement is uniquely marked by the parent entity "(GM)" /
# "GRUPO MEXICO, S.A.B. DE C.V." — subsidiary statements (GMXT, MPD/Infra, …) carry
# their own tickers. Some reports (e.g. 2019) print only the subsidiary statements
# and no consolidated one; gating on this marker makes the parser correctly find
# nothing there (consolidated then comes from the page-2 table / XBRL / prose).
_GM_ENTITY = re.compile(r"\(GM\)|GRUPO M[EÉ]XICO,?\s+S\.?\s*A\.?\s*B\.?\s+DE\s+C\.?\s*V\.?", re.I)
_STMT_START = re.compile(r"ESTADO DE RESULTADOS", re.I)
_STMT_END = re.compile(r"BALANCE GENERAL|ESTADO DE (?:SITUACI|FLUJO)", re.I)
_STMT_ROWS = (
    (re.compile(r"^\s*Ventas netas\b", re.I), "revenue"),
    (re.compile(r"^\s*Costo de ventas\b", re.I), "cogs"),
    (re.compile(r"^\s*EBITDA\b", re.I), "ebitda"),
    (re.compile(r"^\s*Utilidad de operaci[oó]n\b", re.I), "operating_income"),
    # Majority (controlling-interest) net income — GMEXICO's headline (the page-2
    # table's "Utilidad Neta" is this majority line, not the group total).
    (re.compile(r"^\s*Utilidad Neta Controladora\b", re.I), "net_income"),
)


def _parse_period(period: str | None) -> tuple[int | None, int | None]:
    """Return (year, quarter) from either the download label ``2022-2T`` or the
    canonical scoring label ``2Q22A`` — the two forms the cascade passes in."""
    if not period:
        return None, None
    m = re.match(r"(\d{4})-([1-4])T", period)          # 2022-2T
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"([1-4])Q(\d{2})A?", period)          # 2Q22A
    if m:
        return 2000 + int(m.group(2)), int(m.group(1))
    m = re.search(r"(20\d{2})", period)
    return (int(m.group(1)), None) if m else (None, None)


def _tokens(line: str) -> list[float]:
    out: list[float] = []
    for tok in _NUM.findall(line):
        t = tok.replace(",", "").strip(".")
        if not t:
            continue
        try:
            out.append(float(t))
        except ValueError:
            pass
    return out


def extract_gmexico(text, metric_defs, period=None, pdf_path=None) -> dict[str, MetricRow]:
    defs = {m.key: m for m in metric_defs}
    report_year, report_q = _parse_period(period)
    target = PeriodKey(report_year, report_q) if (report_year and report_q) else None
    lines = text.splitlines()
    n = len(lines)
    found: dict[str, MetricRow] = {}

    def emit(key: str, cur: float, prior: float | None, seg: str, body: str) -> None:
        if key not in defs or key in found:
            return
        mdef = defs[key]
        found[key] = MetricRow(
            metric=key,
            label_es=mdef.label_es,
            current=cur * _THOUSANDS_TO_MILLIONS,
            prior=(prior * _THOUSANDS_TO_MILLIONS) if prior else None,
            var_pct=None,
            unit=mdef.unit,
            source_line=f"[statement] gmexico {seg} {body.strip()[:70]}",
        )

    # ── Pass 1 · consolidated P&L from the standardized income statement ──────
    # Only the parent "(GM)" statement is read — a subsidiary's ESTADO DE RESULTADOS
    # is skipped. Column order flips report-to-report, so orient the header row
    # (quarter tags "2T24 2T25" or plain years "2025 2026") via the shared
    # table_periods helper; default to current-first when it can't resolve.
    in_stmt = False
    section = None            # "is" (income statement) | "balance"
    stmt_idx = 0
    gm_seen_at = -99
    deuda_cp = deuda_lp = cash_val = None

    def _row_value(line: str, idx: int) -> float | None:
        toks = _tokens(repair_split_digits(line))
        return toks[idx] if len(toks) > idx else None

    for idx, line in enumerate(lines):
        if _GM_ENTITY.search(line):
            gm_seen_at = idx
        if not in_stmt:
            if _STMT_START.search(line) and (idx - gm_seen_at) <= 6:
                in_stmt, section = True, "is"
                stmt_idx = (current_column_in_header_line(line, target) if target else None) or 0
            continue
        # Cash-flow statement or a subsidiary's income statement ends the GM region.
        if re.search(r"ESTADO DE FLUJO|FLUJO DE EFECTIVO", line, re.I) or _STMT_START.search(line):
            in_stmt = False
            continue
        if _STMT_END.search(line):          # "BALANCE GENERAL"
            section = "balance"
            continue
        if section == "is":
            for rx, key in _STMT_ROWS:
                mm = rx.match(line)
                if not mm:
                    continue
                tail = line[mm.end():].lstrip()
                if not tail[:1].isdigit() and tail[:1] not in "($":
                    continue
                v = _row_value(line, stmt_idx)
                if v is not None:
                    emit(key, v, _row_value(line, 1 - stmt_idx), "consolidated", line)
                break
        elif section == "balance":
            if re.match(r"\s*Efectivo y valores equivalentes\b", line, re.I):
                cash_val = _row_value(line, stmt_idx) or cash_val
            elif re.match(r"\s*Deuda a corto plazo\b", line, re.I):
                deuda_cp = _row_value(line, stmt_idx)
            elif re.match(r"\s*Deuda a largo plazo\b", line, re.I):
                deuda_lp = _row_value(line, stmt_idx)

    # Consolidated cash + total/net debt from the balance section (all eras where the
    # "(GM)" statement is present; XBRL backstops 2021+).
    if cash_val is not None:
        emit("cash", cash_val, None, "consolidated", "Efectivo y valores equivalentes")
    if deuda_cp is not None or deuda_lp is not None:
        total_debt = (deuda_cp or 0.0) + (deuda_lp or 0.0)
        emit("total_debt", total_debt, None, "consolidated", "Deuda corto + largo plazo")
        if cash_val is not None:
            emit("net_debt", total_debt - cash_val, None, "consolidated", "total_debt − cash")

    def segment_for(idx: int) -> str:
        """Nearest division SECTION HEADER above this block. Only a short, header-like
        line counts (``División Minera`` on its own) — a prose mention (``La División
        Infraestructura obtuvo…``) or a multi-division chart label is ignored, so a
        consolidated page-2 table near such text isn't mislabeled."""
        seen = 0
        for j in range(idx - 1, max(-1, idx - 14), -1):
            s = lines[j].strip()
            if not s:
                continue
            if len(s) <= 44:
                for seg, rx in _DIVISIONS:
                    m = rx.match(s)  # anchored at line start
                    if m and not any(o[1].search(s[m.end():]) for o in _DIVISIONS):
                        return seg
            seen += 1
            if seen >= 8:
                break
        return "consolidated"

    i = 0
    while i < n:
        hdr = _BLOCK_HDR.search(lines[i])
        if not hdr:
            i += 1
            continue
        y1, y2 = int(hdr.group(1)), int(hdr.group(2))
        seg = segment_for(i)
        current_idx: int | None = None
        if target is not None:
            current_idx = current_column_in_header_line(f"{y1} {y2}", target)
        elif report_year is not None and report_year in (y1, y2):
            current_idx = 0 if y1 == report_year else 1

        for k in range(i + 1, min(n, i + 14)):
            body = lines[k]
            if _BLOCK_HDR.search(body):
                break
            for rx, ckey, dstub in _ROWS:
                m = rx.match(body)
                if not m:
                    continue
                # Table row, not prose: the label is directly followed by a number.
                tail = body[m.end():].lstrip()
                if not tail[:1].isdigit() and tail[:1] not in "($":
                    continue
                toks = _tokens(repair_split_digits(body))
                if len(toks) < 2:
                    continue
                a, b = toks[0], toks[1]
                dpct = toks[3] if len(toks) >= 4 else (toks[2] if len(toks) >= 3 else None)
                if current_idx is not None:
                    cur, prior = (a, b)[current_idx], (a, b)[1 - current_idx]
                else:
                    cur, prior = orient_by_growth(a, b, dpct)

                key = ckey if seg == "consolidated" else (f"{dstub}_{seg}" if dstub else None)
                if not key:
                    break
                emit(key, cur, prior, seg, body)
                break
        i += 1

    return found

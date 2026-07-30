#!/usr/bin/env python3
"""bmv_statements.py — generic extraction from CNBV/BMV standardized statements.

Every Mexican BMV filer publishes its quarterly report with the same regulatory
financial-statement blocks, tagged by a six-digit CNBV taxonomy code:

    [210000]  Estado de situación financiera   (balance sheet, INSTANT columns)
    [310000]  Estado de resultados             (income statement, DURATION columns)

The labels in these blocks are taxonomy-fixed ("Ingresos", "Costo de ventas",
"Total de activos", …) — identical across every filer and across time (verified
on Liverpool 2017 and 2024) — and the values are printed in FULL PESOS with
explicit per-column date headers. That makes a *config-free* extractor possible:

  1. slice the document into [NNNNNN] blocks,
  2. read the per-column date headers to learn each column's period,
  3. map each standardized row label to a canonical metric by exact (normalized)
     match — precise enough to avoid "Ingresos financieros" → revenue slips,
  4. pick the single-quarter column and scale full pesos → the company unit.

YTD safety (the correctness core): income columns are "Acumulado" (year-to-date).
For Q1 the YTD column *is* the quarter (its date range starts and ends in Q1). For
Q2-Q4 the single-quarter value lives in a separate "Trimestre" column when the
filing provides one; otherwise the block is YTD-only and this tier emits NOTHING
for income/cash-flow metrics — an honest gap the press-release tiers fill. This
mirrors xbrl_facts._pick_entry, which rejects YTD durations for the same reason.

This runs as Tier 1.5 in the cascade (after authoritative XBRL, before the regex
engine); it gap-fills, so XBRL always wins and the pre-2021 PDF-only era — where
XBRL is absent — is where it contributes most.
"""

from __future__ import annotations

import re
from collections import Counter

from src.model.financial_model import MetricDef
from src.extract.extract_metrics import MetricRow, parse_number
from src.extract.semantic_search import normalize_label
from src.extract.table_periods import normalize_target_period
from src.extract.xbrl_facts import pesos_per_unit_for


# Standardized CNBV statement labels (normalized via semantic_search.normalize_label)
# → canonical metric key. Exact-match keeps this precise: only the headline rows
# map, never their sub-lines (e.g. "Ingresos financieros", "Otros ingresos",
# "Utilidad (pérdida) de operaciones continuas").
_CANON_LABELS = {
    # [310000] income statement, por función de gasto
    "ingresos": "revenue",
    "costo de ventas": "cogs",
    "utilidad bruta": "gross_profit",
    "utilidad perdida de operacion": "operating_income",
    "utilidad de operacion": "operating_income",
    "utilidad perdida neta": "net_income",
    "utilidad neta": "net_income",
    # [210000] balance sheet
    "total de activos": "total_assets",
    "total de pasivos": "total_liabilities",
    "total pasivos": "total_liabilities",
    "total de capital contable": "equity",
    "efectivo y equivalentes de efectivo": "cash",
}

# Metric units whose full-peso value must be divided by pesos-per-unit.
_MONETARY_UNITS = {"currency", "miles_mxn"}

# Whitelisted block codes. Other blocks (e.g. [700003] "12 meses" trailing-twelve-
# month figures, [800xxx] annexes) carry look-alike rows at different magnitudes
# and must NOT be read.
_BALANCE_CODES = {"210000"}
_INCOME_CODES = {"310000"}

_BLOCK_RE = re.compile(r"^\s*\[(\d{6})\]", re.MULTILINE)
# A numeric value token anchored at end of line: optional parens/sign/$, digit
# groups with commas and optional decimals. Used to peel trailing cells off a row.
_VALUE_TAIL_RE = re.compile(r"\(?\s*-?\s*\$?\s*\d[\d,]*(?:\.\d+)?\s*\)?\s*$")
_VALUE_TOKEN_RE = re.compile(r"^\(?\s*-?\s*\$?\s*\d[\d,]*(?:\.\d+)?\s*\)?$")
# Most columns any block we read should have (Acumulado×2 + Trimestre×2).
_MAX_COLS = 8


def _split_blocks(text: str) -> list[tuple[str, str]]:
    """Return [(code, body), …] where body is the text until the next [NNNNNN]."""
    matches = list(_BLOCK_RE.finditer(text))
    blocks: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        code = m.group(1)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append((code, text[m.start():end]))
    return blocks


def _header_region(body: str) -> str:
    """The column-header text: lines from the block start up to the first data row.

    A data row ends in ≥2 value tokens (e.g. "Ingresos 41,220 37,569"); the
    multi-line column headers and the date rows come before it. Stopping there
    keeps mid-statement page furniture ("Clave de Cotización … Trimestre: 3") out
    of the header, so the 'Trimestre' column test below isn't fooled by it.
    """
    out: list[str] = []
    for line in body.splitlines():
        if len(_label_and_values(line, _MAX_COLS)[1]) >= 2:
            break
        out.append(line)
    return normalize_label(" ".join(out))


def _value_columns(body: str) -> int:
    """Number of value columns = the most common trailing-number count over rows."""
    counts: Counter[int] = Counter()
    for line in body.splitlines():
        n = len(_label_and_values(line, _MAX_COLS)[1])
        if n >= 2:
            counts[n] += 1
    if not counts:
        return 0
    return max(counts, key=lambda c: (counts[c], c))


def _select(body: str, ncols: int, kind: str, target) -> tuple[int | None, int | None]:
    """Pick (current_idx, prior_idx) among the ncols value columns.

    Balance ([210000]) is instants: column 0 = Cierre Actual, column 1 = prior.
    Income ([310000]) columns run Acumulado(actual, anterior) then, when present,
    Trimestre(actual, anterior). The single-quarter value is:
      • the Trimestre actual column (second-to-last) when the filing prints one —
        valid for every quarter; or
      • the Acumulado actual column ONLY in Q1, where YTD == the quarter.
    Otherwise the block is YTD-only → return (None, None) so it's skipped (an
    honest gap, never a YTD value passed off as the quarter).
    """
    if kind == "balance":
        return (0, 1 if ncols >= 2 else None)
    has_trimestre = "trimestre" in _header_region(body)
    if has_trimestre and ncols >= 4:
        return (ncols - 2, ncols - 1)       # Trimestre actual, Trimestre anterior
    if ncols == 2 and target.quarter == 1:
        return (0, 1)                        # Acumulado == the quarter in Q1
    return (None, None)


def _label_and_values(line: str, ncols: int) -> tuple[str, list[str]]:
    """Peel up to ``ncols`` trailing value tokens off a row; return (label, values).

    Capping at ncols protects any digits embedded in the label (the real value
    columns are the rightmost ncols tokens).
    """
    parts = line.rstrip().split()
    if not parts:
        return "", []
    end = len(parts)
    values: list[str] = []
    while len(values) < ncols and end > 0:
        token = parts[end - 1]
        if not _VALUE_TOKEN_RE.match(token):
            break
        values.append(token)
        end -= 1
    values.reverse()
    return " ".join(parts[:end]), values


def extract_from_statements(
    text: str,
    metric_defs: list[MetricDef],
    cfg: dict | None,
    period: str | None,
) -> dict[str, MetricRow]:
    """Tier 1.5: return {metric_key: MetricRow} from CNBV statement blocks.

    No per-company config is required; ``cfg`` is consulted only for the reporting
    unit (``company.unit``) used to scale full pesos. Returns an empty dict when
    the period can't be parsed or no whitelisted block yields a single-quarter
    column.
    """
    target = normalize_target_period(period)
    if target is None:
        return {}

    by_key = {m.key: m for m in metric_defs}
    ppu = pesos_per_unit_for(cfg)
    found: dict[str, MetricRow] = {}

    for code, body in _split_blocks(text):
        if code in _BALANCE_CODES:
            kind = "balance"
        elif code in _INCOME_CODES:
            kind = "income"
        else:
            continue

        ncols = _value_columns(body)
        if ncols < 2:
            continue
        cur_idx, prior_idx = _select(body, ncols, kind, target)
        if cur_idx is None:
            continue

        for line in body.splitlines():
            label, values = _label_and_values(line, ncols)
            if len(values) != ncols:
                continue
            key = _CANON_LABELS.get(normalize_label(label))
            if key is None or key in found or key not in by_key:
                continue
            cur = parse_number(values[cur_idx])
            if cur is None:
                continue
            prior = parse_number(values[prior_idx]) if prior_idx is not None else None

            mdef = by_key[key]
            if mdef.unit in _MONETARY_UNITS:
                cur /= ppu
                prior = prior / ppu if prior is not None else None
            var_pct = (
                round((cur - prior) / abs(prior) * 100, 2)
                if prior not in (None, 0)
                else None
            )
            found[key] = MetricRow(
                metric=key,
                label_es=mdef.label_es,
                current=cur,
                prior=prior,
                var_pct=var_pct,
                unit=mdef.unit,
                source_line=f"[bmv] {code} {label.strip()}"[:120],
            )

    return found

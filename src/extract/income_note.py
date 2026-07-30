#!/usr/bin/env python3
"""income_note.py — header-aligned extraction of the CNBV `[800200]` income note.

Mexican BMV filers publish a detailed income note, ``[800200] Notas - Análisis de
ingresos y gastos``, that breaks revenue into types (``Venta de bienes`` →
commercial, ``Intereses`` → financial, ``Arrendamiento`` → real estate, plus the
``Total de ingresos`` line). These segment rows are the *only* place several
companies (Liverpool, Chedraui, …) disclose the split, so configs have been
grabbing them with brittle PER-COMPANY POSITIONAL regexes ("capture the 3rd
number"). That breaks because the note's column ORDER varies by year/quarter:

    2024-3T:  [Acum-cur, Acum-prior, Trim-cur (3rd), Trim-prior]   → "3rd" correct
    2025-3T:  [Trim-cur (1st), Acum-cur, Trim-prior, Acum-prior]   → "3rd" = PRIOR year (wrong!)
    Q1:       [Acum-cur, Acum-prior]                                → Q1 acumulado == the quarter

This tier reads the column HEADER instead of a fixed position and selects the
single-quarter ("Trimestre … Actual") column whose quarter matches the target —
the same correctness rule as ``bmv_statements`` / ``xbrl_facts``, generalized so
the column order no longer matters. It REUSES the block-slicing and row-tokenizing
helpers from ``bmv_statements`` and the period parsing from ``table_periods``.

The load-bearing signal is the **type-keyword order** on the header line
(``Trimestre``/``Acumulado``, in column order, exactly N for an N-column table)
plus the BMV convention that within each type the **Actual** column precedes the
**Anterior** column. The OCR-fragmented date ranges are used only as a best-effort
sanity check (target year must appear), never for positional mapping.

No-regression contract: whenever the header can't be confidently aligned — header
not parseable, type-word count ≠ column count, target year absent, the
``Total de ingresos`` magnitude guard fails, or there is no single-quarter column
(a YTD-only filing) — this returns ``{}`` so the caller's positional-regex
fallback keeps today's behavior. It only ever ADDS a confident, header-aligned
value; it never guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.model.financial_model import MetricDef
from src.extract.extract_metrics import MetricRow, parse_number
from src.extract.semantic_search import normalize_label
from src.extract.table_periods import normalize_target_period, PeriodKey
from src.extract.xbrl_facts import pesos_per_unit_for
from src.extract.bmv_statements import (
    _split_blocks,
    _value_columns,
    _label_and_values,
    _MAX_COLS,
)

# Metric units whose full-peso value must be divided by pesos-per-unit.
_MONETARY_UNITS = {"currency", "miles_mxn"}

# The headline revenue line; used for the magnitude guard and as a default code map.
_TOTAL_LABEL = "total de ingresos"

# Column-type words, normalized. Their COUNT on the header line must equal the
# number of value columns, and their ORDER is the column order.
_TYPE_WORDS = ("trimestre", "acumulado")

# A standalone 4-digit year (1900-2099), used to confirm the note is for the target
# period even when the OCR-split date ranges can't be fully reconstructed.
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


@dataclass(frozen=True)
class _ColSpec:
    """One reconstructed column of the note's value grid."""

    idx: int
    is_trim: bool      # True = Trimestre (single quarter), False = Acumulado (YTD)
    is_actual: bool    # True = current year, False = prior year (Anterior)
    year: int
    quarter: int
    is_ytd: bool       # Acumulado spanning >1 quarter (Q2-Q4); never single-quarter


def _row_map(body: str, ncols: int) -> dict[str, list[str]]:
    """Map each data row's normalized label → its ``ncols`` value strings.

    Only rows that yield exactly ``ncols`` values are kept (the real grid rows);
    section headers like ``Ingresos [sinopsis]`` carry no values and drop out.
    First occurrence of a label wins.
    """
    rows: dict[str, list[str]] = {}
    for line in body.splitlines():
        label, values = _label_and_values(line, ncols)
        if len(values) != ncols:
            continue
        key = normalize_label(label)
        if key and key not in rows:
            rows[key] = values
    return rows


def _header_lines(body: str) -> list[str]:
    """Raw header lines: from the block start up to the first ≥2-value data row."""
    out: list[str] = []
    for line in body.splitlines():
        if len(_label_and_values(line, _MAX_COLS)[1]) >= 2:
            break
        out.append(line)
    return out


def _type_sequence(header_lines: list[str], ncols: int) -> list[str] | None:
    """The column-type words (``trimestre``/``acumulado``) in column order.

    Returns the list from the FIRST header line that carries exactly ``ncols`` of
    them, or None when no single line does (wrapped/garbled header → degrade).
    """
    for line in header_lines:
        toks = [w for w in normalize_label(line).split() if w in _TYPE_WORDS]
        if len(toks) == ncols:
            return toks
    return None


def _build_columns(type_seq: list[str], target: PeriodKey) -> list[_ColSpec]:
    """Reconstruct each column from type order + Actual-before-Anterior invariant."""
    seen = {"trimestre": 0, "acumulado": 0}
    cols: list[_ColSpec] = []
    for i, t in enumerate(type_seq):
        is_actual = seen[t] == 0
        seen[t] += 1
        year = target.year if is_actual else target.year - 1
        if t == "trimestre":
            cols.append(_ColSpec(i, True, is_actual, year, target.quarter, is_ytd=False))
        else:  # acumulado: in Q1 the YTD column IS the quarter (single-quarter span)
            cols.append(_ColSpec(i, False, is_actual, year, target.quarter,
                                 is_ytd=(target.quarter != 1)))
    return cols


def _current_col(cols: list[_ColSpec], target: PeriodKey) -> _ColSpec | None:
    """The single-quarter Actual column for the target (Trimestre, or Q1 Acumulado)."""
    for c in cols:
        if c.is_actual and not c.is_ytd and c.year == target.year and c.quarter == target.quarter:
            return c
    return None


def _prior_col(cols: list[_ColSpec], current: _ColSpec, target: PeriodKey) -> _ColSpec | None:
    """The matching prior-year single-quarter column (same is_trim as ``current``)."""
    for c in cols:
        if (not c.is_actual and c.is_trim == current.is_trim and not c.is_ytd
                and c.year == target.year - 1 and c.quarter == target.quarter):
            return c
    return None


def _acum_actual_col(cols: list[_ColSpec]) -> _ColSpec | None:
    for c in cols:
        if not c.is_trim and c.is_actual:
            return c
    return None


def _passes_magnitude_guard(rows: dict[str, list[str]], cols: list[_ColSpec],
                            current: _ColSpec, target: PeriodKey) -> bool:
    """For Q2-Q4: the chosen quarter must be 0 < quarter < YTD on Total de ingresos.

    Catches a gross mis-selection (e.g. picking an Acumulado column as the quarter,
    or an Actual/Anterior swap that lands on the YTD column). Q1 has no separate YTD
    column, so the guard is a no-op there. Degrades (skips) if the total row or the
    Acumulado-Actual column is unavailable to check against — never emits unchecked.
    """
    if target.quarter == 1:
        return True
    acum = _acum_actual_col(cols)
    tot = rows.get(_TOTAL_LABEL)
    if acum is None or tot is None:
        return False
    cur_v = parse_number(tot[current.idx])
    acum_v = parse_number(tot[acum.idx])
    return bool(cur_v is not None and acum_v is not None and 0 < cur_v < acum_v)


def _summed(rows: dict[str, list[str]], labels: list[str], idx: int) -> float | None:
    """Sum the values at column ``idx`` across ``labels``; None if any is missing.

    A configured key may map to one label (a note row) or several (an operating
    segment built from revenue types, e.g. commercial = goods + services). Requiring
    every label present keeps a partial sum from silently undercounting.
    """
    total = 0.0
    for lbl in labels:
        cells = rows.get(lbl)
        if cells is None:
            return None
        v = parse_number(cells[idx])
        if v is None:
            return None
        total += v
    return total


def _find_block(text: str, code: str, want_labels: set[str]) -> str | None:
    """Body of the income-note block ``code`` that carries the real value grid.

    Skips the table-of-contents entry (no value columns) and requires ``Concepto``
    plus at least two of the configured row labels, so we never read the wrong note.
    """
    best: str | None = None
    best_hits = 1  # require ≥2 mapped labels present
    for c, body in _split_blocks(text):
        if c != code:
            continue
        ncols = _value_columns(body)
        if ncols < 2:
            continue
        header = " ".join(_header_lines(body))
        if "concepto" not in normalize_label(header):
            continue
        rows = _row_map(body, ncols)
        hits = len(want_labels & rows.keys())
        if hits > best_hits:
            best, best_hits = body, hits
    return best


def _extract_block(body: str, key_to_labels: dict[str, list[str]], by_key: dict[str, MetricDef],
                   ppu: float, target: PeriodKey, code: str) -> dict[str, MetricRow]:
    """Header-align one note block and emit the configured keys, or {} to degrade."""
    ncols = _value_columns(body)
    if ncols < 2:
        return {}
    type_seq = _type_sequence(_header_lines(body), ncols)
    if type_seq is None:
        return {}
    cols = _build_columns(type_seq, target)
    current = _current_col(cols, target)
    if current is None:                       # YTD-only filing → honest gap
        return {}

    # Best-effort sanity: the target year must appear in the header date region.
    header_years = {int(y) for y in _YEAR_RE.findall(" ".join(_header_lines(body)))}
    if header_years and target.year not in header_years:
        return {}

    rows = _row_map(body, ncols)
    if not _passes_magnitude_guard(rows, cols, current, target):
        return {}

    prior = _prior_col(cols, current, target)
    found: dict[str, MetricRow] = {}
    for key, labels in key_to_labels.items():
        if key not in by_key:
            continue
        cur = _summed(rows, labels, current.idx)
        if cur is None:
            continue
        prior_val = _summed(rows, labels, prior.idx) if prior is not None else None
        mdef = by_key[key]
        if mdef.unit in _MONETARY_UNITS:
            cur /= ppu
            prior_val = prior_val / ppu if prior_val is not None else None
        var_pct = (round((cur - prior_val) / abs(prior_val) * 100, 2)
                   if prior_val not in (None, 0) else None)
        found[key] = MetricRow(
            metric=key,
            label_es=mdef.label_es,
            current=cur,
            prior=prior_val,
            var_pct=var_pct,
            unit=mdef.unit,
            source_line=f"[note] {code} {'+'.join(labels)}"[:120],
        )
    return found


def extract_from_income_note(
    text: str,
    metric_defs: list[MetricDef],
    cfg: dict | None,
    period: str | None,
) -> dict[str, MetricRow]:
    """Tier 'note': segment revenue from the ``[800200]`` note by header alignment.

    Config (opt-in). Each key maps to one note label or a LIST summed together —
    an operating segment is often the sum of revenue types (commercial = goods +
    services)::

        income_note:
          enabled: true
          code: "800200"              # default
          fallback_codes: ["310000"]  # tried, in order, if the note is absent
          rows:
            revenue_commercial:  ["Venta de bienes", "Servicios"]
            revenue_financial:   "Intereses"
            revenue_real_estate: "Arrendamiento"
            revenue:             "Total de ingresos"

    Returns ``{metric_key: MetricRow}`` (source tag ``[note]``), or ``{}`` whenever
    the header can't be confidently aligned so the caller falls back to its
    positional regex. Never emits a value selected by fixed position.
    """
    spec = (cfg or {}).get("income_note") or {}
    if not spec.get("enabled") or not spec.get("rows"):
        return {}
    target = normalize_target_period(period)
    if target is None:
        return {}

    key_to_labels: dict[str, list[str]] = {}
    for key, labels in spec["rows"].items():
        if isinstance(labels, str):
            labels = [labels]
        key_to_labels[key] = [normalize_label(lbl) for lbl in labels]
    want_labels = {lbl for labels in key_to_labels.values() for lbl in labels}
    by_key = {m.key: m for m in metric_defs}
    ppu = pesos_per_unit_for(cfg)

    codes = [str(spec.get("code") or "800200")]
    codes += [str(c) for c in (spec.get("fallback_codes") or [])]
    for code in codes:
        body = _find_block(text, code, want_labels)
        if body is None:
            continue
        found = _extract_block(body, key_to_labels, by_key, ppu, target, code)
        if found:
            return found
    return {}

#!/usr/bin/env python3
"""
xbrl_facts.py — Tier 1 extraction: direct IFRS concept lookup from XBRL facts.

For 2021+ BMV filings, bmv_xbrl.extract_artifacts() already writes a flat
``<stem>_facts.json`` mapping each tagged IFRS concept to its numeric facts.
This module reads that structure and returns the same ``MetricRow`` objects the
regex engine produces — so a metric answered here is authoritative (≈100%
confidence) and needs no pattern matching.

The metric_key → IFRS-concept map and the unit scaling live here; the map itself
is data (configs/xbrl_concepts.yaml, attached to each MetricDef.xbrl_concepts by
financial_model.apply_config).

Key facts about the input (facts = ``json["facts"]`` from a ``*_facts.json``):

    {"ifrs-full_Revenue": [
        {"value": 589053000.0, "period_start": "2026-01-01",
         "period_end": "2026-03-31", "instant": null,
         "unit": "ISO4217:MXN", "decimals": "-3", "dimensions": null}
    ], ...}

Raw IFRS monetary values are in PESOS. The rest of the project works in
*miles de pesos* (thousands), so monetary metrics are divided by 1000.
"""

from __future__ import annotations

from datetime import date

from src.model.financial_model import MetricDef

# Units whose raw IFRS value (pesos) must be divided by 1000 → miles de pesos.
MONETARY_UNITS = {"currency", "miles_mxn"}
PESOS_PER_UNIT = 1000.0

# A quarterly flow spans ~one calendar quarter; YTD spans are longer. Used to
# distinguish the single-quarter fact from the year-to-date fact.
_QUARTER_DAYS = 92
_QUARTER_MAX_DAYS = 100   # accept up to ~3 months; rejects half-year/YTD spans


def _parse_iso(d) -> date | None:
    if not isinstance(d, str) or len(d) < 10:
        return None
    try:
        return date.fromisoformat(d[:10])
    except ValueError:
        return None


def _span_days(entry: dict) -> int | None:
    start = _parse_iso(entry.get("period_start"))
    end = _parse_iso(entry.get("period_end"))
    if start is None or end is None:
        return None
    return (end - start).days


def _has_dimensions(entry: dict) -> bool:
    """True for segment/dimensional breakdowns — we want consolidated totals only."""
    dims = entry.get("dimensions")
    if dims is None:
        return False
    if isinstance(dims, (list, dict)):
        return len(dims) > 0
    return bool(dims)


def _is_instant(entry: dict) -> bool:
    return entry.get("instant") is not None


def _pick_entry(entries: list[dict], period_end: str | None, section: str) -> dict | None:
    """Choose the fact for the current reporting period.

    Balance-sheet metrics are instants (a snapshot at period_end); income and
    cash-flow metrics are durations (prefer the single quarter, not YTD).
    Dimensional (segment) facts are excluded — we want the consolidated total.
    """
    candidates = [e for e in entries if not _has_dimensions(e)]
    if not candidates:
        candidates = list(entries)   # fall back rather than return nothing

    target_end = _parse_iso(period_end) if period_end else None
    prefer_instant = section == "balance"

    instants = [e for e in candidates if _is_instant(e)]
    durations = [e for e in candidates if not _is_instant(e)]

    def best_instant() -> dict | None:
        if not instants:
            return None
        if target_end is not None:
            exact = [e for e in instants if _parse_iso(e.get("instant")) == target_end]
            if exact:
                return exact[0]
        return max(instants, key=lambda e: (_parse_iso(e.get("instant")) or date.min))

    def best_duration() -> dict | None:
        if not durations:
            return None
        pool = durations
        if target_end is not None:
            exact = [e for e in durations if _parse_iso(e.get("period_end")) == target_end]
            if exact:
                pool = exact
        else:
            latest = max(
                (_parse_iso(e.get("period_end")) or date.min) for e in durations
            )
            pool = [e for e in durations
                    if (_parse_iso(e.get("period_end")) or date.min) == latest]
        # Among same-period-end candidates, the quarter is the SHORTEST span
        # (YTD spans are longer). A flow metric is reported per quarter, so a
        # YTD-only fact (e.g. BMV files income as estado de resultados acumulado)
        # is the WRONG number for Q2–Q4 — reject it rather than fall back, so the
        # cascade can fill the quarter from the press-release table instead.
        # (Q1 YTD == the quarter, ~90 days, so it stays within the window.)
        quarterly = [e for e in pool if (_span_days(e) or 0) <= _QUARTER_MAX_DAYS]
        if not quarterly:
            return None
        return min(quarterly, key=lambda e: _span_days(e) or 10**6)

    order = (best_instant, best_duration) if prefer_instant else (best_duration, best_instant)
    for getter in order:
        entry = getter()
        if entry is not None:
            return entry
    return None


def _pick_prior(entries: list[dict], current: dict, section: str) -> dict | None:
    """Same period one year earlier, matching the current fact's temporal type."""
    cur_instant = _is_instant(current)
    if cur_instant:
        cur_end = _parse_iso(current.get("instant"))
    else:
        cur_end = _parse_iso(current.get("period_end"))
    if cur_end is None:
        return None
    try:
        target = cur_end.replace(year=cur_end.year - 1)
    except ValueError:               # Feb 29 → Feb 28
        target = cur_end.replace(year=cur_end.year - 1, day=28)

    cur_span = _span_days(current)
    best = None
    best_gap = 45                    # within ~6 weeks of the year-ago date
    for e in entries:
        if _has_dimensions(e) or _is_instant(e) != cur_instant:
            continue
        end = _parse_iso(e.get("instant") if cur_instant else e.get("period_end"))
        if end is None:
            continue
        gap = abs((end - target).days)
        if gap > best_gap:
            continue
        # match span class for durations (quarter vs YTD)
        if not cur_instant and cur_span is not None:
            if abs((_span_days(e) or 0) - cur_span) > 20:
                continue
        best, best_gap = e, gap
    return best


def _scale(entry: dict, unit: str, pesos_per_unit: float = PESOS_PER_UNIT) -> float:
    """Raw IFRS value (full pesos) → project units for monetary metrics.

    ``pesos_per_unit`` is how many pesos one project unit represents: 1e3 for a
    company reporting in miles (thousands), 1e6 for one reporting in millions.
    """
    value = float(entry["value"])
    if unit in MONETARY_UNITS:
        value = value / pesos_per_unit
    # decimals is a precision hint; the value is already exact, so just clean
    # float noise rather than rescaling.
    return round(value, 4)


# Map a company config's ``company.unit`` to pesos-per-unit for XBRL scaling.
_UNIT_PESOS = {
    "millions": 1e6, "millones": 1e6, "mdp": 1e6,
    "miles_mxn": 1e3, "miles": 1e3, "thousands": 1e3,
}


def pesos_per_unit_for(cfg: dict | None) -> float:
    """Pesos represented by one project unit, from a company config (default miles)."""
    unit = ((cfg or {}).get("company") or {}).get("unit", "")
    return _UNIT_PESOS.get(str(unit).lower(), PESOS_PER_UNIT)


def extract_from_xbrl(
    facts: dict,
    metric_defs: list[MetricDef],
    period_end: str | None = None,
    pesos_per_unit: float = PESOS_PER_UNIT,
) -> dict:
    """Tier 1: return {metric_key: MetricRow} for every IFRS-mappable metric present.

    Args:
        facts:       the ``"facts"`` dict from a ``*_facts.json`` artifact.
        metric_defs: metric definitions; only those with ``xbrl_concepts`` apply.
        period_end:  ISO date of the quarter end (e.g. "2026-03-31") to anchor
                     context selection. If None, the latest period is used.
        pesos_per_unit: pesos per project unit (1e6 for "millions", 1e3 for miles).
    """
    from src.extract.extract_metrics import MetricRow   # local import avoids a cycle

    found: dict = {}
    if not facts:
        return found

    for mdef in metric_defs:
        if not mdef.xbrl_concepts or mdef.key in found:
            continue
        for concept in mdef.xbrl_concepts:
            entries = facts.get(concept)
            if not entries:
                continue
            current = _pick_entry(entries, period_end, mdef.section)
            if current is None:
                continue
            cur_val = _scale(current, mdef.unit, pesos_per_unit)
            prior_entry = _pick_prior(entries, current, mdef.section)
            prior_val = _scale(prior_entry, mdef.unit, pesos_per_unit) if prior_entry else None
            var_pct = None
            if prior_val not in (None, 0):
                var_pct = round((cur_val - prior_val) / abs(prior_val) * 100, 2)

            anchor = current.get("period_end") or current.get("instant") or ""
            found[mdef.key] = MetricRow(
                metric=mdef.key,
                label_es=mdef.label_es,
                current=cur_val,
                prior=prior_val,
                var_pct=var_pct,
                unit=mdef.unit,
                source_line=f"[xbrl] {concept} {anchor}".strip(),
            )
            break

    return found

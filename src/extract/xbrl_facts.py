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


# Approximate USDMXN — used ONLY by the convert-to-MXN path (soft coverage), where
# USD reporters' facts are converted so peso multiples line up with the MXN price.
# Env-overridable (SOFT_USDMXN) so it can be kept current. (Back-ported from the
# soft fork during the 2026-07 reconcile.)
def _usdmxn_default() -> float:
    import os
    raw = os.environ.get("SOFT_USDMXN")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 18.7


_USDMXN = _usdmxn_default()


def detect_reporting_currency(facts: dict) -> str | None:
    """Dominant ISO4217 currency across monetary facts, e.g. 'USD' / 'MXN'. None if undetectable.
    Uses the mode (not first hit): some MXN filers carry ISO4217:USD on FX-note line items, so a
    single-entry check would misfire. This is authoritative (straight from the filing), unlike
    ``company.currency`` in config, which is frequently wrong for USD reporters."""
    from collections import Counter
    c: Counter = Counter()
    for entries in (facts or {}).values():
        for e in entries:
            u = (e.get("unit") or "")
            if u.startswith("ISO4217:") and isinstance(e.get("value"), (int, float)):
                c[u.split(":", 1)[1].upper()] += 1
    return c.most_common(1)[0][0] if c else None


def pesos_per_unit_for(cfg: dict | None, detected_currency: str | None = None) -> float:
    """Pesos represented by one project unit, from a company config (default miles).

    ``detected_currency`` (from :func:`detect_reporting_currency`) opts INTO the
    USD→MXN conversion used by the coverage path: a USD fact must become a LARGER
    peso figure (×USDMXN), so the unit is divided by the rate. When it is None —
    every root/deliverable caller — the unit is returned unconverted, preserving
    the certified native-currency behavior (orbia/gmexico report USD as printed,
    guarded by the ``expected_currency`` filter instead).
    """
    unit = ((cfg or {}).get("company") or {}).get("unit", "")
    base = _UNIT_PESOS.get(str(unit).lower(), PESOS_PER_UNIT)
    if detected_currency and str(detected_currency).upper() == "USD":
        return base / _USDMXN
    return base


# Large balance-sheet totals used to sense a filing's reporting magnitude. A full-peso BMV filer's
# largest total is >> 1e7 (assets/revenue run 1e9–1e11); a filing tagged already-in-millions is ~1e6×
# smaller (Pena Verde FY2025: assets 48,054 / revenue 25,230). The gap is enormous, so the threshold
# sits far from both regimes and never misfires on a real full-peso (or thousands-scale) filer.
_SCALE_REF_CONCEPTS = ("ifrs-full_Assets", "ifrs-full_Equity", "ifrs-full_Liabilities",
                       "ifrs-full_Revenue")
_MILLIONS_MAX_TOTAL = 1e7


def _facts_in_millions(facts: dict | None) -> bool:
    """True when a filing's monetary facts are ALREADY denominated in millions (not full pesos).
    Some issuers file the XBRL in millions for a single year (Pena Verde FY2025) while the rest of
    their history is in full pesos, so the scale must be sensed per filing rather than per company."""
    mags = [abs(e["value"]) for c in _SCALE_REF_CONCEPTS for e in (facts or {}).get(c, [])
            if isinstance(e.get("value"), (int, float)) and e["value"] and not e.get("dimensions")]
    return bool(mags) and max(mags) < _MILLIONS_MAX_TOTAL


def pesos_per_unit_for_facts(cfg: dict | None, facts: dict | None) -> float:
    """Per-filing pesos-per-unit for the CONVERT-to-MXN coverage path: config unit +
    facts-detected currency (USD converts via USDMXN) + a magnitude guard. When a
    full-peso config (base ≥ 1e6) meets a filing whose totals are already in
    millions, downshift the unit ×1e6 so the value isn't divided by a million again
    (which would land an equity of 6,962mn at 0.007). Only downshifts a
    millions-config filer; thousands/USD scaling and every normal filing are
    untouched."""
    base = pesos_per_unit_for(cfg, detect_reporting_currency(facts))
    if base >= 1e6 and _facts_in_millions(facts):
        return base / 1e6
    return base


# Metrics whose mapped concept list can contain a present-but-0.0 line that shadows the real value in
# a later concept → prefer the first non-zero candidate (see the loop in extract_from_xbrl).
_PREFER_NONZERO = {"depreciation"}


def iso_currency_for(cfg: dict | None) -> str | None:
    """ISO-4217 code from ``company.currency`` (None when unset → no filtering)."""
    currency = ((cfg or {}).get("company") or {}).get("currency")
    return str(currency).upper() if currency else None


def _currency_ok(entry: dict, expected: str | None) -> bool:
    """Reject monetary facts filed in a different functional currency.

    USD reporters (GMEXICO, ORBIA) tag facts ``ISO4217:USD``; mixing an MXN-
    tagged fact into that series (or vice versa) is a silent unit error. Facts
    with non-monetary units (``xbrli:pure``, shares) pass through untouched.
    """
    if not expected:
        return True
    unit = str(entry.get("unit") or "")
    if "ISO4217" not in unit.upper():
        return True
    return unit.upper().rsplit(":", 1)[-1] == expected


def extract_from_xbrl(
    facts: dict,
    metric_defs: list[MetricDef],
    period_end: str | None = None,
    pesos_per_unit: float = PESOS_PER_UNIT,
    expected_currency: str | None = None,
) -> dict:
    """Tier 1: return {metric_key: MetricRow} for every IFRS-mappable metric present.

    Args:
        facts:       the ``"facts"`` dict from a ``*_facts.json`` artifact.
        metric_defs: metric definitions; only those with ``xbrl_concepts`` apply.
        period_end:  ISO date of the quarter end (e.g. "2026-03-31") to anchor
                     context selection. If None, the latest period is used.
        pesos_per_unit: pesos per project unit (1e6 for "millions", 1e3 for miles).
        expected_currency: ISO-4217 code of the company's reporting currency
                     (``iso_currency_for(cfg)``); monetary facts in any other
                     currency are ignored. None disables the filter.
    """
    from src.extract.extract_metrics import MetricRow   # local import avoids a cycle

    found: dict = {}
    if not facts:
        return found

    for mdef in metric_defs:
        if not mdef.xbrl_concepts or mdef.key in found:
            continue
        chosen = None    # (concept, cur_val, prior_val, anchor)
        zero_fb = None   # first zero-valued candidate — used only if no non-zero concept resolves
        for concept in mdef.xbrl_concepts:
            # "A + B" sums component concepts (e.g. Liverpool commercial revenue =
            # RevenueFromSaleOfGoods + RevenueFromRenderingOfServices). Every part
            # must resolve for the same period, or the whole spec degrades.
            parts = [p.strip() for p in concept.split("+")] if "+" in concept else [concept]
            cur_val = 0.0
            prior_val: float | None = 0.0
            anchor = ""
            ok = True
            for part in parts:
                entries = facts.get(part)
                if entries and expected_currency:
                    entries = [e for e in entries if _currency_ok(e, expected_currency)]
                current = _pick_entry(entries, period_end, mdef.section) if entries else None
                if current is None:
                    ok = False
                    break
                part_anchor = current.get("period_end") or current.get("instant") or ""
                if anchor and part_anchor != anchor:
                    ok = False   # parts answered different periods — do not mix
                    break
                anchor = part_anchor
                cur_val += _scale(current, mdef.unit, pesos_per_unit)
                prior_entry = _pick_prior(entries, current, mdef.section)
                if prior_val is not None and prior_entry is not None:
                    prior_val += _scale(prior_entry, mdef.unit, pesos_per_unit)
                else:
                    prior_val = None
            if not ok:
                continue
            # Some issuers file a mapped concept as an empty 0.0 line while the real
            # value sits in a LATER concept (e.g. ifrs-full Adjustments D&A = 0 vs the
            # real mx_ccd_ D&A) — for these metrics prefer the first NON-ZERO concept,
            # keeping a zero only as a last resort.
            if cur_val == 0 and mdef.key in _PREFER_NONZERO:
                zero_fb = zero_fb or (concept, cur_val, prior_val, anchor)
                continue
            chosen = (concept, cur_val, prior_val, anchor)
            break

        chosen = chosen or zero_fb
        if chosen is None:
            continue
        concept, cur_val, prior_val, anchor = chosen
        var_pct = None
        if prior_val not in (None, 0):
            var_pct = round((cur_val - prior_val) / abs(prior_val) * 100, 2)

        found[mdef.key] = MetricRow(
            metric=mdef.key,
            label_es=mdef.label_es,
            current=cur_val,
            prior=prior_val,
            var_pct=var_pct,
            unit=mdef.unit,
            source_line=f"[xbrl] {concept} {anchor}".strip(),
        )

    return found

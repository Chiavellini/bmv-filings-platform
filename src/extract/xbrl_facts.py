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

Most raw IFRS monetary values are in full currency units. Some BMV artifacts,
however, preserve the filing's displayed millions denomination. Callers use
``fact_value_divisor_for`` to normalize either representation into the
company's configured workbook unit before extracting metrics.
"""

from __future__ import annotations

from datetime import date
import math
import re

from src.model.financial_model import MetricDef

# Units whose raw IFRS value (pesos) must be divided by 1000 → miles de pesos.
MONETARY_UNITS = {"currency", "miles_mxn"}
PESOS_PER_UNIT = 1000.0

# A quarterly flow spans ~one calendar quarter; YTD spans are longer. Used to
# distinguish the single-quarter fact from the year-to-date fact.
_QUARTER_DAYS = 92
_QUARTER_MAX_DAYS = 100   # accept up to ~3 months; rejects half-year/YTD spans
_QUARTER_MIN_DAYS = 70    # reject monthly/irregular contexts as quarter assertions
_FY_MIN_DAYS = 300
_FY_MAX_DAYS = 380


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


def fact_value_divisor_for(
    cfg: dict | None,
    facts: dict | None,
    *,
    currency_mode: str = "native",
) -> float:
    """Return the divisor that normalizes one filing into workbook units.

    The unit math is deliberately expressed in three independent quantities::

        output = fact_value * fact_denomination * fx / output_unit_scale

    ``_scale`` performs division, so this function returns the equivalent
    ``output_unit_scale / (fact_denomination * fx)``.  A filing detected as
    already denominated in millions uses ``fact_denomination=1e6``.  Native
    mode leaves the filing currency unchanged; ``convert_to_mxn`` applies
    USD→MXN exactly once.

    This also handles configurations whose workbook unit is thousands: a fact
    reported in millions correctly uses a ``0.001`` divisor (one million-unit
    fact equals one thousand thousands).
    """
    output_unit_scale = pesos_per_unit_for(cfg)
    fact_denomination = 1e6 if _facts_in_millions(facts) else 1.0
    fx_to_output_currency = 1.0
    if currency_mode == "convert_to_mxn" and detect_reporting_currency(facts) == "USD":
        fx_to_output_currency = _USDMXN
    return output_unit_scale / (fact_denomination * fx_to_output_currency)


def pesos_per_unit_for_facts(cfg: dict | None, facts: dict | None) -> float:
    """Compatibility wrapper for the convert-to-MXN coverage path.

    New code should call :func:`fact_value_divisor_for` and state its currency
    mode explicitly.  Keeping this wrapper preserves the coverage API while
    fixing already-in-millions USD filings, whose FX-adjusted divisor is below
    ``1e6`` and therefore escaped the former magnitude guard.
    """
    return fact_value_divisor_for(cfg, facts, currency_mode="convert_to_mxn")


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


def _quarter_label_for_end(
    end: date,
    report_period: str,
    report_end: date | None,
) -> str:
    """Map a duration end to the issuer's canonical fiscal-quarter label.

    When the report end is known, map relative to that report's quarter. This
    preserves non-calendar fiscal calendars. The calendar-quarter fallback is
    used only when that anchor is unavailable.
    """
    from src.extract.revisions import normalize_period_label

    normalized = normalize_period_label(report_period)
    match = re.fullmatch(r"(?P<year>\d{4})-(?P<quarter>[1-4])T", normalized)
    if match and report_end is not None and end <= report_end:
        month_gap = (report_end.year - end.year) * 12 + report_end.month - end.month
        steps = int(round(month_gap / 3.0))
        expected_days = steps * (365.2425 / 4.0)
        if steps >= 0 and abs((report_end - end).days - expected_days) <= 24:
            index = int(match.group("year")) * 4 + int(match.group("quarter")) - 1 - steps
            return f"{index // 4:04d}-{index % 4 + 1}T"
    quarter = (end.month - 1) // 3 + 1
    return f"{end.year:04d}-{quarter}T"


def _duration_period_identity(
    entry: dict,
    report_period: str,
    report_end: date | None,
) -> tuple[str, str] | None:
    """Return ``(period, kind)`` for safe quarter/FY duration contexts only."""
    if _has_dimensions(entry) or _is_instant(entry):
        return None
    end = _parse_iso(entry.get("period_end"))
    span = _span_days(entry)
    if end is None or span is None:
        return None
    if _QUARTER_MIN_DAYS <= span <= _QUARTER_MAX_DAYS:
        return _quarter_label_for_end(end, report_period, report_end), "quarter"
    if _FY_MIN_DAYS <= span <= _FY_MAX_DAYS:
        from src.extract.revisions import normalize_period_label

        normalized = normalize_period_label(report_period)
        report_fy = re.fullmatch(r"(?P<year>\d{4})-FY", normalized)
        report_q4 = re.fullmatch(r"(?P<year>\d{4})-4T", normalized)
        if report_end is not None and (report_fy or report_q4) and end <= report_end:
            year_gap = int(round((report_end - end).days / 365.2425))
            report_year = int((report_fy or report_q4).group("year"))
            return f"{report_year - year_gap:04d}-FY", "fy"
        return f"{end.year:04d}-FY", "fy"
    # YTD and other irregular contexts are deliberately not projected onto a
    # quarterly/FY workbook cell.
    return None


def _unambiguous_scaled_value(
    entries: list[dict],
    unit: str,
    pesos_per_unit: float,
) -> float | None:
    """One value when duplicate contexts agree; None for conflicting contexts."""
    values = [_scale(entry, unit, pesos_per_unit) for entry in entries]
    if not values:
        return None
    first = values[0]
    if any(not math.isclose(first, value, rel_tol=1e-12, abs_tol=1e-9)
           for value in values[1:]):
        return None
    return first


def extract_comparative_observations_from_xbrl(
    facts: dict,
    metric_defs: list[MetricDef],
    *,
    report_period: str,
    period_end: str | None = None,
    pesos_per_unit: float = PESOS_PER_UNIT,
    expected_currency: str | None = None,
    source_document_id: str = "",
) -> list:
    """Emit trusted typed assertions for earlier periods in a later XBRL filing.

    This is intentionally narrower than global ``latest_comparative`` behavior:
    only dimensionless official XBRL duration contexts that unambiguously map to
    a single quarter or full year are emitted. YTD contexts, balance-sheet
    instants, dimensional facts, and conflicting duplicate contexts are skipped.
    The same mapped concept used by the report's current period anchors every
    comparative, preventing cross-taxonomy fallback from rewriting history.
    """
    from src.extract.revisions import (
        FactObservation,
        ObservationRole,
        PeriodKind,
        normalize_period_label,
    )
    from src.shared.report_index import period_sort_key

    if not facts:
        return []
    normalized_report = normalize_period_label(report_period)
    report_end = _parse_iso(period_end)
    current_targets = [normalized_report]
    q4 = re.fullmatch(r"(?P<year>\d{4})-4T", normalized_report)
    if q4:
        current_targets.append(f"{q4.group('year')}-FY")

    observations: list[FactObservation] = []
    for mdef in metric_defs:
        if not mdef.xbrl_concepts or mdef.section == "balance":
            continue

        chosen = None
        zero_fallback = None
        for concept in mdef.xbrl_concepts:
            parts = [part.strip() for part in concept.split("+")]
            grouped_parts: list[dict[tuple[str, str], list[dict]]] = []
            valid = True
            for part in parts:
                entries = list(facts.get(part) or [])
                if expected_currency:
                    entries = [entry for entry in entries
                               if _currency_ok(entry, expected_currency)]
                grouped: dict[tuple[str, str], list[dict]] = {}
                for entry in entries:
                    identity = _duration_period_identity(
                        entry, normalized_report, report_end,
                    )
                    if identity is not None:
                        grouped.setdefault(identity, []).append(entry)
                if not grouped:
                    valid = False
                    break
                grouped_parts.append(grouped)
            if not valid:
                continue

            common = set(grouped_parts[0])
            for grouped in grouped_parts[1:]:
                common.intersection_update(grouped)
            current_identity = next(
                (identity for target in current_targets for identity in common
                 if identity[0] == target),
                None,
            )
            if current_identity is None:
                continue
            current_value = 0.0
            for grouped in grouped_parts:
                component = _unambiguous_scaled_value(
                    grouped[current_identity], mdef.unit, pesos_per_unit,
                )
                if component is None:
                    valid = False
                    break
                current_value += component
            if not valid:
                continue
            payload = (concept, grouped_parts, common)
            if current_value == 0 and mdef.key in _PREFER_NONZERO:
                zero_fallback = zero_fallback or payload
                continue
            chosen = payload
            break

        chosen = chosen or zero_fallback
        if chosen is None:
            continue
        concept, grouped_parts, common = chosen
        for observed_period, kind in sorted(
                common, key=lambda identity: period_sort_key(identity[0])):
            if observed_period in current_targets:
                continue
            if period_sort_key(observed_period) >= period_sort_key(normalized_report):
                continue
            value = 0.0
            valid = True
            for grouped in grouped_parts:
                component = _unambiguous_scaled_value(
                    grouped[(observed_period, kind)], mdef.unit, pesos_per_unit,
                )
                if component is None:
                    valid = False
                    break
                value += component
            if not valid or not math.isfinite(value):
                continue
            observations.append(FactObservation(
                metric=mdef.key,
                observed_period=observed_period,
                value=value,
                report_period=normalized_report,
                period_kind=(PeriodKind.FY if kind == "fy" else PeriodKind.QUARTER),
                source_tier="xbrl",
                source_document_id=source_document_id,
                role=ObservationRole.COMPARATIVE,
                trusted=True,
                evidence=f"{concept} comparative context {observed_period}",
            ))
    return observations


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

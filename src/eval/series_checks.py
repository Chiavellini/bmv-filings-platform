"""
series_checks.py — shared cell-level suspicion checks (pipeline + gate).

Single source of truth for "is this extracted cell suspicious?" so the workbook
marking (pipeline attaches ``df.attrs['suspects']`` before the Excel build) and
the verification gate (Phase 6 scorecard/worklist) can never disagree.

Checks live here: sign (metric expected positive), config range, and the
magnitude-break detector. Validator cross-check failures and low confidence are
NOT here — those already travel per-cell in ``df.attrs['confidence']``.

Also home of the declarative accounting-identity table shared by the Python
identity audit (validation_report) and the in-sheet identity Check rows
(segments_sheet), so the two can't drift.
"""
from __future__ import annotations

import math
import re
import statistics

_PERIOD_RE = re.compile(r"(\d{4})-(\d)[TQ]", re.IGNORECASE)
_FY_PERIOD_RE = re.compile(r"(\d{4})-FY", re.IGNORECASE)

# Metrics that can legitimately be negative — exempt from the sign check, and
# from the magnitude check (they swing around zero; a small value isn't an error).
SIGNED_DEFAULT = {
    "net_new_stores", "stores_closed", "fx_gain_loss", "other_income_expense",
    "interest_income_net", "minority_interest", "var_pct",
}

RATIO = 4.0        # value off its neighborhood median by >this factor = break
NEIGHBORS = 4      # window on each side for the local median
_MIN_NEIGHBORS = 3  # fewer usable neighbors than this → global-median fallback
_MIN_POINTS = 5    # too little history to call an outlier at all

# Trusted accounting identities, declarative: (target, op_a, operator, op_b,
# label). Only these are trusted for auditing/check rows; subtotals like
# operating_income vs gross−opex have intervening line items and would
# false-positive. Consumed by validation_report (Python audit) and
# segments_sheet (live in-sheet Check rows).
IDENTITIES = [
    ("gross_profit", "revenue", "-", "cogs", "Gross = Revenue − COGS"),
    ("ebitda", "operating_income", "+", "depreciation", "EBITDA = Operating income + D&A"),
    ("net_income", "ebt", "-", "tax_expense", "Net income = Pre-tax − Tax"),
    ("net_debt", "total_debt", "-", "cash", "Net debt = Total debt − Cash"),
    ("free_cash_flow", "cfo", "-", "capex", "FCF = CFO − Capex"),
]

# Validator rule names corresponding to each declarative identity.  Workbook
# auto-checks use this registry to honor company-level ``validator.skip_rules``
# without duplicating accounting semantics in the Excel layer.
IDENTITY_RULES = {
    "gross_profit": "gross_profit_identity",
    "ebitda": "ebitda_derivation",
    "net_income": "net_income_identity",
    "net_debt": "net_debt_identity",
    "free_cash_flow": "fcf_derivation",
}


def identity_expected(values: dict, identity) -> float:
    """Evaluate one IDENTITIES entry against a {key: value} mapping."""
    _, a, op, b, _ = identity
    return values[a] + values[b] if op == "+" else values[a] - values[b]


def _period_key(p) -> tuple[int, int] | None:
    m = _PERIOD_RE.match(str(p))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _series_period_key(period: str) -> tuple[int, int, str]:
    """Chronological key that places FY after Q4 of the same calendar year."""
    text = str(period)
    quarter = _PERIOD_RE.fullmatch(text)
    if quarter:
        return (int(quarter.group(1)), int(quarter.group(2)), text)
    annual = _FY_PERIOD_RE.fullmatch(text)
    if annual:
        return (int(annual.group(1)), 5, text)
    return (9999, 99, text)


def _period_kind(period: str) -> str:
    """Coarse comparison bucket used only for sum/flow magnitude checks."""
    text = str(period)
    if _FY_PERIOD_RE.fullmatch(text):
        return "fy"
    if _PERIOD_RE.fullmatch(text):
        return "quarter"
    return "other"


def in_window(period: str, window: str | None) -> bool:
    """`window` is "START:END" (either side optional), inclusive, period-sorted."""
    if not window:
        return True
    yq = _period_key(period)
    if yq is None:
        return True
    start, _, end = window.partition(":")
    if start and (_period_key(start) or (0, 0)) > yq:
        return False
    if end and (_period_key(end) or (9999, 9)) < yq:
        return False
    return True


def magnitude_breaks(series: list[tuple[str, float]], *, ratio: float = RATIO,
                     neighbors: int = NEIGHBORS,
                     separate_period_kinds: bool = False) -> dict[str, str]:
    """Return ``{period: reason}`` for values that break from their neighborhood.

    Each value is compared to the median of its nearest non-zero neighbors (up
    to ``neighbors`` on each side, period-sorted, excluding itself); flagged in
    BOTH directions (>ratio× or <1/ratio×) — this is what catches "revenue in
    the hundreds for ten periods, then suddenly single-digit". Fewer than
    _MIN_NEIGHBORS usable neighbors falls back to the whole-series median.

    Ratio-based (not MAD/z-score) on purpose: financial series trend and step
    over a decade, so a robust-z flags legitimate drift. The real failure modes
    are unit artifacts (×10³/×10⁶) and wrong-row/period grabs, which are
    *factor*-off — those exceed the ratio while a secular trend, compared to
    its local neighborhood rather than the global median, does not.

    When ``separate_period_kinds`` is true, quarter and FY observations form
    independent peer groups. Callers enable this only for sum/flow metrics;
    stocks, averages, ratios, and unknown metrics preserve the legacy mixed-
    period comparison.
    """
    pts = sorted(series, key=lambda pv: _series_period_key(pv[0]))
    if separate_period_kinds:
        groups: dict[str, list[tuple[str, float]]] = {}
        for period, value in pts:
            groups.setdefault(_period_kind(period), []).append((period, value))
        out: dict[str, str] = {}
        for kind, group in groups.items():
            group_breaks = magnitude_breaks(
                group,
                ratio=ratio,
                neighbors=neighbors,
                separate_period_kinds=False,
            )
            if kind == "fy" and group_breaks:
                # Annual extraction can contain a run of historical unit-scale
                # artifacts followed by a stable corrected basis. A symmetric
                # local median would mark both clusters. Confirm FY candidates
                # against the median scale of the latest three non-zero FY peers:
                # an isolated newest artifact is still rejected, while a stable
                # current annual regime is not condemned by older bad history.
                recent = [abs(value) for _, value in group if value != 0][-_MIN_NEIGHBORS:]
                if len(recent) >= _MIN_NEIGHBORS:
                    current_med = statistics.median(recent)
                    if current_med:
                        values = {period: value for period, value in group}
                        group_breaks = {
                            period: reason
                            for period, reason in group_breaks.items()
                            if (
                                abs(values[period]) / current_med > ratio
                                or abs(values[period]) / current_med < 1.0 / ratio
                            )
                        }
            out.update(group_breaks)
        return dict(sorted(out.items(), key=lambda item: _series_period_key(item[0])))

    nonzero = [abs(v) for _, v in pts if v != 0]
    if len(nonzero) < _MIN_POINTS:
        return {}
    global_med = statistics.median(nonzero)
    if global_med == 0:
        return {}
    out: dict[str, str] = {}
    for i, (p, v) in enumerate(pts):
        neigh = ([abs(x) for _, x in pts[max(0, i - neighbors):i] if x != 0]
                 + [abs(x) for _, x in pts[i + 1:i + 1 + neighbors] if x != 0])
        if len(neigh) >= _MIN_NEIGHBORS:
            med, basis = statistics.median(neigh), "neighbor median"
        else:
            med, basis = global_med, "series median"
        if med == 0:
            continue
        r = abs(v) / med
        if r > ratio or r < 1.0 / ratio:
            out[p] = (f"magnitude outlier vs surrounding periods "
                      f"({v:,.4g} vs {basis} {med:,.4g})")
    return out


def compute_cell_suspects(df, keys, expectations: dict | None = None,
                          conf: dict | None = None) -> dict[tuple[str, str], str]:
    """Return ``{(period, key): reason}`` for sign / range / magnitude suspects.

    Mirrors the gate's per-cell rules minus validator-flagged / low-confidence
    (those already live in ``df.attrs['confidence']``). ``[verified]`` cells are
    skipped — a manual verification is resolved, never re-flagged. Sign and
    range reasons take precedence over a magnitude break on the same cell.
    """
    expectations = expectations or {}
    if conf is None:
        conf = getattr(df, "attrs", {}).get("confidence", {}) or {}
    aggregation_map = (
        getattr(df, "attrs", {}).get("metric_aggregations", {}) or {}
    )
    periods = [str(p) for p in df["period"]] if "period" in df.columns else []

    val_at: dict[tuple[str, str], float] = {}
    for _, row in df.iterrows():
        p = str(row["period"])
        for k in keys:
            if k in df.columns:
                v = row[k]
                if v is not None and not (isinstance(v, float) and math.isnan(v)):
                    val_at[(p, k)] = float(v)

    out: dict[tuple[str, str], str] = {}
    for k in keys:
        exp = expectations.get(k, {})
        win_periods = [p for p in periods if in_window(p, exp.get("window"))]
        present = [(p, val_at[(p, k)]) for p in win_periods if (p, k) in val_at]
        if not present:
            continue
        signed = exp.get("sign") == "any" or (exp.get("sign") != "positive" and k in SIGNED_DEFAULT)
        rng = exp.get("range")
        lo, hi = rng if isinstance(rng, (list, tuple)) else (None, None)
        aggregation = exp.get("aggregation") or aggregation_map.get(k)
        if aggregation is None:
            try:
                from src.model.financial_model import METRIC_BY_KEY
                metric = METRIC_BY_KEY.get(k)
                aggregation = metric.aggregation if metric is not None else None
            except Exception:  # pragma: no cover - conservative standalone fallback
                aggregation = None
        aggregation_kind = str(aggregation or "").strip().lower()
        breaks = {} if signed else magnitude_breaks(
            present,
            separate_period_kinds=(aggregation_kind == "sum"),
        )
        for p, v in present:
            src = (conf.get((p, k), {}) or {}).get("source", "") or ""
            if "[verified]" in src:
                continue  # manually verified → resolved
            if not signed and v < 0:
                out[(p, k)] = "negative value (expected positive)"
            elif lo is not None and v < lo:
                out[(p, k)] = f"below expected min {lo}"
            elif hi is not None and v > hi:
                out[(p, k)] = f"above expected max {hi}"
            elif p in breaks:
                out[(p, k)] = breaks[p]
    return out

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
]


def identity_expected(values: dict, identity) -> float:
    """Evaluate one IDENTITIES entry against a {key: value} mapping."""
    _, a, op, b, _ = identity
    return values[a] + values[b] if op == "+" else values[a] - values[b]


def _period_key(p) -> tuple[int, int] | None:
    m = _PERIOD_RE.match(str(p))
    return (int(m.group(1)), int(m.group(2))) if m else None


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
                     neighbors: int = NEIGHBORS) -> dict[str, str]:
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
    """
    pts = sorted(series, key=lambda pv: (_period_key(pv[0]) or (0, 0), str(pv[0])))
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
        breaks = {} if signed else magnitude_breaks(present)
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

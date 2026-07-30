"""SUE (standardized unexpected earnings) math. Scale-invariant by construction."""
from __future__ import annotations

from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

_QEND = {"1": "-03-31", "2": "-06-30", "3": "-09-30", "4": "-12-31"}


def winsorize(x: np.ndarray, lim: float) -> np.ndarray:
    return np.clip(x, -lim, lim)


def availability_array(periods, filed) -> np.ndarray:
    """Availability timestamps aligned with ``periods``, for trailing_sue's
    filed= mask. Real filing timestamps pass through; undated quarters fall
    back to period_end + 90d @ 23:59 CDMX — usable for sigma masking only,
    never as events. Every masked SUE in the study must come through here so
    all call sites share one convention (issue A of the v2 audit).
    """
    from earnlib.calendar import CDMX

    out = []
    for p, f in zip(periods, filed):
        if f is not None and not pd.isna(f):
            out.append(f.to_pydatetime() if hasattr(f, "to_pydatetime") else f)
        else:
            end = datetime.fromisoformat(p[:4] + _QEND[p[5]])
            out.append(datetime.combine((end + timedelta(days=90)).date(),
                                        time(23, 59), tzinfo=CDMX))
    return np.array(out, dtype=object)


def trailing_sue(
    dx: np.ndarray,
    min_hist: int = 4,
    target_hist: int = 8,
    filed: np.ndarray | None = None,
) -> np.ndarray:
    """SUE per quarter from an ordered (oldest->newest) YoY-change series.

    SUE[q] = dx[q] / std(trailing dx); NaN until min_hist trailing observations
    exist. Only strictly-earlier quarters enter sigma. If ``filed`` (array of
    filing timestamps, NaT allowed) is given, a trailing quarter additionally
    requires filed[j] < filed[q] — BMV issuers often file Q4 XBRL with the
    audited annual AFTER next year's Q1, so period order alone is not
    availability order.
    """
    out = np.full(len(dx), np.nan)
    for q in range(len(dx)):
        if np.isnan(dx[q]):
            continue
        lo = max(0, q - target_hist)
        hist = []
        for j in range(lo, q):
            if np.isnan(dx[j]):
                continue
            if filed is not None:
                fj, fq = filed[j], filed[q]
                if fj is None or fq is None or not (fj < fq):
                    continue
            hist.append(dx[j])
        if len(hist) < min_hist:
            continue
        sigma = np.std(np.array(hist), ddof=1)
        if sigma > 0:
            out[q] = dx[q] / sigma
    return out


def cross_z(values: np.ndarray, min_pool: int = 5, winsor: float = 3.0) -> np.ndarray:
    """Within-quarter cross-sectional z-score (NaN-aware)."""
    out = np.full(len(values), np.nan)
    mask = ~np.isnan(values)
    if mask.sum() < min_pool:
        return out
    pool = values[mask]
    sd = np.std(pool, ddof=1)
    if sd == 0:
        return out
    out[mask] = winsorize((pool - pool.mean()) / sd, winsor)
    return out

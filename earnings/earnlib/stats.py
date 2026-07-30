"""Hand-rolled event-study statistics (no statsmodels in the venv)."""
from __future__ import annotations

import numpy as np
from scipy import stats as sps


def mean_t(x: np.ndarray) -> dict:
    """Cross-sectional mean with plain t-stat (NaN-aware)."""
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 2:
        return {"mean": np.nan, "t": np.nan, "p": np.nan, "n": n}
    m = x.mean()
    se = x.std(ddof=1) / np.sqrt(n)
    t = m / se if se > 0 else np.nan
    p = 2 * sps.t.sf(abs(t), n - 1) if np.isfinite(t) else np.nan
    return {"mean": m, "t": t, "p": p, "n": n}


def fama_macbeth(per_group: np.ndarray) -> dict:
    """t-stat over per-quarter statistics (bucket means or slopes)."""
    r = mean_t(per_group)
    return {"fm_mean": r["mean"], "fm_t": r["t"], "fm_p": r["p"], "n_quarters": r["n"]}


def ols_fe_clustered(y: np.ndarray, x: np.ndarray, groups: np.ndarray) -> dict:
    """OLS slope of y on x with group (quarter) fixed effects and
    group-clustered standard errors. Demeaning within group absorbs the FE."""
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    mask = ~(np.isnan(y) | np.isnan(x))
    y, x, groups = y[mask], x[mask], np.asarray(groups)[mask]
    if len(y) < 10:
        return {"slope": np.nan, "t": np.nan, "p": np.nan, "n": len(y)}

    yd = np.empty_like(y)
    xd = np.empty_like(x)
    for g in np.unique(groups):
        idx = groups == g
        yd[idx] = y[idx] - y[idx].mean()
        xd[idx] = x[idx] - x[idx].mean()

    sxx = np.sum(xd * xd)
    if sxx == 0:
        return {"slope": np.nan, "t": np.nan, "p": np.nan, "n": len(y)}
    slope = np.sum(xd * yd) / sxx
    resid = yd - slope * xd
    # cluster-robust sandwich: Var(b) = sum_g (sum_i x_i e_i)^2 / sxx^2
    uniq = np.unique(groups)
    score_sq = 0.0
    for g in uniq:
        idx = groups == g
        score_sq += np.sum(xd[idx] * resid[idx]) ** 2
    g_count = len(uniq)
    if g_count > 1:
        score_sq *= g_count / (g_count - 1)   # small-sample cluster correction
    se = np.sqrt(score_sq) / sxx
    t = slope / se if se > 0 else np.nan
    p = 2 * sps.t.sf(abs(t), g_count - 1) if np.isfinite(t) else np.nan
    return {"slope": slope, "t": t, "p": p, "n": len(y), "n_clusters": g_count}


def spearman_ic(signal: np.ndarray, fwd: np.ndarray) -> dict:
    """Rank IC between signal and forward return (NaN-aware)."""
    signal = np.asarray(signal, dtype=float)
    fwd = np.asarray(fwd, dtype=float)
    mask = ~(np.isnan(signal) | np.isnan(fwd))
    n = int(mask.sum())
    if n < 5:
        return {"ic": np.nan, "t": np.nan, "n": n}
    ic, _ = sps.spearmanr(signal[mask], fwd[mask])
    return {"ic": ic, "t": ic * np.sqrt(n), "n": n}


def dual_sharpe(daily: np.ndarray, years: float,
                trading_days_per_year: int = 252) -> dict:
    """Two labeled Sharpe bases for an event strategy's trade-day returns.

    sharpe_trade_day: annualized by realized trade-days/year — a
    'while invested' figure, NOT comparable to buy-and-hold Sharpe.
    sharpe_calendar: the trade-day returns embedded in a flat-cash calendar
    of trading_days_per_year*years days — comparable to buy-and-hold.
    (Issue F of the v2 audit: only the first was reported, next to B&H.)
    """
    daily = np.asarray(daily, dtype=float)
    daily = daily[np.isfinite(daily)]
    out = {"sharpe_trade_day": np.nan, "sharpe_calendar": np.nan}
    if len(daily) < 2 or years <= 0:
        return out
    sd = daily.std(ddof=1)
    if sd > 0:
        out["sharpe_trade_day"] = float(
            daily.mean() / sd * np.sqrt(len(daily) / years))
    n_cal = int(round(trading_days_per_year * years))
    if n_cal > len(daily):
        cal = np.zeros(n_cal)
        cal[:len(daily)] = daily
        sd_c = cal.std(ddof=1)
        if sd_c > 0:
            out["sharpe_calendar"] = float(
                cal.mean() / sd_c * np.sqrt(trading_days_per_year))
    return out


def hit_rate(signal: np.ndarray, ret: np.ndarray) -> dict:
    """P(ret>0 | signal>0) and P(ret<0 | signal<0), with exact binomial p."""
    signal = np.asarray(signal, dtype=float)
    ret = np.asarray(ret, dtype=float)
    mask = ~(np.isnan(signal) | np.isnan(ret)) & (signal != 0) & (ret != 0)
    s, r = signal[mask], ret[mask]
    agree = int(np.sum(np.sign(s) == np.sign(r)))
    n = len(s)
    if n == 0:
        return {"hit": np.nan, "p": np.nan, "n": 0}
    p = sps.binomtest(agree, n, 0.5, alternative="greater").pvalue
    return {"hit": agree / n, "p": p, "n": n}

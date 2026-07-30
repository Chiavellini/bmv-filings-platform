"""Mechanical data-quality rules for the v2 audit.

Every rule here is pre-committed in study.yaml (audit_v2 block) and defined
on internal consistency only — no rule may reference returns, events, or
SUEs. Rows are DROPPED, never rescaled, and every drop is logged with the
rule that fired. Constants were calibrated 2026-07-28 against known-bad
exemplars only (see the audit_v2 comments) and are frozen.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def clean_metrics_hist(df: pd.DataFrame,
                       rules: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply the audit_v2.hist_cleaning rules to a long metrics frame with
    columns [slug, period, metric, current, ...]. Returns (clean, log) where
    log has one row per dropped value with the rule that fired.

    Order matters: R0 (exact zero) -> R1/R1b (scale vs same-period revenue)
    -> R2 (accounting identity, offender by distance to own-series median)
    -> R3 (jump vs clean neighbors) -> R4 (too-short series).
    """
    df = df.sort_values(["slug", "metric", "period"]).reset_index(drop=True)
    flag = pd.Series("", index=df.index, dtype=object)

    r1_metrics = set(rules["r1_margin_level"]["metrics"])
    r1_frac = float(rules["r1_margin_level"]["revenue_frac"])
    r1b_frac = float(rules["r1b_eps_as_net_income"]["revenue_frac"])
    r0_metrics = set(rules["r0_exact_zero_metrics"])
    jump_thr = float(rules["r3_jump"]["log10_threshold"])
    nei_span = int(rules["r3_jump"]["neighbor_span"])
    min_series = int(rules["r4_min_clean_series"])

    wide = df.pivot_table(index=["slug", "period"], columns="metric",
                          values="current", aggfunc="first")
    rev = wide["revenue"] if "revenue" in wide.columns else pd.Series(dtype=float)

    def _rev_of(slug, period):
        if rev.empty:
            return np.nan
        try:
            v = rev.loc[(slug, period)]
        except KeyError:
            return np.nan
        return float(v) if np.isfinite(v) else np.nan

    # ---- R0 / R1 / R1b: per-row magnitude screens -------------------------
    for i, r in df.iterrows():
        v, m = r["current"], r["metric"]
        if not np.isfinite(v) or m == "eps":
            continue
        if v == 0.0 and m in r0_metrics:
            flag.iloc[i] = "r0_exact_zero"
            continue
        rv = _rev_of(r["slug"], r["period"])
        if not (np.isfinite(rv) and rv > 0):
            continue
        if m in r1_metrics and abs(v) < r1_frac * rv:
            flag.iloc[i] = "r1_margin_level"
        elif m == "net_income" and 0 < abs(v) < r1b_frac * rv:
            flag.iloc[i] = "r1b_eps_as_net_income"

    # ---- series medians on rows clean so far (for R2 resolution) ----------
    med = {}
    for (s, m), sub in df.groupby(["slug", "metric"]):
        ok = sub.loc[(flag.loc[sub.index] == "") & np.isfinite(sub["current"])]
        med[(s, m)] = float(np.median(np.abs(ok["current"]))) if len(ok) else np.nan

    def _logdist(slug, metric, v):
        m = med.get((slug, metric), np.nan)
        if not (np.isfinite(m) and m > 0 and v != 0):
            return np.inf
        return abs(np.log10(abs(v) / m))

    # ---- R2: accounting identity, drop the farther-out member -------------
    for (s, p), row in wide.iterrows():
        rv = row.get("revenue", np.nan)
        if not (np.isfinite(rv) and rv > 0):
            continue
        for m in ("operating_income", "ebitda"):
            v = row.get(m, np.nan)
            if np.isfinite(v) and v > rv:
                offender = (m if _logdist(s, m, v) >= _logdist(s, "revenue", rv)
                            else "revenue")
                idx = df.index[(df["slug"] == s) & (df["period"] == p) &
                               (df["metric"] == offender) & (flag == "")]
                flag.loc[idx] = "r2_identity"

    # ---- R3: order-of-magnitude jump vs clean neighbors -------------------
    for (s, m), sub in df.groupby(["slug", "metric"]):
        sub = sub.sort_values("period")
        vals = sub["current"].to_numpy(dtype=float)
        f = flag.loc[sub.index].to_numpy(dtype=object)
        idxs = sub.index.to_numpy()
        for k in range(len(vals)):
            if f[k] != "" or not np.isfinite(vals[k]) or vals[k] == 0:
                continue
            nb = [vals[j] for j in range(len(vals))
                  if j != k and abs(j - k) <= nei_span
                  and f[j] == "" and np.isfinite(vals[j]) and vals[j] != 0]
            if len(nb) < 2:
                continue
            mdn = float(np.median(np.abs(nb)))
            if mdn > 0 and abs(np.log10(abs(vals[k]) / mdn)) > jump_thr:
                flag.loc[idxs[k]] = "r3_jump"

    # ---- R4: series left too short for any SUE ----------------------------
    for (s, m), sub in df.groupby(["slug", "metric"]):
        ok = sub.loc[(flag.loc[sub.index] == "") & np.isfinite(sub["current"])]
        if 0 < len(ok) < min_series:
            flag.loc[ok.index] = "r4_short_series"

    log = df.loc[flag != ""].copy()
    log["rule"] = flag.loc[flag != ""]
    clean = df.loc[flag == ""].reset_index(drop=True)
    return clean, log.reset_index(drop=True)


def filter_price_quality(df: pd.DataFrame,
                         min_bars: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Drop rows with adj_factor <= 0 (Yahoo corruption: negative adjusted
    closes) and symbols with fewer than min_bars bars (silent universe loss
    otherwise). Returns (clean, dropped_log) — the log names every dropped
    symbol so nothing disappears quietly (issue D)."""
    dropped = []
    bad_adj = df["adj_factor"] <= 0
    if bad_adj.any():
        for sym, n in df.loc[bad_adj, "symbol"].value_counts().items():
            dropped.append({"symbol": sym, "reason": "adj_factor<=0",
                            "n_rows": int(n)})
    clean = df.loc[~bad_adj].copy()
    counts = clean.groupby("symbol").size()
    thin = counts[counts < min_bars]
    for sym, n in thin.items():
        dropped.append({"symbol": sym, "reason": f"fewer_than_{min_bars}_bars",
                        "n_rows": int(n)})
    clean = clean[~clean["symbol"].isin(set(thin.index))].reset_index(drop=True)
    return clean, pd.DataFrame(dropped, columns=["symbol", "reason", "n_rows"])

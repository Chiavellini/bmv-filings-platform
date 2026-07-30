#!/usr/bin/env python3
"""compute_surprises.py — SUE scores + composite surprise per event.

Per company and metric: dX(q) = current(q) - prior(q) (both from filing q,
point-in-time), SUE(q) = dX(q) / std(trailing dX), winsorized. Composite:

  S_cs  (primary)  = mean of within-quarter cross-sectional z-scores of the
                     per-metric SUEs (hypothesis-testing construct)
  S_ts  (tradable) = mean of the winsorized per-metric SUEs (no peer info)

Lookahead guard: every trailing dX comes from a strictly earlier period whose
filing timestamp is asserted to be strictly before the event's.

Output: outputs/surprises.parquet
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib.study import merge_hist_currents
from earnlib.surprises import availability_array, trailing_sue, winsorize, cross_z

import numpy as np
import pandas as pd


XBRL_START = "2021-2T"


def _prev_year_period(period: str) -> str:
    return f"{int(period[:4]) - 1}{period[4:]}"


def main() -> None:
    cfg = bs.load_config()
    scfg = cfg["surprise"]
    metrics = pd.read_parquet(bs.art_path("metrics"))
    ev_path = bs.art_path("events_all")
    if not ev_path.exists():
        ev_path = bs.art_path("events")
    events = pd.read_parquet(ev_path)
    events = events[events["in_universe"] & events["has_facts"]]

    wide_cur = metrics.pivot_table(index=["slug", "period"], columns="metric",
                                   values="current", aggfunc="first")
    wide_pri = metrics.pivot_table(index=["slug", "period"], columns="metric",
                                   values="prior", aggfunc="first")

    # historical era: merge pre-XBRL currents; dX there = X(q) - X(same T, y-1)
    hist_path = bs.art_path("metrics_hist")
    if hist_path.exists():
        hist = pd.read_parquet(hist_path)
        hist_cur = hist.pivot_table(index=["slug", "period"], columns="metric",
                                    values="current", aggfunc="first")
        n_overlap = int(wide_cur.index.intersection(hist_cur.index).size)
        print(f"hist merge: {len(hist_cur)} hist rows, "
              f"{n_overlap} colliding (slug, period) keys (XBRL wins)")
        wide_cur = merge_hist_currents(wide_cur, hist_cur)
        wide_pri = wide_pri.reindex(wide_cur.index)

    dx_pit = wide_cur - wide_pri            # point-in-time (XBRL filings)
    prev_idx = pd.MultiIndex.from_tuples(
        [(s, _prev_year_period(p)) for s, p in wide_cur.index],
        names=wide_cur.index.names)
    dx_yoy = wide_cur - wide_cur.reindex(prev_idx).to_numpy()
    # cell-wise: point-in-time prior wherever a filing provides one, else the
    # YoY of merged currents (DoF log: refined from an era-boundary rule after
    # gate-1 caught the inconsistency for pre-2021 XBRL-tail companies)
    dx_full = dx_pit.where(dx_pit.notna(), dx_yoy)
    dx = dx_full.reset_index().sort_values(["slug", "period"])

    date_col = "info_dt" if "info_dt" in events.columns else "filed_dt"
    ev = events.set_index(["slug", "period"])[date_col]
    all_metrics = scfg["metrics"] + [scfg["eps_variant"]]
    rows = []
    order_violations = 0
    for slug, sub in dx.groupby("slug"):
        sub = sub.sort_values("period").reset_index(drop=True)
        filed = [ev.get((slug, p), pd.NaT) for p in sub["period"]]
        # lookahead guard: filing order must follow period order
        fs = pd.Series(filed)
        known = fs.dropna()
        if not known.is_monotonic_increasing:
            order_violations += 1
            print(f"WARN {slug}: filing timestamps not monotone in period order")

        # metric quarters with no dated event (deep history, CNBV-format docs)
        # are treated as public 90 days after quarter end for MASKING only —
        # they can serve in a later event's sigma, but are never events.
        filed_arr = availability_array(sub["period"], fs)
        sues, yoys = {}, {}
        for m in all_metrics:
            vals = sub[m].to_numpy(dtype=float) if m in sub else np.full(len(sub), np.nan)
            sues[m] = winsorize(
                trailing_sue(vals, scfg["min_hist_quarters"],
                             scfg["target_hist_quarters"], filed=filed_arr),
                scfg["sue_winsor"],
            )
            # plain YoY% variant with denominator floor
            cur = wide_cur.reindex([(slug, p) for p in sub["period"]])[m].to_numpy(dtype=float) \
                if m in wide_cur else np.full(len(sub), np.nan)
            pri = cur - vals
            floor = np.full(len(sub), np.nan)
            for i in range(len(sub)):
                h = np.abs(cur[max(0, i - 4):i])
                h = h[~np.isnan(h)]
                if len(h):
                    floor[i] = scfg["yoy_floor_frac"] * h.mean()
            denom = np.maximum(np.abs(pri), floor)
            with np.errstate(divide="ignore", invalid="ignore"):
                yoys[m] = np.where(denom > 0, vals / denom, np.nan)

        for i, p in enumerate(sub["period"]):
            rows.append({
                "slug": slug, "period": p, "info_dt": filed[i],
                **{f"sue_{m}": sues[m][i] for m in all_metrics},
                **{f"yoy_{m}": yoys[m][i] for m in scfg["metrics"]},
            })

    df = pd.DataFrame(rows)

    # composite scores
    comp_cols = [f"sue_{m}" for m in scfg["metrics"]]
    zcols = []
    for c in comp_cols:
        zc = f"z_{c}"
        df[zc] = np.nan
        for p, idx in df.groupby("period").groups.items():
            df.loc[idx, zc] = cross_z(
                df.loc[idx, c].to_numpy(dtype=float),
                scfg["min_cs_pool"], scfg["z_winsor"],
            )
        zcols.append(zc)

    zmat = df[zcols].to_numpy(dtype=float)
    smat = df[comp_cols].to_numpy(dtype=float)
    n_z = np.isfinite(zmat).sum(axis=1)
    n_s = np.isfinite(smat).sum(axis=1)
    with np.errstate(invalid="ignore"):
        df["s_cs"] = np.where(n_z >= scfg["min_components"], np.nanmean(zmat, axis=1), np.nan)
        df["s_ts"] = np.where(n_s >= scfg["min_components"], np.nanmean(smat, axis=1), np.nan)
    df["n_components"] = n_s

    tickers = {s: v["ticker"] for s, v in bs.load_universe(cfg, "full").items()}
    df["ticker"] = df["slug"].map(tickers)
    df.to_parquet(bs.art_path("surprises"), index=False)

    # ---- verify block ----
    scored = df[df["s_cs"].notna()]
    print(f"\ncompany-quarters: {len(df)}   scoreable (s_cs): {len(scored)}")
    print(f"filing-order violations: {order_violations}")
    print(f"first scoreable period: {scored['period'].min()}   last: {scored['period'].max()}")
    print("\nSUE distributions (should be roughly symmetric, fat-tailed):")
    print(df[comp_cols + ["sue_eps", "s_cs", "s_ts"]].describe().round(2).to_string())
    print("\nscoreable events per quarter:")
    print(scored.groupby("period").size().to_string())


if __name__ == "__main__":
    main()

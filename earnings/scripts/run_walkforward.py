#!/usr/bin/env python3
"""run_walkforward.py — ONE-SHOT 2026-2T evaluation: incumbent vs refined.

Pristine data: 54 filings downloaded from BMV after every development
decision was locked. Scores the quarter with the frozen incumbent (s_cs
composite / s_ts tradable) and the refined spec (margin-SUE z / raw
margin-SUE tradable), evaluates next-day reactions and the strategy legs
(exit close t0 and close t+1), once. Results are final as printed.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib import stats as st
from earnlib import study
from earnlib.calendar import CDMX
from earnlib.surprises import availability_array, trailing_sue, winsorize, cross_z

import numpy as np
import pandas as pd
import yaml

WF_DIR = bs.EARNINGS_ROOT / "data" / "walkforward"
PERIOD = "2026-2T"
PEND = "2026-06-30"


def wf_metrics(cfg: dict) -> pd.DataFrame:
    """Extract 2026-2T metrics from the walkforward facts files."""
    from src.extract.xbrl_facts import extract_from_xbrl
    sys.path.insert(0, str(bs.EARNINGS_ROOT / "scripts"))
    from build_metrics import metric_defs

    defs = metric_defs()
    uni = bs.load_universe(cfg, "full")
    t2s = {v["ticker"]: s for s, v in uni.items()}
    rows = []
    for f in sorted((WF_DIR / "xbrl").glob(f"*_{PERIOD}_facts.json")):
        ticker = f.name.split("_", 1)[0]
        slug = t2s.get(ticker)
        if slug is None:
            continue
        facts = json.loads(f.read_text()).get("facts", {})
        found = extract_from_xbrl(facts, defs, period_end=PEND, pesos_per_unit=1e6)
        for key, row in found.items():
            rows.append({"slug": slug, "ticker": ticker, "metric": key,
                         "current": row.current, "prior": row.prior})
        oi, dep = found.get("operating_income"), found.get("depreciation")
        if oi and dep and oi.current is not None and dep.current is not None:
            rows.append({"slug": slug, "ticker": ticker, "metric": "ebitda",
                         "current": oi.current + dep.current,
                         "prior": (oi.prior + dep.prior
                                   if oi.prior is not None and dep.prior is not None
                                   else None)})
    return pd.DataFrame(rows)


def main() -> None:
    cfg = bs.load_config()
    scfg = cfg["surprise"]
    wf = wf_metrics(cfg)
    print(f"walkforward metrics: {wf['slug'].nunique()} companies")

    hist = pd.read_parquet(bs.art_path("metrics"))
    cur_h = hist.pivot_table(index=["slug", "period"], columns="metric",
                             values="current", aggfunc="first")
    pri_h = hist.pivot_table(index=["slug", "period"], columns="metric",
                             values="prior", aggfunc="first")
    dx_h = cur_h - pri_h

    # filing timestamps from the fresh archive snapshot (needed up front so
    # every SUE below carries the availability mask — v2 audit issue A)
    meta = json.loads((WF_DIR / "filings_meta.json").read_text())
    filed = {}
    for m in meta:
        dt = datetime.strptime(m["filed_date"], "%d/%m/%Y %H:%M").replace(tzinfo=CDMX)
        k = m["slug"]
        if k not in filed or dt < filed[k]:
            filed[k] = dt
    avail = study.availability_map()

    def masked_last_sue(slug, series, hist_periods, dnew):
        """SUE of the appended walk-forward value with the availability mask.
        History quarters were all public before the 2026-2T filing, so the
        mask is a no-op for the final value today — it is here so this code
        path stays correct if reused with a pending stale quarter."""
        periods = list(hist_periods) + [PERIOD]
        stamps = ([avail.get((slug, p)) for p in hist_periods]
                  + [filed.get(slug)])
        s = trailing_sue(np.append(series, dnew),
                         scfg["min_hist_quarters"], scfg["target_hist_quarters"],
                         filed=availability_array(periods, stamps))
        return float(winsorize(np.array([s[-1]]), scfg["sue_winsor"])[0])

    # per-slug SUEs for 2026-2T (history = existing dX series, all pre-filed)
    srows = []
    for slug, sub in wf.groupby("slug"):
        vals = {r["metric"]: (r["current"], r["prior"]) for _, r in sub.iterrows()}

        def dx_new(metric):
            c, p = vals.get(metric, (None, None))
            return c - p if c is not None and p is not None else np.nan

        def sue_of(metric, dnew):
            if slug not in dx_h.index.get_level_values(0):
                return np.nan
            h = dx_h.loc[slug].sort_index()
            series = (h[metric].to_numpy(dtype=float)
                      if metric in h else np.array([]))
            return masked_last_sue(slug, series, list(h.index), dnew)

        # margin dX history (point-in-time)
        def margin_sue():
            c_oi, p_oi = vals.get("operating_income", (None, None))
            c_r, p_r = vals.get("revenue", (None, None))
            if None in (c_oi, p_oi, c_r, p_r) or 0 in (c_r, p_r):
                return np.nan
            dnew = c_oi / c_r - p_oi / p_r
            if slug not in cur_h.index.get_level_values(0):
                return np.nan
            hc, hp = cur_h.loc[slug].sort_index(), pri_h.loc[slug].sort_index()
            with np.errstate(divide="ignore", invalid="ignore"):
                dm = (hc["operating_income"] / hc["revenue"]
                      - hp["operating_income"] / hp["revenue"]).to_numpy(dtype=float)
            return masked_last_sue(slug, dm, list(hc.index), dnew)

        srows.append({
            "slug": slug,
            "sue_revenue": sue_of("revenue", dx_new("revenue")),
            "sue_net_income": sue_of("net_income", dx_new("net_income")),
            "sue_ebitda": sue_of("ebitda", dx_new("ebitda")),
            "margin_sue": margin_sue(),
        })
    S = pd.DataFrame(srows)
    comp = ["sue_revenue", "sue_net_income", "sue_ebitda"]
    for c in comp + ["margin_sue"]:
        S[f"z_{c}"] = cross_z(S[c].to_numpy(dtype=float),
                              scfg["min_cs_pool"], scfg["z_winsor"])
    zmat = S[[f"z_{c}" for c in comp]].to_numpy(dtype=float)
    smat = S[comp].to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        S["s_cs"] = np.where(np.isfinite(zmat).sum(axis=1) >= 2,
                             np.nanmean(zmat, axis=1), np.nan)
        S["s_ts"] = np.where(np.isfinite(smat).sum(axis=1) >= 2,
                             np.nanmean(smat, axis=1), np.nan)
    S["s_refined"] = S["z_margin_sue"]
    S["s_refined_ts"] = S["margin_sue"]

    # events: filing timestamps (loaded above, before scoring)
    uni = bs.load_universe(cfg, "full")
    sym = {s: v["symbol"] for s, v in uni.items()}
    S["filed_dt"] = S["slug"].map(filed)
    S["symbol"] = S["slug"].map(sym)
    S = S[S["filed_dt"].notna() & S["s_cs"].notna()]

    panel = study.load_panel(cfg)
    cutoff = datetime.fromisoformat(cfg["phase_e"]["walkforward_price_cutoff"]).date()
    rows = []
    late = 0
    for _, r in S.iterrows():
        al = panel.cal.align(r["filed_dt"].to_pydatetime())
        if al is None or al.t0 > cutoff:
            late += 1
            continue
        m = panel.measures(r["symbol"], al.t0, cfg,
                           filing_day=al.filing_day if al.filed_in_session else None)
        if m is None or not np.isfinite(m["ar0_cc"]):
            late += 1
            continue
        rows.append({**r.to_dict(), **m, "filed_in_session": al.filed_in_session})
    df = pd.DataFrame(rows)
    df["period"] = PERIOD
    df = study.apply_liquidity(df, cfg["liquidity"]["primary_min_median_peso_volume"])
    print(f"evaluable walkforward events: {len(df)} (excluded late/no-bar: {late})")

    # ---- evaluation, once ----
    out = []
    for sig, name in [("s_cs", "incumbent s_cs"), ("s_refined", "refined margin-SUE")]:
        d = df[df[sig].notna()].copy()
        d = study.add_terciles(d, sig)
        t3 = d.loc[d["tercile"] == 3.0, "ar0_cc"]
        t1 = d.loc[d["tercile"] == 1.0, "ar0_cc"]
        hit = st.hit_rate(d[sig].to_numpy(), d["ar0_cc"].to_numpy())
        ic = st.spearman_ic(d[sig].to_numpy(), d["ar0_cc"].to_numpy())
        out.append({"spec": name, "n": len(d),
                    "T3_bps": round(t3.mean() * 1e4), "T1_bps": round(t1.mean() * 1e4),
                    "spread_bps": round((t3.mean() - t1.mean()) * 1e4),
                    "hit": round(hit["hit"], 2), "ic": round(ic["ic"], 3)})
    ev = pd.DataFrame(out)
    ev.to_csv(bs.RESULTS_DIR / "walkforward_eval.csv", index=False)
    print("\n=== WALK-FORWARD 2026-2T (one shot) ===")
    print(ev.to_string(index=False))

    # strategy legs: enter open(t0) (after-hours) / skip in-session for purity
    px = pd.read_parquet(bs.art_path("prices")).set_index(["symbol", "date"])
    cal = panel.cal.dates
    srow = []
    for sig, cut, name in [("s_ts", 1.0, "incumbent long s_ts>1"),
                           ("s_refined_ts", 1.0, "refined long margin_sue>1")]:
        leg = df[(df[sig] > cut) & ~df["filed_in_session"]]
        rets0, rets1 = [], []
        for _, r in leg.iterrows():
            try:
                b0 = px.loc[(r["symbol"], r["t0"])]
                i = np.searchsorted(cal, r["t0"])
                nxt = cal[i + 1] if i + 1 < len(cal) else None
                r0 = b0["close"] / b0["open"] - 1 if b0["open"] else np.nan
                rets0.append(r0)
                if nxt is not None and (r["symbol"], nxt) in px.index:
                    c1 = px.loc[(r["symbol"], nxt)]["close"]
                    rets1.append(r0 + (c1 / b0["close"] - 1))
                else:
                    rets1.append(np.nan)
            except KeyError:
                rets0.append(np.nan); rets1.append(np.nan)
        r0 = np.array(rets0, dtype=float); r0 = r0[np.isfinite(r0)] - 25e-4
        r1 = np.array(rets1, dtype=float); r1 = r1[np.isfinite(r1)] - 25e-4
        srow.append({"leg": name, "trades": len(r0),
                     "mean_bps_exit_t0": round(r0.mean() * 1e4) if len(r0) else None,
                     "sum_pct_exit_t0": round(r0.sum() * 100, 2) if len(r0) else None,
                     "mean_bps_exit_t1": round(r1.mean() * 1e4) if len(r1) else None,
                     "sum_pct_exit_t1": round(r1.sum() * 100, 2) if len(r1) else None})
    sdf = pd.DataFrame(srow)
    sdf.to_csv(bs.RESULTS_DIR / "walkforward_strategy.csv", index=False)
    print("\n=== walk-forward strategy legs (net 25bps/rt) ===")
    print(sdf.to_string(index=False))


if __name__ == "__main__":
    main()

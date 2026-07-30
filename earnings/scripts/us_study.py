#!/usr/bin/env python3
"""us_study.py — S&P 500 earnings event study with consensus surprises.

Spec pre-committed in study.yaml `us:` — surprise = consensus EPS surprise%
(winsorized, within-quarter z), t0 = first regular NY session opening after
the event timestamp, hold sweep gap/intraday/cc0/+2/+5/+20, market adjustment
vs ^GSPC, holdout quarters excluded from dev.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib import stats as st
from earnlib import study
from earnlib.eventmath import PricePanel
from earnlib.surprises import cross_z

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_prices import bars_frame

US_DIR = bs.EARNINGS_ROOT / "data" / "us"
NY = ZoneInfo("America/New_York")


def us_cfg(cfg: dict) -> dict:
    c = json.loads(json.dumps({k: v for k, v in cfg.items()
                               if k in ("windows", "liquidity", "beta", "refinements")}))
    c["windows"]["post"] = [[1, 2], [1, 5], [1, 20]]
    c["windows"]["profile"] = [-5, 20]
    return c


def load_events(cfg) -> pd.DataFrame:
    w = cfg["us"]["surprise_winsor_pct"]
    rows = []
    for f in sorted((US_DIR / "earnings").glob("*.json")):
        for r in json.loads(f.read_text()):
            tkr, dt_s, typ, est, act, spct = (r + [None] * 6)[:6]
            if spct is None or est is None or dt_s is None:
                continue
            dt = datetime.fromisoformat(dt_s.replace("Z", "+00:00")).astimezone(NY)
            rows.append({"symbol": f.stem, "event_dt": dt, "ann_type": typ,
                         "eps_est": est, "eps_act": act,
                         "spct": float(np.clip(spct, -w, w))})
    df = pd.DataFrame(rows)
    df = df[df["event_dt"] < datetime(2026, 7, 22, tzinfo=NY)]
    q = df["event_dt"].dt.tz_localize(None)
    df["period"] = q.dt.year.astype(str) + "-Q" + q.dt.quarter.astype(str)
    df["z_spct"] = np.nan
    for _, idx in df.groupby("period").groups.items():
        df.loc[idx, "z_spct"] = cross_z(df.loc[idx, "spct"].to_numpy(dtype=float),
                                        cfg["us"]["min_cs_pool"], 3.0)
    return df


def main() -> None:
    cfg = bs.load_config()
    ucfg = us_cfg(cfg)

    frames = []
    index_df = None
    for f in sorted((US_DIR / "prices").glob("*.json")):
        res = json.loads(f.read_text())
        sym = f.stem
        fr = bars_frame(res, sym)
        if sym == "^GSPC":
            index_df = fr
        else:
            frames.append(fr)
    prices = pd.concat(frames, ignore_index=True)
    panel = PricePanel(prices, index_df, ucfg["windows"]["min_coverage"],
                       tz=NY, session_open=time(9, 30), session_close=time(16, 0))
    print(f"US panel: {prices['symbol'].nunique()} symbols, "
          f"{len(panel.cal)} NYSE days {panel.cal.dates[0]}..{panel.cal.dates[-1]}")

    ev = load_events(cfg)
    print(f"events with consensus: {len(ev)} across {ev['symbol'].nunique()} tickers, "
          f"{ev['period'].nunique()} quarters")

    rows = []
    for _, r in ev.iterrows():
        al = panel.cal.align(r["event_dt"])
        if al is None:
            continue
        m = panel.measures(r["symbol"], al.t0, ucfg,
                           filing_day=al.filing_day if al.filed_in_session else None)
        if m is None:
            continue
        rows.append({**r.to_dict(), **m, "filed_in_session": al.filed_in_session})
    df = pd.DataFrame(rows)
    df = df[df["ar0_cc"].notna() & df["z_spct"].notna()]
    df = df[df["median_peso_volume"] >= cfg["us"]["liquidity_min_usd_volume"]]

    holdout = set(cfg["us"]["holdout_quarters"])
    dev = df[~df["period"].isin(holdout)].reset_index(drop=True)
    print(f"analyzable: {len(df)}  dev (holdout excluded): {len(dev)}")
    dev.to_parquet(bs.OUTPUTS_DIR / "us_event_windows.parquet", index=False)

    dev = study.add_terciles(dev, "z_spct")
    dev["quintile"] = np.nan
    for _, idx in dev.groupby("period").groups.items():
        v = dev.loc[idx, "z_spct"]
        if v.notna().sum() >= 30:
            dev.loc[idx, "quintile"] = pd.qcut(
                v.rank(method="first"), 5, labels=[1, 2, 3, 4, 5]).astype(float)

    tables = {}
    for meas in ("ar0_cc", "ar0_gap", "ar0_intra", "car_post2", "car_post5",
                 "car_post20", "car_pre5"):
        tables[f"us_quintile_{meas}"] = study.bucket_table(
            dev[dev["quintile"].notna()], meas, "quintile",
            (1.0, 2.0, 3.0, 4.0, 5.0))
    tables["us_regressions"] = study.regression_table(
        dev, ["ar0_cc", "ar0_gap", "ar0_intra", "car_post2", "car_post5",
              "car_post20", "car_pre5"], signal="z_spct")
    tables["us_profile"] = study.profile_table(dev, ucfg)

    peryear = []
    dy = dev.copy(); dy["year"] = dy["period"].str[:4]
    for y, sub in dy.groupby("year"):
        q5 = sub.loc[sub["quintile"] == 5.0, "ar0_cc"].mean()
        q1 = sub.loc[sub["quintile"] == 1.0, "ar0_cc"].mean()
        peryear.append({"year": y, "n": len(sub),
                        "q5_q1_bps": round((q5 - q1) * 1e4)})
    tables["us_per_year"] = pd.DataFrame(peryear)

    # permutation gate on the primary measure
    rng = np.random.default_rng(cfg["validation"]["seed"] + 2)
    d5 = dev[dev["quintile"].notna()]
    real = (d5.loc[d5["quintile"] == 5.0, "ar0_cc"].mean()
            - d5.loc[d5["quintile"] == 1.0, "ar0_cc"].mean())
    n_ge = 0
    for _ in range(500):
        sh = d5.copy()
        sh["z_spct"] = sh.groupby("period")["z_spct"].transform(
            lambda s: s.sample(frac=1, random_state=int(rng.integers(1e9))).to_numpy())
        sh["quintile"] = np.nan
        for _, idx in sh.groupby("period").groups.items():
            v = sh.loc[idx, "z_spct"]
            sh.loc[idx, "quintile"] = pd.qcut(v.rank(method="first"), 5,
                                              labels=[1, 2, 3, 4, 5]).astype(float)
        p = (sh.loc[sh["quintile"] == 5.0, "ar0_cc"].mean()
             - sh.loc[sh["quintile"] == 1.0, "ar0_cc"].mean())
        if p >= real:
            n_ge += 1
    perm_p = (n_ge + 1) / 501

    # strategy hold sweep: long Q5 / short Q1, enter open(t0), exits per horizon
    px = prices.set_index(["symbol", "date"]).sort_index()
    cal = panel.cal.dates
    srows = []
    for cost in cfg["us"]["costs_bps"]:
        for exit_h, label in [(0, "close t0"), (2, "close t+2"),
                              (5, "close t+5"), (20, "close t+20")]:
            rets = []
            for _, r in d5[d5["quintile"].isin([1.0, 5.0])].iterrows():
                sgn = 1 if r["quintile"] == 5.0 else -1
                try:
                    b0 = px.loc[(r["symbol"], r["t0"])]
                    if not b0["open"] or not np.isfinite(b0["open"]):
                        continue
                    i = np.searchsorted(cal, r["t0"])
                    j = min(i + exit_h, len(cal) - 1)
                    exit_bar = px.loc[(r["symbol"], cal[j])]
                    ret = sgn * (exit_bar["close"] / b0["open"] - 1) - cost / 1e4
                    rets.append(ret)
                except KeyError:
                    continue
            a = np.array(rets)
            t = st.mean_t(a)
            srows.append({"cost_bps": cost, "exit": label, "trades": len(a),
                          "mean_bps": round(float(a.mean()) * 1e4),
                          "t": round(t["t"], 2),
                          "hit": round(float((a > 0).mean()), 2)})
    tables["us_strategy_sweep"] = pd.DataFrame(srows)

    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, tbl in tables.items():
        tbl.to_csv(bs.RESULTS_DIR / f"{name}.csv", index=False)

    pd.set_option("display.width", 220)
    print(f"\npermutation p (Q5-Q1 ar0_cc, within-quarter shuffle x500): {perm_p:.3f}")
    print("\n-- Q5..Q1 next-day AR (cc) --")
    print(tables["us_quintile_ar0_cc"].round(4).to_string(index=False))
    print("\n-- decomposition: gap vs intraday --")
    print(tables["us_quintile_ar0_gap"].round(4).to_string(index=False))
    print(tables["us_quintile_ar0_intra"].round(4).to_string(index=False))
    print("\n-- PEAD: car_post5 / car_post20 --")
    print(tables["us_quintile_car_post5"].round(4).to_string(index=False))
    print(tables["us_quintile_car_post20"].round(4).to_string(index=False))
    print("\n-- strategy hold sweep (long Q5 / short Q1, from open t0) --")
    print(tables["us_strategy_sweep"].to_string(index=False))
    print("\n-- per year --")
    print(tables["us_per_year"].to_string(index=False))


if __name__ == "__main__":
    main()

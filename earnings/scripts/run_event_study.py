#!/usr/bin/env python3
"""run_event_study.py — the main analysis: surprise vs event-window returns.

Samples:
  (default)              development quarters, full universe
  --universe phase_a     restrict to the 17 phase-A issuers
  --new-only             replication: only companies NOT in phase A (frozen spec)
  --holdout              one-shot sacred-holdout evaluation — run ONCE, last

Outputs: outputs/event_windows.parquet, outputs/results/<tag>_*.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib import study

import pandas as pd

MEASURES = ["ar0_cc", "ar_react", "sar0_cc", "sar_react", "ar0_gap", "ar0_intra",
            "car_pre5", "car_pre10", "car_post5", "car_post20"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true")
    ap.add_argument("--universe", choices=["full", "phase_a"], default="full")
    ap.add_argument("--new-only", action="store_true",
                    help="replication on companies not in phase A")
    ap.add_argument("--no-liquidity-filter", action="store_true")
    ap.add_argument("--era", choices=["all", "modern", "historical"], default="all")
    ap.add_argument("--xbrl-dates", action="store_true",
                    help="robustness: XBRL filing timestamps only (ignore press releases)")
    args = ap.parse_args()
    which = "holdout" if args.holdout else "dev"
    if args.holdout and bs.V2:
        raise SystemExit("audit_v2.holdout_rerun is false: gate 5 is spent and "
                         "is never re-evaluated on corrected data (frozen "
                         "decision, study.yaml).")

    cfg = bs.load_config()
    panel = study.load_panel(cfg)
    all_events = study.assemble_events(
        cfg, panel, date_col="filed_dt" if args.xbrl_dates else "info_dt")
    all_events.to_parquet(bs.art_path("event_windows"), index=False)

    df = study.dev_sample(all_events, cfg, which=which,
                          universe=args.universe, new_only=args.new_only,
                          era=args.era)
    liq = None if args.no_liquidity_filter else cfg["liquidity"]["primary_min_median_peso_volume"]
    df = study.apply_liquidity(df, liq)
    df = study.add_terciles(df)
    df = study.add_quintiles(df, cfg)

    if args.new_only:
        tag = "repl"
    elif args.holdout:
        tag = "holdout"
    elif args.universe == "phase_a":
        tag = "dev"
    else:
        tag = "dev_full"
    if args.era != "all":
        tag += f"_{args.era}"
    if args.no_liquidity_filter:
        tag += "_nofilter"
    if args.xbrl_dates:
        tag += "_xbrldates"
    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"=== sample: {tag} ===")
    print(f"events: {len(df)}  companies: {df['slug'].nunique()}  "
          f"quarters: {df['period'].nunique()}")
    print(f"in-session filings: {df['filed_in_session'].mean():.0%}   "
          f"quintile-eligible events: {int(df['quintile'].notna().sum())}")

    tables: dict[str, pd.DataFrame] = {}
    for measure in MEASURES:
        tables[f"tercile_{measure}"] = study.bucket_table(
            df, measure, "tercile", (1.0, 2.0, 3.0))
    if df["quintile"].notna().any():
        for measure in ("ar0_cc", "ar_react", "sar0_cc", "car_pre5", "car_post5"):
            tables[f"quintile_{measure}"] = study.bucket_table(
                df[df["quintile"].notna()], measure, "quintile",
                (1.0, 2.0, 3.0, 4.0, 5.0))

    tables["sign_ar0_cc"] = pd.concat([
        study.sign_table(df, "ar0_cc", "s_ts", 0.0),
        study.sign_table(df, "ar0_cc", "s_ts", 1.0),
    ])
    tables["regressions"] = study.regression_table(
        df, ["ar0_cc", "ar_react", "sar0_cc", "ar0_gap",
             "car_pre5", "car_pre10", "car_post5", "car_post20"])
    tables["profile"] = study.profile_table(df, cfg)
    tables["tradability"] = study.tradability_table(df)

    from earnlib import stats as st
    hits = st.hit_rate(df["s_ts"].to_numpy(), df["ar0_cc"].to_numpy())
    tables["hit_rates"] = pd.DataFrame([{"signal": "s_ts sign vs ar0_cc", **hits}])

    peryear = []
    dfy = df.copy()
    dfy["year"] = dfy["period"].str[:4]
    for y, sub in dfy.groupby("year"):
        peryear.append({"year": y, "n": len(sub),
                        "t3_t1_bps": study.spread_t3t1(sub, "ar0_cc") * 1e4})
    tables["per_year"] = pd.DataFrame(peryear)

    for name, tbl in tables.items():
        tbl.to_csv(bs.RESULTS_DIR / f"{tag}_{name}.csv", index=False)

    pd.set_option("display.width", 220)
    fmt = lambda t: t.round(4).to_string(index=False)
    print("\n-- AR0_cc by tercile --")
    print(fmt(tables["tercile_ar0_cc"]))
    if "quintile_ar0_cc" in tables:
        print("\n-- AR0_cc by quintile --")
        print(fmt(tables["quintile_ar0_cc"]))
        print("\n-- SAR0_cc (vol-standardized) by quintile --")
        print(fmt(tables["quintile_sar0_cc"]))
    print("\n-- AR_react (reaction-window fix) by tercile --")
    print(fmt(tables["tercile_ar_react"]))
    print("\n-- Leakage: CAR_pre5 by tercile --")
    print(fmt(tables["tercile_car_pre5"]))
    print("\n-- Regressions (quarter FE, clustered t) --")
    print(fmt(tables["regressions"]))
    print("\n-- Tradability decomposition --")
    print(tables["tradability"].round(2).to_string(index=False))
    print("\n-- Per-year stability --")
    print(tables["per_year"].round(1).to_string(index=False))
    print("\n-- Hit rate --")
    print(fmt(tables["hit_rates"]))


if __name__ == "__main__":
    main()

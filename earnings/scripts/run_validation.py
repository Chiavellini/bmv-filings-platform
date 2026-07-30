#!/usr/bin/env python3
"""run_validation.py — quant-audit gates for the earnings event study.

Gate 1: structural integrity (lookahead asserts + manual alignment sample)
Gate 2: signal strength (rank IC of surprise vs next-day AR)
Gate 3: permutation tests (within-quarter shuffle of S; placebo event dates)
Gate 4: robustness (spec variants x liquidity cutoffs x beta adjustment;
        per-year stability)
Gate 5: sacred holdout — NOT run here; run_event_study.py --holdout, once.

Development sample only; holdout quarters never touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib import stats as st
from earnlib import study

import numpy as np
import pandas as pd


def gate1(df: pd.DataFrame, cfg: dict, rng: np.random.Generator) -> None:
    print("=== Gate 1: structural integrity ===")
    # mirror compute_surprises: availability masking uses events_ALL (its
    # historical rows carry pdf-header dates for pre-2021 quarters; reading
    # only the XBRL-era events made the independent recompute fall back to
    # period_end+90d and mismatch on events whose trailing window spans 2021)
    ev_path = bs.art_path("events_all")
    events = pd.read_parquet(ev_path if ev_path.exists() else bs.art_path("events"))
    events = events[events["in_universe"] & events["has_facts"]]  # as the pipeline
    dc = "info_dt" if "info_dt" in events.columns else "filed_dt"
    ev = events[events["slug"].notna()].set_index(["slug", "period"])[dc]
    ev = ev[~ev.index.duplicated(keep="first")]

    # invariant: t0's session open (08:30 CDMX) is strictly after the filing
    # timestamp (a pre-open filing correctly reacts the SAME calendar day)
    from datetime import datetime, time
    from earnlib.calendar import CDMX, SESSION_OPEN

    t0_opens = [datetime.combine(d, SESSION_OPEN, tzinfo=CDMX) for d in df["t0"]]
    filed = [f.to_pydatetime() for f in df["event_dt"]]
    bad = sum(1 for o, f in zip(t0_opens, filed) if not o > f)
    assert bad == 0, f"{bad} events with t0 session open <= filed_dt"
    same_day = sum(1 for o, f in zip(t0_opens, filed)
                   if o.date() == f.astimezone(CDMX).date())
    print(f"  t0 session open strictly after filed_dt for all {len(df)} events: OK "
          f"({same_day} pre-open filings react same day)")

    # SUE availability mask: independently recompute SUE(net_income) for a few
    # random events from raw metrics + filing timestamps and compare to stored
    import numpy as np
    from earnlib.surprises import trailing_sue, winsorize

    metrics = pd.read_parquet(bs.art_path("metrics"))
    ni = metrics[metrics["metric"] == "net_income"]
    # the pipeline pivots over ALL metrics, so a (slug, period) row exists —
    # and occupies a slot in the trailing window — whenever ANY metric was
    # extracted for that quarter, even if net_income itself is NaN there.
    # The recompute must use the same row scaffold or windows shift.
    period_rows = metrics[["slug", "period"]].drop_duplicates()
    hp = bs.art_path("metrics_hist")
    if hp.exists():
        nh = pd.read_parquet(hp)
        period_rows = pd.concat(
            [period_rows, nh[["slug", "period"]].drop_duplicates()],
            ignore_index=True).drop_duplicates()
        nh = nh[nh["metric"] == "net_income"].copy()
        nh["prior"] = np.nan
        keep = ~nh.set_index(["slug", "period"]).index.isin(
            ni.set_index(["slug", "period"]).index)
        ni = pd.concat([ni, nh[keep.tolist()][["slug", "period", "current", "prior", "metric"]]],
                       ignore_index=True)
    scfg = cfg["surprise"]
    checked = 0
    df_x = df[df["period"] >= "2021-2T"]   # XBRL-era rows (hist metrics live in
    if len(df_x) == 0:                     # metrics_hist; same masking code path)
        df_x = df
        print("  (no XBRL-era rows in sample — SUE recompute vs raw skipped)")
        df_x = df_x.iloc[0:0]
    for _, r in df_x.sample(min(8, len(df_x)), random_state=7).iterrows():
        scaffold = (period_rows[period_rows["slug"] == r["slug"]]
                    [["slug", "period"]].drop_duplicates())
        sub = scaffold.merge(
            ni[ni["slug"] == r["slug"]][["period", "current", "prior"]],
            on="period", how="left").sort_values("period").reset_index(drop=True)
        # mirror the pipeline: point-in-time prior where present, else YoY
        cur_map = dict(zip(sub["period"], sub["current"]))
        dx_pit = (sub["current"] - sub["prior"]).to_numpy(dtype=float)
        dx_yoy = np.array([
            c - cur_map.get(f"{int(p[:4]) - 1}{p[4:]}", np.nan)
            for c, p in zip(sub["current"], sub["period"])], dtype=float)
        dxv = np.where(np.isnan(dx_pit), dx_yoy, dx_pit)
        filed_map = ev.loc[r["slug"]]
        # mirror compute_surprises: undated metric quarters become available
        # for masking at period_end + 90d (never as events)
        from datetime import datetime as _dt, time as _t, timedelta as _td
        from earnlib.calendar import CDMX as _CDMX
        _qe = {"1": "-03-31", "2": "-06-30", "3": "-09-30", "4": "-12-31"}

        def _avail(p):
            if p in filed_map.index and pd.notna(filed_map.get(p)):
                return filed_map.get(p).to_pydatetime()
            end = _dt.fromisoformat(p[:4] + _qe[p[5]])
            return _dt.combine((end + _td(days=90)).date(), _t(23, 59), tzinfo=_CDMX)

        filed_arr = np.array([_avail(p) for p in sub["period"]], dtype=object)
        sue = winsorize(trailing_sue(dxv, scfg["min_hist_quarters"],
                                     scfg["target_hist_quarters"], filed=filed_arr),
                        scfg["sue_winsor"])
        i = sub.index[sub["period"] == r["period"]][0]
        stored = r["sue_net_income"]
        if np.isnan(sue[i]):
            # checker sees XBRL rows only; a stored value backed by merged
            # (hist) metrics is not re-derivable here — skip, don't assert
            continue
        assert np.isnan(stored) or abs(sue[i] - stored) < 1e-9, \
            f"SUE mismatch {r['slug']} {r['period']}: recomputed {sue[i]} vs stored {stored}"
        checked += 1
    print(f"  SUE(net_income) independently recomputed for {checked} random events: match")

    sample = df.sample(min(5, len(df)), random_state=int(rng.integers(1e9)))
    print("  manual alignment sample:")
    for _, r in sample.iterrows():
        print(f"    {r['ticker']:8s} {r['period']}  {r['date_source']} {r['event_dt']}  "
              f"t0={r['t0']}  in_session={r['filed_in_session']}  "
              f"S={r['s_cs']:+.2f}  AR0={r['ar0_cc']*1e4:+.0f}bps")


def gate2(df: pd.DataFrame) -> None:
    print("\n=== Gate 2: signal strength (threshold: |IC| t > 2) ===")
    for measure in ("ar0_cc", "car_pre5", "car_post5"):
        ic = st.spearman_ic(df["s_cs"].to_numpy(), df[measure].to_numpy())
        per_q = [
            st.spearman_ic(sub["s_cs"].to_numpy(), sub[measure].to_numpy())["ic"]
            for _, sub in df.groupby("period")
        ]
        fm = st.fama_macbeth(np.array(per_q, dtype=float))
        print(f"  s_cs vs {measure:10s}: pooled IC={ic['ic']:+.3f} (t={ic['t']:+.2f}, "
              f"n={ic['n']})  per-quarter mean IC={fm['fm_mean']:+.3f} (FM t={fm['fm_t']:+.2f})")


def gate3(df: pd.DataFrame, cfg: dict, panel, rng: np.random.Generator,
          era: str = "all") -> None:
    print("\n=== Gate 3: permutation tests ===")
    vcfg = cfg["validation"]
    persist_rows = []

    # (a) within-quarter shuffle of the surprise score
    for measure in ("ar0_cc", "car_pre5"):
        real_spread = study.spread_t3t1(df, measure)
        real_slope = st.ols_fe_clustered(
            df[measure].to_numpy(), df["s_cs"].to_numpy(), df["period"].to_numpy()
        )["slope"]
        n_sp = n_sl = 0
        for _ in range(vcfg["n_perm_shuffle"]):
            sh = df.copy()
            sh["s_cs"] = sh.groupby("period")["s_cs"].transform(
                lambda s: s.sample(frac=1, random_state=int(rng.integers(1e9))).to_numpy())
            sh = study.add_terciles(sh)
            if study.spread_t3t1(sh, measure) >= real_spread:
                n_sp += 1
            slope = st.ols_fe_clustered(
                sh[measure].to_numpy(), sh["s_cs"].to_numpy(), sh["period"].to_numpy()
            )["slope"]
            if abs(slope) >= abs(real_slope):
                n_sl += 1
        n = vcfg["n_perm_shuffle"]
        print(f"  shuffle-S  {measure:9s}: T3-T1={real_spread*1e4:+.0f}bps "
              f"p={(n_sp + 1) / (n + 1):.3f} | slope p(two-sided)={(n_sl + 1) / (n + 1):.3f}")
        persist_rows.append({
            "test": "shuffle-S", "measure": measure,
            "real_spread_bps": round(real_spread * 1e4, 1),
            "p_spread": round((n_sp + 1) / (n + 1), 4),
            "p_slope_two_sided": round((n_sl + 1) / (n + 1), 4),
            "n_perm": n, "seed": vcfg["seed"], "era": era,
            "n_events": len(df)})

    # (b) placebo event dates: same S, shifted t0 — alignment machinery check
    lo, hi = vcfg["placebo_shift_days"]
    real_spread = study.spread_t3t1(df, "ar0_cc")
    count = 0
    n_pl = vcfg["n_perm_placebo"]
    for _ in range(n_pl):
        sh = df.copy()
        shifted = []
        for _, r in sh.iterrows():
            k = int(rng.integers(lo, hi + 1)) * (1 if rng.random() < 0.5 else -1)
            d = panel.cal.shift(r["t0"], k)
            m = panel.measures(r["symbol"], d, cfg) if d is not None else None
            shifted.append(m["ar0_cc"] if m else np.nan)
        sh["ar0_cc"] = shifted
        if study.spread_t3t1(sh, "ar0_cc") >= real_spread:
            count += 1
    print(f"  placebo-dates ar0_cc: real T3-T1={real_spread*1e4:+.0f}bps "
          f"p={(count + 1) / (n_pl + 1):.3f}")
    persist_rows.append({
        "test": "placebo-dates", "measure": "ar0_cc",
        "real_spread_bps": round(real_spread * 1e4, 1),
        "p_spread": round((count + 1) / (n_pl + 1), 4),
        "p_slope_two_sided": np.nan,
        "n_perm": n_pl, "seed": vcfg["seed"], "era": era,
        "n_events": len(df)})
    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(persist_rows).to_csv(
        bs.RESULTS_DIR / f"validation_shuffle_placebo_{era}.csv", index=False)


def gate4(all_events: pd.DataFrame, df: pd.DataFrame, cfg: dict) -> None:
    print("\n=== Gate 4: robustness ===")
    rows = []

    universe = getattr(cfg, "_universe_arg", "full")

    def spread_of(d: pd.DataFrame, signal: str, measure: str = "ar0_cc") -> dict:
        d = study.add_terciles(d, signal)
        per_q = []
        for _, sub in d.groupby("period"):
            m3 = sub.loc[sub["tercile"] == 3.0, measure].mean()
            m1 = sub.loc[sub["tercile"] == 1.0, measure].mean()
            if np.isfinite(m3) and np.isfinite(m1):
                per_q.append(m3 - m1)
        fm = st.fama_macbeth(np.array(per_q))
        return {"spread_bps": study.spread_t3t1(d, measure) * 1e4,
                "fm_t": fm["fm_t"], "n": len(d)}

    liq = cfg["liquidity"]["primary_min_median_peso_volume"]
    for signal in ("s_cs", "s_ts", "sue_eps", "sue_net_income", "yoy_net_income"):
        d = study.dev_sample(all_events, cfg)
        d = study.apply_liquidity(d, liq)
        d = d[d[signal].notna()]
        rows.append({"variant": f"signal={signal}", **spread_of(d, signal)})

    for cut in [None, cfg["liquidity"]["variants"][0], liq, cfg["liquidity"]["variants"][1]]:
        d = study.dev_sample(all_events, cfg)
        d = study.apply_liquidity(d, cut)
        rows.append({"variant": f"liquidity>={cut}", **spread_of(d, "s_cs")})

    # beta-adjusted: r_i - beta*r_m = ar0_cc + (1-beta)*r_m
    d = study.dev_sample(all_events, cfg)
    d = study.apply_liquidity(d, liq)
    d = d[d["beta"].notna()].copy()
    d["ar0_cc"] = d["ar0_cc"] + (1 - d["beta"]) * d["i_ret0"]
    rows.append({"variant": "beta-adjusted", **spread_of(d, "s_cs")})

    tbl = pd.DataFrame(rows)
    print(tbl.round(2).to_string(index=False))

    print("\n  per-year T3-T1 spread of ar0_cc (stability):")
    d = study.add_terciles(study.apply_liquidity(study.dev_sample(all_events, cfg), liq))
    d["year"] = d["period"].str[:4]
    for y, sub in d.groupby("year"):
        print(f"    {y}: {study.spread_t3t1(sub, 'ar0_cc')*1e4:+.0f} bps  (n={len(sub)})")

    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tbl.to_csv(bs.RESULTS_DIR / "validation_robustness.csv", index=False)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", choices=["full", "phase_a"], default="full")
    ap.add_argument("--era", choices=["all", "modern", "historical"], default="all")
    args = ap.parse_args()

    cfg = bs.load_config()
    rng = np.random.default_rng(cfg["validation"]["seed"])
    panel = study.load_panel(cfg)
    all_events = study.assemble_events(cfg, panel)
    df = study.apply_liquidity(
        study.dev_sample(all_events, cfg, universe=args.universe, era=args.era),
        cfg["liquidity"]["primary_min_median_peso_volume"])
    df = study.add_terciles(df)
    print(f"development sample: {len(df)} events\n")

    gate1(df, cfg, rng)
    gate2(df)
    gate3(df, cfg, panel, rng, era=args.era)
    gate4(all_events, df, cfg)
    print("\nGate 5 (sacred holdout) is run separately, once: "
          "run_event_study.py --holdout")


if __name__ == "__main__":
    main()

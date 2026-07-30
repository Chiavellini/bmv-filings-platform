#!/usr/bin/env python3
"""run_phase_h.py — ONE evaluation run of the pre-committed phase-H
candidates (study.yaml phase_h, frozen 2026-07-28 before this run).

Per-company "good report" pillars — revenue growth, profitability, debt —
each vs the company's own history, every SUE availability-masked, evaluated
on the CORRECTED (v2) modern dev sample with the phase-E machinery.
beat_desk is printed as a diagnostic only (vintages exist only from 2026-1T).

Adoption rule (frozen): dev FM t >= incumbent + 0.5 AND shuffle p < 0.05 AND
economic rationale; max 1 adoption from H1-H4. Final arbiter: one-shot
2026-3T walk-forward. The modern sacred holdout is NOT touched.

Outputs: results_v2/phaseh_candidates.csv, phaseh_pillar_correlations.csv,
phaseh_doublesort_debt.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib import stats as st
from earnlib import study
from earnlib.surprises import availability_array, trailing_sue, winsorize, cross_z

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_refinements import add_margin_sue, eval_signal


def masked_sue_map(series: pd.DataFrame, value_col: str, scfg: dict,
                   avail: pd.Series) -> dict:
    """(slug, period) -> winsorized masked SUE of YoY dX of value_col.
    dX(q) = x(q) - x(same quarter, prior year)."""
    out = {}
    for slug, sub in series.groupby("slug"):
        sub = sub.sort_values("period").reset_index(drop=True)
        vmap = dict(zip(sub["period"], sub[value_col]))

        def prev_year(p):
            return f"{int(p[:4]) - 1}{p[4:]}"

        dx = np.array([vmap[p] - vmap.get(prev_year(p), np.nan)
                       for p in sub["period"]], dtype=float)
        filed = availability_array(
            sub["period"], [avail.get((slug, p)) for p in sub["period"]])
        sue = winsorize(trailing_sue(dx, scfg["min_hist_quarters"],
                                     scfg["target_hist_quarters"], filed=filed),
                        scfg["sue_winsor"])
        for p, v in zip(sub["period"], sue):
            out[(slug, p)] = v
    return out


def add_cross_z(df: pd.DataFrame, col: str, scfg: dict) -> pd.Series:
    out = pd.Series(np.nan, index=df.index)
    for _, idx in df.groupby("period").groups.items():
        out.loc[idx] = cross_z(df.loc[idx, col].to_numpy(dtype=float),
                               scfg["min_cs_pool"], scfg["z_winsor"])
    return out


def main() -> None:
    cfg = bs.load_config()
    scfg = cfg["surprise"]
    hcfg = cfg["phase_h"]
    rng = np.random.default_rng(cfg["validation"]["seed"] + hcfg["seed_offset"])

    panel = study.load_panel(cfg)
    ew = study.assemble_events(cfg, panel)
    df = study.apply_liquidity(
        study.dev_sample(ew, cfg, era="modern"),
        cfg["liquidity"]["primary_min_median_peso_volume"]).copy()
    print(f"modern dev sample (corrected inputs): {len(df)} events")

    avail = study.availability_map()

    # ---- pillar 1: revenue growth surprise (already masked upstream) ----
    df["rev_growth_sue"] = df["sue_revenue"]

    # ---- pillar 2: margin SUE, issue-A-corrected (masked) ----
    df = add_margin_sue(df, cfg)          # adds margin_sue + margin_sue_z

    # ---- pillar 3: debt signal from the KPI panel ----
    kpi = pd.read_parquet(bs.art_path("kpi_panel"))
    nd = kpi[["slug", "period", "nd_to_ebitda_ttm"]].dropna()
    nd_map = masked_sue_map(nd, "nd_to_ebitda_ttm", scfg, avail)
    df["debt_signal"] = [-1.0 * nd_map.get((s, p), np.nan)
                         for s, p in zip(df["slug"], df["period"])]

    # ---- pillar 4: revenue growth acceleration vs own trajectory ----
    sp = pd.read_parquet(bs.art_path("surprises"))
    yo = sp[["slug", "period", "yoy_revenue"]].dropna().sort_values(["slug", "period"])
    yo["trail4"] = yo.groupby("slug")["yoy_revenue"].transform(
        lambda s: s.shift(1).rolling(4, min_periods=2).mean())
    yo["accel"] = yo["yoy_revenue"] - yo["trail4"]
    am = yo.set_index(["slug", "period"])["accel"]
    df["accel_rev_raw"] = [am.get((s, p), np.nan)
                           for s, p in zip(df["slug"], df["period"])]

    # cross-sectional z's
    for col in ("rev_growth_sue", "margin_sue", "debt_signal", "accel_rev_raw"):
        df[f"z_{col}"] = add_cross_z(df, col, scfg)

    # ---- candidates (frozen combinations) ----
    w = hcfg["overlay_weight"]
    z3 = df[["z_rev_growth_sue", "z_margin_sue", "z_debt_signal"]].to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        df["H1_quality3"] = np.where(np.isfinite(z3).sum(axis=1) >= 2,
                                     np.nanmean(z3, axis=1), np.nan)
    z2 = df[["z_rev_growth_sue", "z_margin_sue"]].to_numpy(dtype=float)
    with np.errstate(invalid="ignore"):
        df["H2_quality2"] = np.where(np.isfinite(z2).sum(axis=1) >= 2,
                                     np.nanmean(z2, axis=1), np.nan)
    df["H3_incumbent_plus_debt"] = df["s_cs"] + w * df["z_debt_signal"].fillna(0)
    df["H4_accel_overlay"] = df["s_cs"] + w * df["z_accel_rev_raw"].fillna(0)

    # ---- evaluation, once ----
    candidates = ["s_cs", "H1_quality3", "H2_quality2",
                  "H3_incumbent_plus_debt", "H4_accel_overlay"]
    rows = [eval_signal(df, c, rng) for c in candidates]
    cand = pd.DataFrame(rows)
    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    header = ("# phase H one-shot dev evaluation; 4 candidates => "
              "Bonferroni-effective alpha 0.0125 at nominal 0.05; adoption "
              "rule: FM t >= incumbent+0.5 AND shuffle p<0.05\n")
    out_path = bs.RESULTS_DIR / "phaseh_candidates.csv"
    with open(out_path, "w") as fh:
        fh.write(header)
        cand.to_csv(fh, index=False)
    print("\n=== phase H candidates (incumbent = s_cs) ===")
    print(cand.to_string(index=False))

    # ---- pillar independence (gate 2) ----
    corr_rows = []
    for col in ("rev_growth_sue", "margin_sue", "debt_signal", "accel_rev_raw"):
        sub = df[[col, "s_cs", "car_pre10", "ar0_cc"]].dropna()
        ic = st.spearman_ic(df[col].to_numpy(dtype=float),
                            df["ar0_cc"].to_numpy(dtype=float))
        corr_rows.append({
            "pillar": col, "n": len(sub),
            "corr_vs_s_cs": round(float(sub[col].corr(sub["s_cs"])), 3),
            "corr_vs_mom10": round(float(sub[col].corr(sub["car_pre10"])), 3),
            "ic_vs_ar0_cc": round(ic["ic"], 3), "ic_t": round(ic["t"], 2)})
    pc = pd.DataFrame(corr_rows)
    pc.to_csv(bs.RESULTS_DIR / "phaseh_pillar_correlations.csv", index=False)
    print("\n=== pillar independence ===")
    print(pc.to_string(index=False))

    # ---- double sort: S tercile x debt tercile ----
    d = study.add_terciles(df[df["debt_signal"].notna()].copy(), "s_cs")
    d["debt_ter"] = np.nan
    for _, idx in d.groupby("period").groups.items():
        v = d.loc[idx, "debt_signal"]
        if v.notna().sum() >= 6:
            d.loc[idx, "debt_ter"] = pd.qcut(v.rank(method="first"), 3,
                                             labels=[1, 2, 3]).astype(float)
    mat = d.pivot_table(index="tercile", columns="debt_ter",
                        values="ar0_cc", aggfunc="mean") * 1e4
    mat.round(0).to_csv(bs.RESULTS_DIR / "phaseh_doublesort_debt.csv")
    print("\n=== double sort: S tercile x debt-signal tercile (mean AR0 bps) ===")
    print(mat.round(0).to_string())

    # ---- beat_desk diagnostic (n too small for anything else) ----
    if "beat_revenue_pct" in kpi.columns:
        bd = kpi[kpi["beat_revenue_pct"].notna()][
            ["slug", "period", "beat_revenue_pct", "beat_ebitda_pct",
             "beat_net_income_pct"]]
        print("\n=== beat_desk DIAGNOSTIC ONLY (n too small; vintages start "
              "2026-1T; never used for selection) ===")
        print(bd.to_string(index=False))

    # ---- adoption verdict per frozen rule ----
    inc_t = cand.loc[cand["signal"] == "s_cs", "fm_t"].iloc[0]
    print("\n=== adoption verdict (frozen rule) ===")
    adopted = None
    for _, r in cand[cand["signal"] != "s_cs"].iterrows():
        if r.get("fm_t") is not None and np.isfinite(r.get("fm_t", np.nan)):
            ok = (r["fm_t"] >= inc_t + 0.5) and (r["shuffle_p"] < 0.05)
            print(f"  {r['signal']:24s} fm_t={r['fm_t']:+.2f} "
                  f"(bar {inc_t + 0.5:+.2f}) shuffle_p={r['shuffle_p']:.3f} "
                  f"-> {'CANDIDATE' if ok else 'no'}")
            if ok and adopted is None:
                adopted = r["signal"]
    if adopted:
        print(f"\n  ADOPTED (max 1): {adopted} — pending economic-rationale "
              "review; final arbiter = one-shot 2026-3T walk-forward.")
    else:
        print("\n  NO candidate clears the bar — incumbent stands; the "
              "2026-3T walk-forward will test the incumbent alone.")


if __name__ == "__main__":
    main()

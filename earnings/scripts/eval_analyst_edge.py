#!/usr/bin/env python3
"""eval_analyst_edge.py — decompose the analyst's edge: can the model's
feature set reconstruct their calls, where do they beat the model, and is
analyst-like precision attainable from any model-observable rule?

POST-HOC-MOTIVATED; spec + interpretation rules FROZEN in study.yaml
`analyst_edge` and the REPORT.md ledger BEFORE this script first ran.
DIAGNOSTIC ONLY. Dev quarters only; holdout never joined.

Outputs: results_v3/analyst_edge_{univariate,predict_auc,contrasts,
clustering,abstain_grid,abstain_perm,margin_vs_scs,case_list}.csv
+ outputs/ANALYST_EDGE.md (embeds analyst_edge_case_reads.csv when present)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib.mdutil import md_table
from earnlib import study

import numpy as np
import pandas as pd
from scipy import stats as sps

sys.path.insert(0, str(bs.EARNINGS_ROOT / "scripts"))
from run_refinements import add_margin_sue  # noqa: E402
from eval_simple_analyst import sector_map  # noqa: E402

FEATURES = ["s_cs", "s_ts", "z_sue_net_income", "z_sue_revenue",
            "z_sue_ebitda", "margin_sue_z", "n_components", "car_pre5",
            "car_pre10", "ar_dm1", "prevol_ratio_w", "sigma_pre", "beta",
            "filing_lag_days", "log_liquidity", "filed_in_session_i"]
KPI_DESCRIPTIVE = ["ebitda_margin_yoy_pp", "net_margin_yoy_pp",
                   "rev_accel_pp", "nd_to_ebitda_ttm", "net_debt_yoy_chg"]
OUTCOME_COLS = {"ar0_cc", "ar0_gap", "ar0_intra", "ar_react", "sar0_cc",
                "sar_react", "car_post5", "car_post20", "ar_filing_day",
                "i_ret0"} | {f"ar_d{k}" for k in range(0, 6)}
assert set(FEATURES).isdisjoint(OUTCOME_COLS)
A3_FEATURES = ["s_ts", "margin_sue_z", "sigma_pre"]   # PIT only, frozen
MIN_CELL = 15


def bh_fdr(p: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg q-values (NaN-aware)."""
    p = np.asarray(p, dtype=float)
    q = np.full_like(p, np.nan)
    ok = ~np.isnan(p)
    pv = p[ok]
    n = len(pv)
    order = np.argsort(pv)
    ranked = pv[order] * n / (np.arange(n) + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.clip(ranked, 0, 1)
    q[ok] = out
    return q


def build_join(cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(matched analyst frame n=73, full dev feature frame n~484)."""
    ew = pd.read_parquet(bs.art_path("event_windows"))
    ew = add_margin_sue(ew, cfg)                      # full-frame cross_z pool
    dev = study.dev_sample(ew, cfg, which="dev")
    dev = study.add_terciles(dev)
    # NOTE: no liquidity filter — this is an information study; the matched
    # frame must reproduce eval_analyst's canonical 73/67 join. A3 applies
    # the liquidity filter separately (tradable universe).

    dev = dev.rename(columns={"ar_d-1": "ar_dm1"})
    lo, hi = dev["prevol_ratio"].quantile([0.01, 0.99])
    dev["prevol_ratio_w"] = dev["prevol_ratio"].clip(lo, hi)
    dev["log_liquidity"] = np.log10(dev["median_peso_volume"])
    dev["filed_in_session_i"] = dev["filed_in_session"].astype(int)

    kpi = pd.read_parquet(bs.OUTPUTS_DIR / "kpi_panel.parquet")
    dev = dev.merge(kpi[["slug", "period"] + KPI_DESCRIPTIVE],
                    on=["slug", "period"], how="left")

    pred = pd.read_parquet(bs.OUTPUTS_DIR / "analyst_predictions.parquet")
    pred = pred[pred["slug"].notna()]
    j = pred.merge(dev, on=["slug", "period"], how="inner",
                   validate="many_to_one", suffixes=("_pred", ""))
    holdout_q = set(cfg["holdout"]["quarters"]) | set(
        cfg.get("phase_d", {}).get("historical_holdout_quarters", []))
    assert not set(j["period"]) & holdout_q
    j["hit"] = (j["direction"] == 1) == (j["ar0_cc"] > 0)
    j["model_dir_ts"] = np.sign(j["s_ts"]).astype(int)
    j["sector"] = j["ticker"].map(sector_map())
    return j, dev


def loqo_auc(X: pd.DataFrame, y: np.ndarray, groups: np.ndarray,
             rng=None) -> float:
    """Leave-one-quarter-out pooled OOF AUC (L2 logistic pipeline)."""
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    oof = np.full(len(y), np.nan)
    for q in np.unique(groups):
        te = groups == q
        tr = ~te
        if y[tr].min() == y[tr].max():
            continue
        pipe = make_pipeline(SimpleImputer(strategy="median"),
                             StandardScaler(),
                             LogisticRegression(C=1.0, max_iter=1000))
        pipe.fit(X[tr], y[tr])
        oof[te] = pipe.predict_proba(X[te])[:, 1]
    ok = ~np.isnan(oof)
    if y[ok].min() == y[ok].max():
        return np.nan
    return float(roc_auc_score(y[ok], oof[ok]))


def perm_p_auc(X, y, groups, real, n_perm, rng) -> float:
    ge = 0
    idx_by_q = {q: np.where(groups == q)[0] for q in np.unique(groups)}
    for _ in range(n_perm):
        yp = y.copy()
        for _, idx in idx_by_q.items():
            if len(idx) > 1:
                yp[idx] = rng.permutation(yp[idx])
        a = loqo_auc(X, yp, groups)
        if np.isfinite(a) and a >= real:
            ge += 1
    return (ge + 1) / (n_perm + 1)


def univariate(d: pd.DataFrame, split_col: str, pos, neg,
               feats: list[str]) -> pd.DataFrame:
    rows = []
    for f in feats:
        a = d[d[split_col] == pos][f].dropna()
        b = d[d[split_col] == neg][f].dropna()
        if len(a) < 5 or len(b) < 5:
            rows.append({"feature": f, "median_pos": np.nan,
                         "median_neg": np.nan, "p_mwu": np.nan,
                         "n_pos": len(a), "n_neg": len(b)})
            continue
        p = sps.mannwhitneyu(a, b, alternative="two-sided").pvalue
        rows.append({"feature": f, "median_pos": round(float(a.median()), 4),
                     "median_neg": round(float(b.median()), 4),
                     "p_mwu": float(p), "n_pos": len(a), "n_neg": len(b)})
    out = pd.DataFrame(rows)
    out["q_fdr"] = bh_fdr(out["p_mwu"].to_numpy()).round(4)
    out["p_mwu"] = out["p_mwu"].round(4)
    return out


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (np.nan, np.nan)
    from math import sqrt
    z, p = 1.96, k / n
    den = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / den, (c + h) / den)


def abstain_cells(dev: pd.DataFrame, sig_med: float) -> pd.DataFrame:
    rows = []
    d = dev[dev["s_ts"].notna() & (dev["s_ts"] != 0)]
    for thr in (1.0, 1.5, 2.0):
        for gate in ("any", "margin_agree"):
            for band in ("all", "low_vol"):
                sub = d[d["s_ts"].abs() >= thr]
                if gate == "margin_agree":
                    sub = sub[np.sign(sub["margin_sue_z"]) ==
                              np.sign(sub["s_ts"])]
                if band == "low_vol":
                    sub = sub[sub["sigma_pre"] <= sig_med]
                n = len(sub)
                k = int(((sub["s_ts"] > 0) == (sub["ar0_cc"] > 0)).sum())
                lo, hi = wilson(k, n)
                rows.append({"thr": thr, "gate": gate, "vol": band, "n": n,
                             "hits": k,
                             "hit_pct": round(100 * k / n, 1) if n else np.nan,
                             "wilson_lo": round(100 * lo, 1) if n else np.nan,
                             "wilson_hi": round(100 * hi, 1) if n else np.nan,
                             "coverage_pct": round(100 * n / len(d), 1)})
    return pd.DataFrame(rows)


def main() -> None:
    assert bs.V3, "set EARNINGS_V3=1"
    cfg = bs.load_config()
    ecfg = cfg["analyst_edge"]
    rng = np.random.default_rng(cfg["validation"]["seed"]
                                + int(ecfg["seed_offset"]))
    n_perm = cfg["validation"]["n_perm_shuffle"]

    j, dev = build_join(cfg)
    dev_liq = study.apply_liquidity(
        dev, cfg["liquidity"]["primary_min_median_peso_volume"])
    d = j[j["direction"] != 0].copy()
    n_fs = int((d["strength"] == 1.0).sum())
    print(f"matched {len(j)} / directional {len(d)} / full-strength {n_fs}")
    assert len(j) == 73 and len(d) == 67 and n_fs == 27
    misses = set(map(tuple, d[(d["strength"] == 1.0) & ~d["hit"]]
                     [["slug", "period"]].to_numpy()))
    assert misses == {("oma", "2025-1T"), ("chedraui", "2025-1T")}, misses

    # ---- A1: univariate + predict-the-analyst
    uni_dir = univariate(d, "direction", 1, -1, FEATURES + KPI_DESCRIPTIVE)
    uni_dir.insert(0, "comparison", "bullish_vs_bearish")
    d["fs"] = (d["strength"] == 1.0).astype(int)
    uni_fs = univariate(d, "fs", 1, 0, FEATURES + KPI_DESCRIPTIVE)
    uni_fs.insert(0, "comparison", "full_vs_soft")
    uni = pd.concat([uni_dir, uni_fs], ignore_index=True)
    uni.to_csv(bs.RESULTS_DIR / "analyst_edge_univariate.csv", index=False)

    groups = d["period"].to_numpy()
    X = d[FEATURES]
    auc_rows = []
    for target, y in (("direction (bullish=1)",
                       (d["direction"] == 1).to_numpy().astype(int)),
                      ("full_strength", d["fs"].to_numpy())):
        real = loqo_auc(X, y, groups)
        p = perm_p_auc(X, y.copy(), groups, real, n_perm, rng)
        verdict = ("features RECONSTRUCT the target"
                   if np.isfinite(real) and real >= 0.65 and p < 0.05 else
                   "analyst information is OUTSIDE this feature set")
        auc_rows.append({"target": target, "oof_auc": round(real, 3),
                         "perm_p": round(p, 4), "n": len(y),
                         "verdict_frozen_rule": verdict})
        print(f"A1 {target}: AUC {real:.3f} p {p:.4f} -> {verdict}")
    aucs = pd.DataFrame(auc_rows)
    aucs.to_csv(bs.RESULTS_DIR / "analyst_edge_predict_auc.csv", index=False)

    # ---- B2: analyst-right/model-wrong contrasts
    dd = d[d["model_dir_ts"] != 0].copy()
    dd["model_hit"] = (dd["model_dir_ts"] == 1) == (dd["ar0_cc"] > 0)
    dd["group"] = np.select(
        [dd["hit"] & ~dd["model_hit"], dd["hit"] & dd["model_hit"],
         ~dd["hit"] & dd["model_hit"]],
        ["A+M-", "both_right", "A-M+"], default="both_wrong")
    con = univariate(dd[dd["group"].isin(["A+M-", "both_right"])],
                     "group", "A+M-", "both_right",
                     FEATURES + KPI_DESCRIPTIVE)
    con.insert(0, "comparison", "Aright_Mwrong_vs_bothright")
    con.to_csv(bs.RESULTS_DIR / "analyst_edge_contrasts.csv", index=False)
    clus = (dd.groupby(["group", "sector"]).size().rename("n").reset_index())
    clus.to_csv(bs.RESULTS_DIR / "analyst_edge_clustering.csv", index=False)
    grp_counts = dd["group"].value_counts().to_dict()
    print("B2 groups:", grp_counts)

    # ---- A3: abstain sweep + max-statistic null (tradable universe)
    dv = dev_liq[dev_liq["s_ts"].notna()]
    sig_med = float(dv["sigma_pre"].median())
    grid = abstain_cells(dev_liq, sig_med)
    best = grid[grid["n"] >= MIN_CELL].sort_values(
        "hit_pct", ascending=False).iloc[0]
    ge = 0
    base = dev_liq[dev_liq["s_ts"].notna()].reset_index(drop=True)
    period_idx = base.groupby("period").indices
    tuples = base[A3_FEATURES].to_numpy()
    for _ in range(n_perm):
        pm = tuples.copy()
        for _, idx in period_idx.items():
            if len(idx) > 1:
                pm[idx] = pm[rng.permutation(idx)]
        pb = base.copy()
        pb[A3_FEATURES] = pm
        g = abstain_cells(pb, sig_med)
        g = g[g["n"] >= MIN_CELL]
        if len(g) and g["hit_pct"].max() >= best["hit_pct"]:
            ge += 1
    p_adj = (ge + 1) / (n_perm + 1)
    grid.to_csv(bs.RESULTS_DIR / "analyst_edge_abstain_grid.csv", index=False)
    pd.DataFrame([{"best_cell": f"|s_ts|>={best['thr']}/{best['gate']}/"
                   f"{best['vol']}", "best_hit_pct": best["hit_pct"],
                   "best_n": int(best["n"]), "p_max_stat": round(p_adj, 4),
                   "n_perm": n_perm,
                   "benchmark": "analyst full-strength 92.6% (25/27)"}]
                 ).to_csv(bs.RESULTS_DIR / "analyst_edge_abstain_perm.csv",
                          index=False)
    print(f"A3 best cell {best['hit_pct']}% (n={best['n']}) p_adj={p_adj:.4f}")

    # ---- A4: margin-SUE as the analyst's lens
    from sklearn.metrics import roc_auc_score
    a4_rows = []
    for target, y in (("direction", (d["direction"] == 1).astype(int)),
                      ("full_strength", d["fs"])):
        for f in ("margin_sue_z", "s_cs", "s_ts"):
            ok = d[f].notna()
            a = (roc_auc_score(y[ok], d.loc[ok, f])
                 if y[ok].min() != y[ok].max() else np.nan)
            a4_rows.append({"target": target, "feature": f,
                            "auc_single": round(float(a), 3),
                            "n": int(ok.sum())})
    dm = d[d["margin_sue_z"].notna() & d["s_cs"].notna()].copy()
    ydm = np.empty(0)
    if len(dm) >= 30:
        sd = dm.groupby("period")["s_cs"].transform(lambda s: s - s.mean())
        ad = dm.groupby("period")["margin_sue_z"].transform(
            lambda s: s - s.mean())
        beta = float((sd * ad).sum() / (sd * sd).sum())
        dm["margin_resid"] = dm["margin_sue_z"] - beta * dm["s_cs"]
        bull = dm[dm["direction"] == 1]["margin_resid"]
        bear = dm[dm["direction"] == -1]["margin_resid"]
        p_fwl = sps.mannwhitneyu(bull, bear, alternative="two-sided").pvalue
        a4_rows.append({"target": "direction", "feature":
                        "margin_resid (FWL vs s_cs)",
                        "auc_single": round(float(p_fwl), 4), "n": len(dm)})
    a4 = pd.DataFrame(a4_rows)
    a4.to_csv(bs.RESULTS_DIR / "analyst_edge_margin_vs_scs.csv", index=False)

    # ---- case list (coded selection)
    fsd = d[d["strength"] == 1.0]
    cases = pd.concat([
        fsd[~fsd["hit"]],
        fsd[fsd["hit"] & (fsd["model_dir_ts"] != fsd["direction"])],
        fsd[fsd["hit"]].reindex(
            fsd[fsd["hit"]]["ar0_cc"].abs().sort_values(ascending=False)
            .index[:2]),
    ]).drop_duplicates(["slug", "period"])
    cases[["slug", "ticker", "period", "label_raw", "direction",
           "s_ts", "sector"]].to_csv(
        bs.RESULTS_DIR / "analyst_edge_case_list.csv", index=False)
    print(f"case list: {len(cases)} events")

    # ---- ANALYST_EDGE.md
    reads_p = bs.RESULTS_DIR / "analyst_edge_case_reads.csv"
    reads_md = (md_table(pd.read_csv(reads_p)) if reads_p.exists()
                else "_case reads pending — rerun after curation_")
    lines = [
        "# Analyst edge decomposition (dev, v3 artifacts)",
        "",
        "**POST-HOC-MOTIVATED** (spec + interpretation rules frozen in "
        "study.yaml `analyst_edge` and the REPORT.md ledger before first "
        "run). DIAGNOSTIC ONLY. n = 73 matched / 67 directional / 27 "
        "full-strength; nothing here enters any traded spec. kpi_panel "
        "columns are non-point-in-time (descriptive only).",
        "",
        "## A1 — can the features reconstruct the analyst?",
        "", md_table(aucs), "",
        "Univariate battery (BH-FDR within comparison):",
        "", md_table(uni), "",
        "## B2 — where the analyst beats the model",
        "", f"Groups (model = sign(s_ts)): {grp_counts}", "",
        md_table(con), "", md_table(clus), "",
        "## A3 — learn-to-abstain sweep (full dev, PIT features only)",
        "", md_table(grid), "",
        f"Best qualifying cell: {best['hit_pct']}% (n={int(best['n'])}) — "
        f"max-statistic adjusted p = {p_adj:.4f}. Benchmark: analyst "
        "full-strength 92.6% (25/27) at 27-call coverage.", "",
        "## A4 — margin-SUE as the analyst's lens",
        "", md_table(a4), "",
        "(`margin_resid (FWL vs s_cs)` row reports the Mann-Whitney p of the "
        "s_cs-residualized margin surprise across bullish vs bearish calls.)",
        "",
        "## Case reads (outcome-blinded readers; outcome-selected events)",
        "", reads_md, "",
        "## Prospective protocol",
        "",
        "The only way to decompose the edge going forward is labeled data: "
        "see outputs/protocolo_llamadas_3T26.md (Spanish) and "
        "data/analyst/log_3T26.csv — every 3T26 call logged pre-release "
        "with a reason code, hash-committed per study.yaml "
        "`analyst_direction.provenance_requirement`.",
    ]
    (bs.OUTPUTS_DIR / "ANALYST_EDGE.md").write_text("\n".join(lines))
    print("wrote ANALYST_EDGE.md")


if __name__ == "__main__":
    main()

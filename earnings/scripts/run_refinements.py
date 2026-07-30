#!/usr/bin/env python3
"""run_refinements.py — phase-E candidate evaluation on the modern dev sample.

Computes every pre-committed candidate signal (S1-S7 + overlay), the
pre-report conditioning analyses (interaction regression, 3x3 double sorts,
posture rules), and the candidate-vs-incumbent table with shuffle p-values.
Everything is REPORTED; adoption follows the frozen protocol in study.yaml.

Outputs: results/refine_candidates.csv, refine_doublesort_*.csv,
refine_interactions.csv, refine_posture.csv, mdna_sentiment.csv (cache).
"""
from __future__ import annotations

import html as html_mod
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib import stats as st
from earnlib import study
from earnlib.surprises import availability_array, trailing_sue, winsorize, cross_z

import numpy as np
import pandas as pd

TAG_RE = re.compile(r"<[^>]+>")


# ---------------------------------------------------------------- candidates

def add_margin_sue(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """S1: SUE of the YoY change in operating margin (point-in-time priors
    where the filing provides them)."""
    m = pd.read_parquet(bs.art_path("metrics"))
    cur = m.pivot_table(index=["slug", "period"], columns="metric",
                        values="current", aggfunc="first")
    pri = m.pivot_table(index=["slug", "period"], columns="metric",
                        values="prior", aggfunc="first")
    with np.errstate(divide="ignore", invalid="ignore"):
        mc = cur["operating_income"] / cur["revenue"]
        mp = pri["operating_income"] / pri["revenue"]
    dxm = (mc - mp).rename("dxm").reset_index()

    scfg = cfg["surprise"]
    avail = study.availability_map()
    rows = {}
    for slug, sub in dxm.groupby("slug"):
        sub = sub.sort_values("period").reset_index(drop=True)
        filed = availability_array(
            sub["period"], [avail.get((slug, p)) for p in sub["period"]])
        sue = winsorize(trailing_sue(sub["dxm"].to_numpy(dtype=float),
                                     scfg["min_hist_quarters"],
                                     scfg["target_hist_quarters"],
                                     filed=filed),
                        scfg["sue_winsor"])
        for p, v in zip(sub["period"], sue):
            rows[(slug, p)] = v
    df["margin_sue"] = [rows.get((s, p), np.nan)
                        for s, p in zip(df["slug"], df["period"])]
    # cross-sectional z like the incumbent components
    df["margin_sue_z"] = np.nan
    for _, idx in df.groupby("period").groups.items():
        df.loc[idx, "margin_sue_z"] = cross_z(
            df.loc[idx, "margin_sue"].to_numpy(dtype=float),
            cfg["surprise"]["min_cs_pool"], cfg["surprise"]["z_winsor"])
    return df


def add_composites(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    zr, zn, ze = (df["z_sue_revenue"], df["z_sue_net_income"], df["z_sue_ebitda"])
    sr, sn, se = (df["sue_revenue"], df["sue_net_income"], df["sue_ebitda"])

    # S2: revenue-confirmed beat — NI surprise counted at strength only when
    # the revenue surprise agrees in sign; halved when unconfirmed
    agree_rn = np.sign(sr) == np.sign(sn)
    df["confirmed_beat"] = np.where(agree_rn, zn, 0.5 * zn)

    # S3: conviction — incumbent composite, zeroed unless all 3 agree in sign
    signs = np.sign(pd.concat([sr, sn, se], axis=1))
    all_agree = (signs.abs().sum(axis=1) == 3) & (signs.sum(axis=1).abs() == 3)
    df["conviction"] = np.where(all_agree, df["s_cs"], 0.0)

    # S4: within-quarter mean percentile rank of the raw SUEs
    df["rank_composite"] = np.nan
    for _, idx in df.groupby("period").groups.items():
        sub = df.loc[idx, ["sue_revenue", "sue_net_income", "sue_ebitda"]]
        ranks = sub.rank(pct=True)
        df.loc[idx, "rank_composite"] = (ranks.mean(axis=1) - 0.5) * 2

    # S6: seasonal QoQ of net income (merged currents), sigma-scaled
    m = pd.read_parquet(bs.art_path("metrics"))
    hp = bs.art_path("metrics_hist")
    cur = m[m["metric"] == "net_income"][["slug", "period", "current"]]
    if hp.exists():
        h = pd.read_parquet(hp)
        h = h[h["metric"] == "net_income"][["slug", "period", "current"]]
        cur = pd.concat([cur, h]).drop_duplicates(["slug", "period"], keep="first")
    scfg = cfg["surprise"]
    avail = study.availability_map()
    sq_map = {}
    for slug, sub in cur.groupby("slug"):
        sub = sub.sort_values("period").reset_index(drop=True)
        cmap = dict(zip(sub["period"], sub["current"]))

        def at(p, k):   # period shifted k quarters back
            y, q = int(p[:4]), int(p[5])
            q -= k
            while q <= 0:
                q += 4; y -= 1
            return cmap.get(f"{y}-{q}T", np.nan)

        dq = np.array([
            (cmap[p] - at(p, 1)) - (at(p, 4) - at(p, 5))
            for p in sub["period"]], dtype=float)
        filed = availability_array(
            sub["period"], [avail.get((slug, p)) for p in sub["period"]])
        sue = winsorize(trailing_sue(dq, scfg["min_hist_quarters"],
                                     scfg["target_hist_quarters"],
                                     filed=filed),
                        scfg["sue_winsor"])
        for p, v in zip(sub["period"], sue):
            sq_map[(slug, p)] = v
    df["seasonal_qoq"] = [sq_map.get((s, p), np.nan)
                          for s, p in zip(df["slug"], df["period"])]
    return df


def add_wf_weighted(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """S5: composite with per-metric weights from trailing IC on PAST quarters
    only (expanding, min 8 quarters; equal weights before that)."""
    comps = ["z_sue_revenue", "z_sue_net_income", "z_sue_ebitda"]
    periods = sorted(df["period"].unique())
    win = cfg["phase_e"]["wf_weights_window"]
    out = pd.Series(np.nan, index=df.index)
    for i, p in enumerate(periods):
        past = df[df["period"].isin(periods[max(0, i - win):i])]
        if past["ar0_cc"].notna().sum() >= 60 and i >= win:
            ics = np.array([
                max(st.spearman_ic(past[c].to_numpy(), past["ar0_cc"].to_numpy())["ic"], 0.0)
                for c in comps])
            w = ics / ics.sum() if ics.sum() > 0 else np.ones(3) / 3
        else:
            w = np.ones(3) / 3
        idx = df.index[df["period"] == p]
        zmat = df.loc[idx, comps].to_numpy(dtype=float)
        wsum = np.nansum(zmat * w, axis=1)
        valid = np.isfinite(zmat).sum(axis=1) >= 2
        out.loc[idx] = np.where(valid, wsum, np.nan)
    df["wf_weighted"] = out
    return df


def add_mdna_sentiment(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """S7: lexicon sentiment over the filing's MD&A narrative (modern era)."""
    cache = bs.RESULTS_DIR / "mdna_sentiment.csv"
    if cache.exists():
        sc = pd.read_csv(cache)
    else:
        from src.qa.sentiment import score_sentiment
        rows = []
        for _, r in df[["slug", "ticker", "period"]].drop_duplicates().iterrows():
            path = (bs.SOFT_ROOT / "data" / "reports" / r["slug"] / "xbrl" /
                    f"{r['ticker']}_{r['period']}_mdna.html")
            if not path.exists():
                continue
            text = TAG_RE.sub(" ", path.read_text(errors="ignore"))
            text = html_mod.unescape(text)[:60000]
            if len(text) < 500:
                continue
            res = score_sentiment(text)
            rows.append({"slug": r["slug"], "period": r["period"],
                         "sent_score": res.score, "sent_label": res.label})
        sc = pd.DataFrame(rows)
        bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        sc.to_csv(cache, index=False)
    df = df.merge(sc[["slug", "period", "sent_score"]], on=["slug", "period"],
                  how="left")
    df["mdna_sentiment"] = np.nan
    for _, idx in df.groupby("period").groups.items():
        df.loc[idx, "mdna_sentiment"] = cross_z(
            df.loc[idx, "sent_score"].to_numpy(dtype=float),
            cfg["surprise"]["min_cs_pool"], cfg["surprise"]["z_winsor"])
    w = cfg["phase_e"]["sentiment_overlay_weight"]
    df["s_cs_plus_sent"] = df["s_cs"] + w * df["mdna_sentiment"].fillna(0)
    return df


# ---------------------------------------------------------------- evaluation

def eval_signal(df: pd.DataFrame, col: str, rng, n_shuffle=1000) -> dict:
    d = df[df[col].notna()].copy()
    if len(d) < 60:
        return {"signal": col, "n": len(d), "note": "insufficient"}
    d = study.add_terciles(d, col)
    per_q = []
    for _, sub in d.groupby("period"):
        m3 = sub.loc[sub["tercile"] == 3.0, "ar0_cc"].mean()
        m1 = sub.loc[sub["tercile"] == 1.0, "ar0_cc"].mean()
        if np.isfinite(m3) and np.isfinite(m1):
            per_q.append(m3 - m1)
    fm = st.fama_macbeth(np.array(per_q))
    reg = st.ols_fe_clustered(d["ar0_cc"].to_numpy(), d[col].to_numpy(),
                              d["period"].to_numpy())
    ic = st.spearman_ic(d[col].to_numpy(), d["ar0_cc"].to_numpy())
    real = study.spread_t3t1(d, "ar0_cc")
    n_ge = 0
    for _ in range(n_shuffle):
        sh = d.copy()
        sh[col] = sh.groupby("period")[col].transform(
            lambda s: s.sample(frac=1, random_state=int(rng.integers(1e9))).to_numpy())
        sh = study.add_terciles(sh, col)
        if study.spread_t3t1(sh, "ar0_cc") >= real:
            n_ge += 1
    return {"signal": col, "n": len(d),
            "spread_bps": round(real * 1e4), "fm_spread_bps": round(fm["fm_mean"] * 1e4),
            "fm_t": round(fm["fm_t"], 2), "slope_t": round(reg["t"], 2),
            "ic": round(ic["ic"], 3), "shuffle_p": round((n_ge + 1) / (n_shuffle + 1), 3)}


def ols_fe_multi(y, X, groups):
    """Multi-regressor OLS with quarter FE (within-demeaning) and
    quarter-clustered SEs."""
    mask = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
    y, X, groups = y[mask], X[mask], groups[mask]
    yd, Xd = y.copy().astype(float), X.copy().astype(float)
    for g in np.unique(groups):
        i = groups == g
        yd[i] -= yd[i].mean()
        Xd[i] -= Xd[i].mean(axis=0)
    beta, *_ = np.linalg.lstsq(Xd, yd, rcond=None)
    resid = yd - Xd @ beta
    XtX_inv = np.linalg.inv(Xd.T @ Xd)
    meat = np.zeros((X.shape[1], X.shape[1]))
    uniq = np.unique(groups)
    for g in uniq:
        i = groups == g
        sg = Xd[i].T @ resid[i]
        meat += np.outer(sg, sg)
    meat *= len(uniq) / max(len(uniq) - 1, 1)
    V = XtX_inv @ meat @ XtX_inv
    se = np.sqrt(np.diag(V))
    return beta, beta / se, len(y)


def main() -> None:
    cfg = bs.load_config()
    rng = np.random.default_rng(cfg["validation"]["seed"] + 1)
    panel = study.load_panel(cfg)
    ew = study.assemble_events(cfg, panel)
    df = study.apply_liquidity(
        study.dev_sample(ew, cfg, era="modern"),
        cfg["liquidity"]["primary_min_median_peso_volume"]).copy()
    print(f"modern dev sample: {len(df)} events")

    df = add_margin_sue(df, cfg)
    df = add_composites(df, cfg)
    df = add_wf_weighted(df, cfg)
    df = add_mdna_sentiment(df, cfg)

    # ---- refined composite: the two protocol adoptions combined ----
    # (1) margin SUE added as a 4th component; (2) NI component confirm-
    # weighted by revenue-sign agreement. Everything else unchanged.
    zr, zn, ze = (df["z_sue_revenue"], df["z_sue_net_income"], df["z_sue_ebitda"])
    agree = np.sign(df["sue_revenue"]) == np.sign(df["sue_net_income"])
    zn_conf = np.where(agree, zn, 0.5 * zn)
    comp = np.column_stack([zn_conf, zr, ze, df["margin_sue_z"]])
    nvalid = np.isfinite(comp).sum(axis=1)
    with np.errstate(invalid="ignore"):
        df["s_refined"] = np.where(nvalid >= 2, np.nanmean(comp, axis=1), np.nan)

    # ---- candidate table (incumbent first) ----
    candidates = ["s_cs", "margin_sue_z", "confirmed_beat", "conviction",
                  "rank_composite", "wf_weighted", "seasonal_qoq",
                  "mdna_sentiment", "s_cs_plus_sent", "s_refined"]
    rows = [eval_signal(df, c, rng) for c in candidates]
    cand = pd.DataFrame(rows)
    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    cand.to_csv(bs.RESULTS_DIR / "refine_candidates.csv", index=False)
    print("\n=== candidates (incumbent = s_cs) ===")
    print(cand.to_string(index=False))

    # ---- P1: interaction regressions ----
    inter_rows = []
    for cond, label in [("car_pre5", "S x CAR_pre5"),
                        ("prevol_ratio", "S x prevol_ratio")]:
        sub = df[df[cond].notna() & df["s_cs"].notna() & df["ar0_cc"].notna()]
        X = np.column_stack([sub["s_cs"], sub[cond],
                             sub["s_cs"] * sub[cond]])
        beta, tstats, n = ols_fe_multi(sub["ar0_cc"].to_numpy(), X,
                                       sub["period"].to_numpy())
        inter_rows.append({"model": label, "n": n,
                           "b_S": round(beta[0], 4), "t_S": round(tstats[0], 2),
                           "b_cond": round(beta[1], 4), "t_cond": round(tstats[1], 2),
                           "b_interact": round(beta[2], 4),
                           "t_interact": round(tstats[2], 2)})
    inter = pd.DataFrame(inter_rows)
    inter.to_csv(bs.RESULTS_DIR / "refine_interactions.csv", index=False)
    print("\n=== interaction regressions (quarter FE, clustered t) ===")
    print(inter.to_string(index=False))

    # ---- P2/P3: double sorts ----
    for cond in ("car_pre5", "prevol_ratio"):
        d = df[df[cond].notna()].copy()
        d = study.add_terciles(d, "s_cs")
        d["cond_ter"] = np.nan
        for _, idx in d.groupby("period").groups.items():
            v = d.loc[idx, cond]
            if v.notna().sum() >= 6:
                d.loc[idx, "cond_ter"] = pd.qcut(
                    v.rank(method="first"), 3, labels=[1, 2, 3]).astype(float)
        mat = d.pivot_table(index="tercile", columns="cond_ter",
                            values="ar0_cc", aggfunc="mean") * 1e4
        nmat = d.pivot_table(index="tercile", columns="cond_ter",
                             values="ar0_cc", aggfunc="count")
        mat.round(0).to_csv(bs.RESULTS_DIR / f"refine_doublesort_{cond}.csv")
        print(f"\n=== double sort: S tercile x {cond} tercile (mean AR0 bps) ===")
        print(mat.round(0).to_string(), "\ncounts:\n", nmat.to_string())

    # ---- P4: posture rules on the T3 long side ----
    d = study.add_terciles(df.copy(), "s_cs")
    t3 = d[d["tercile"] == 3.0].copy()
    med_pre = t3.groupby("period")["car_pre5"].transform("median")
    rules = {
        "T3 all (incumbent)": t3,
        "T3 & CAR_pre5 < quarter median": t3[t3["car_pre5"] < med_pre],
        "T3 & prevol_ratio < 1.5": t3[t3["prevol_ratio"] < 1.5],
        "T3 & both": t3[(t3["car_pre5"] < med_pre) & (t3["prevol_ratio"] < 1.5)],
    }
    prow = []
    for name, sub in rules.items():
        r = st.mean_t(sub["ar0_cc"].to_numpy())
        fm = st.fama_macbeth(sub.groupby("period")["ar0_cc"].mean().to_numpy())
        prow.append({"rule": name, "n": r["n"],
                     "mean_bps": round(r["mean"] * 1e4), "t": round(r["t"], 2),
                     "fm_t": round(fm["fm_t"], 2)})
    posture = pd.DataFrame(prow)
    posture.to_csv(bs.RESULTS_DIR / "refine_posture.csv", index=False)
    print("\n=== posture rules (long side, T3) ===")
    print(posture.to_string(index=False))


if __name__ == "__main__":
    main()

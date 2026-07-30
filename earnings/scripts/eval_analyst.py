#!/usr/bin/env python3
"""eval_analyst.py — W3: do the analyst's directional calls add value, and who
predicts the next-day reaction better, the analyst or the model?

DIAGNOSTIC ONLY (study.yaml `analyst.adoption`): nothing here enters the
traded spec. Holdout quarters (2024-2T, 2025-4T modern; historical sacred) are
EXCLUDED — their predictions are counted but never joined to returns.

Every table leads with the sample-size caveat: 138 predictions, fewer matched.

Outputs: results_v3/analyst_eval_{company,agreement,headtohead}.csv
         + outputs/ANALYST_EVAL.md
"""
from __future__ import annotations

import sys
from math import sqrt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib.mdutil import md_table
from earnlib import stats as st
from earnlib import study

import numpy as np
import pandas as pd
from scipy.stats import binomtest


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def main() -> None:
    cfg = bs.load_config()
    acf = cfg["analyst"]
    pred = pd.read_parquet(bs.OUTPUTS_DIR / "analyst_predictions.parquet")
    ew = pd.read_parquet(bs.art_path("event_windows"))

    dev = study.dev_sample(ew, cfg, which="dev")          # holdout excluded
    dev = study.add_terciles(dev)

    holdout_q = set(cfg["holdout"]["quarters"]) | set(
        cfg.get("phase_d", {}).get("historical_holdout_quarters", []))
    n_holdout_pred = int(pred["period"].isin(holdout_q).sum())

    j = pred[pred["slug"].notna()].merge(
        dev[["slug", "period", "ar0_cc", "s_cs", "s_ts", "tercile"]],
        on=["slug", "period"], how="inner")
    j["realized_up"] = j["ar0_cc"] > 0
    d = j[j["direction"] != 0].copy()                     # directional calls
    d["hit"] = (d["direction"] == 1) == d["realized_up"]

    lines = [
        "# W3 — analyst expectations vs the model",
        "",
        "**Sample-size caveat first**: 138 total predictions (2Q24-2Q26); "
        f"{n_holdout_pred} fall in holdout quarters and are excluded from all "
        f"return-joined tables; {len(j)} match a dev event, {len(d)} are "
        "directional. Per-company cells are tiny; exact small-sample tests "
        "and Wilson intervals throughout; nothing here enters the traded spec "
        "(study.yaml `analyst.adoption`).",
        "",
        "**Provenance caveat**: the workbook is analyst-maintained; this "
        "evaluation assumes every label was recorded BEFORE the report was "
        "released. Any label entered or revised after seeing the reaction "
        "inflates hit rates (the 92.6% full-strength figure is extraordinary "
        "for next-day direction — verify recording discipline before acting "
        "on it). Only prospectively logged quarters (2026-3T onward) can "
        "establish the true rate.",
        "",
        "## 1. Overall directional hit rate (outcome: sign of ar0_cc)",
        "",
    ]

    def hit_block(sub: pd.DataFrame, name: str) -> str:
        n, k = len(sub), int(sub["hit"].sum())
        if n == 0:
            return f"| {name} | 0 | – | – | – |"
        bt = binomtest(k, n, 0.5)
        lo, hi = wilson(k, n)
        return (f"| {name} | {n} | {k / n:.1%} | [{lo:.1%}, {hi:.1%}] "
                f"| {bt.pvalue:.3f} |")

    lines += ["| calls | n | hit rate | Wilson 95% | exact p vs 50% |",
              "|---|---|---|---|---|",
              hit_block(d, "all directional"),
              hit_block(d[d["strength"] == 1.0], "full-strength (Positive/Negative)"),
              hit_block(d[d["strength"] == 0.5], "soft (N to P / N to N)"),
              hit_block(d[d["direction"] == 1], "bullish calls"),
              hit_block(d[d["direction"] == -1], "bearish calls"), ""]

    neut = j[j["direction"] == 0]
    if len(neut):
        lines += [
            f"Neutral calls (n={len(neut)}, descriptive only): mean |ar0_cc| "
            f"{neut['ar0_cc'].abs().mean() * 1e4:.0f} bps vs "
            f"{d['ar0_cc'].abs().mean() * 1e4:.0f} bps for directional calls.", ""]

    # 2. per-company
    lines += ["## 2. Per-company analyst hit rate", "",
              f"Inferential rows require >= {acf['min_company_n']} directional "
              "matched calls; a Bonferroni correction across "
              "companies applies to any p below.", "",
              "| ticker | n | hits | hit rate | Wilson 95% | exact p |",
              "|---|---|---|---|---|---|"]
    comp_rows = []
    for tick, g in d.groupby("ticker"):
        n, k = len(g), int(g["hit"].sum())
        row = {"ticker": tick, "n": n, "hits": k, "hit_rate": k / n}
        if n >= acf["min_company_n"]:
            bt = binomtest(k, n, 0.5)
            lo, hi = wilson(k, n)
            row |= {"wilson_lo": lo, "wilson_hi": hi, "p": bt.pvalue}
            lines.append(f"| {tick} | {n} | {k} | {k / n:.0%} "
                         f"| [{lo:.0%}, {hi:.0%}] | {bt.pvalue:.3f} |")
        else:
            lines.append(f"| {tick} | {n} | {k} | {k / n:.0%} | – | – |")
        comp_rows.append(row)
    comp = pd.DataFrame(comp_rows).sort_values(["n", "hit_rate"], ascending=False)

    # 3. agreement with the model
    j["model_dir"] = np.where(j["tercile"] == 3, 1,
                              np.where(j["tercile"] == 1, -1, 0))
    ag = pd.crosstab(j["direction"], j["model_dir"],
                     rownames=["analyst"], colnames=["model tercile dir"])
    lines += ["", "## 3. Agreement: analyst direction x model tercile", "",
              "Model direction: T3 (top surprise tercile) = +1, T1 = -1, T2 = 0.",
              "", md_table(ag.reset_index()), ""]
    agree_rate = float((np.sign(j["direction"]) == np.sign(j["model_dir"]))
                       [j["direction"] != 0].mean()) if len(j) else float("nan")
    lines += [f"Directional agreement rate (analyst vs model, non-neutral "
              f"analyst calls): {agree_rate:.1%}", ""]

    # 4. head-to-head on common events
    hh = j[(j["direction"] != 0) & (j["model_dir"] != 0)].copy()
    hh["hit"] = (hh["direction"] == 1) == hh["realized_up"]
    hh["model_hit"] = (hh["model_dir"] == 1) == hh["realized_up"]
    n_both = len(hh)
    a_only = int((hh["hit"] & ~hh["model_hit"]).sum())
    m_only = int((~hh["hit"] & hh["model_hit"]).sum())
    lines += ["## 4. Head-to-head (events where both give a direction)", "",
              f"- common events: {n_both}",
              f"- analyst hit rate: {hh['hit'].mean():.1%}   "
              f"model hit rate: {hh['model_hit'].mean():.1%}" if n_both else "- none",
              f"- discordant pairs: analyst right/model wrong {a_only}, "
              f"model right/analyst wrong {m_only}"]
    if a_only + m_only > 0:
        mc = binomtest(a_only, a_only + m_only, 0.5)
        lines.append(f"- McNemar exact p (discordant pairs): {mc.pvalue:.3f} — "
                     "the only honest head-to-head at this n")
    lines.append("")

    # 5. incremental value (diagnostic)
    reg = j.dropna(subset=["ar0_cc", "s_cs"]).copy()
    reg["analyst_signal"] = reg["direction"] * reg["strength"]
    lines += ["## 5. Incremental value over the model (diagnostic only)", ""]
    if len(reg) >= 30 and reg["analyst_signal"].std() > 0:
        y = reg["ar0_cc"].to_numpy()
        s = reg["s_cs"].to_numpy()
        a = reg["analyst_signal"].to_numpy()
        groups = reg["period"].to_numpy()
        raw = st.ols_fe_clustered(y, a, groups)
        # Frisch-Waugh: incremental analyst effect = effect of the part of the
        # analyst signal orthogonal to s_cs (both within-quarter demeaned)
        sd = np.empty_like(s)
        ad = np.empty_like(a)
        for g in np.unique(groups):
            idx = groups == g
            sd[idx] = s[idx] - s[idx].mean()
            ad[idx] = a[idx] - a[idx].mean()
        beta = (np.sum(sd * ad) / np.sum(sd * sd)) if np.sum(sd * sd) > 0 else 0.0
        resid_a = a - beta * s
        inc = st.ols_fe_clustered(y, resid_a, groups)
        model = st.ols_fe_clustered(y, s, groups)
        lines += [
            "Quarter-FE regressions with quarter-clustered t "
            "(univariate + Frisch-Waugh incremental):",
            "",
            f"- model alone:   ar0_cc ~ s_cs: slope {model['slope']:+.4f} "
            f"(t = {model['t']:.2f}, n = {model['n']})",
            f"- analyst alone: ar0_cc ~ analyst_signal: slope {raw['slope']:+.4f} "
            f"(t = {raw['t']:.2f}, n = {raw['n']})",
            f"- analyst INCREMENTAL to s_cs (FWL residual): slope "
            f"{inc['slope']:+.4f} (t = {inc['t']:.2f}, p = {inc['p']:.3f})",
            "",
            f"n = {len(reg)} over {reg['period'].nunique()} quarters — a "
            "positive, significant incremental slope would suggest the analyst "
            "adds information beyond the model; adoption still requires the "
            "pre-committed bar.",
            ""]
    else:
        lines += [f"insufficient joined sample for the regression (n={len(reg)}).", ""]

    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    comp.to_csv(bs.RESULTS_DIR / "analyst_eval_company.csv", index=False)
    ag.to_csv(bs.RESULTS_DIR / "analyst_eval_agreement.csv")
    hh.to_csv(bs.RESULTS_DIR / "analyst_eval_headtohead.csv", index=False)
    (bs.OUTPUTS_DIR / "ANALYST_EVAL.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()

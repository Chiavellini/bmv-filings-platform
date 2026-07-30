#!/usr/bin/env python3
"""simulate_signal_layer.py — walk-forward evaluation of the pre-committed
signal layer (study.yaml `signal_layer`): buy/neutral/short classification,
PIT-calibrated conviction, fractional-Kelly sizing.

Dev quarters only (sacred + modern holdout excluded); quarter-q decisions come
from a conviction model fit on the trailing dev quarters strictly before q.
Runs on v3 artifacts (corrected timing) — EARNINGS_V3=1 required.

Outputs: results_v3/signal_layer.csv + outputs/SIGNAL_LAYER.md
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib.mdutil import md_table
from earnlib import signal as sg
from earnlib import stats as st
from earnlib import study

import numpy as np
import pandas as pd

sys.path.insert(0, str(bs.EARNINGS_ROOT / "scripts"))
from simulate_strategy import trade_returns  # noqa: E402  (entry rules reused)


def daily_returns(day: pd.DataFrame, cost_bps: int, sizing: str) -> float:
    """One t0's portfolio return. buy legs earn +raw_ret, short legs -raw_ret.

    kelly: weights = kelly_f (fallback rows get the day's median kelly weight,
           or equal split if no calibrated rows), normalized only if the gross
           exposure exceeds 1. equal: 1/n each (fully invested, matches the
           incumbent's daily-mean convention).
    """
    r = np.where(day["action"] == "buy", day["raw_ret"], -day["raw_ret"])
    r = r - cost_bps / 1e4
    if sizing == "equal":
        w = np.full(len(day), 1.0 / len(day))
    else:
        w = day["kelly_f"].to_numpy(dtype=float).copy()
        calibrated = w >= 0
        fill = float(np.median(w[calibrated])) if calibrated.any() else 1.0 / len(day)
        w[~calibrated] = fill
        if w.sum() > 1.0:
            w = w / w.sum()
    return float(np.sum(w * r))


def sim(decided: pd.DataFrame, cost_bps: int, sizing: str,
        dev_years: float) -> dict:
    traded = decided[decided["action"] != "neutral"]
    if len(traded) < 5:
        return {"trades": 0}
    daily = traded.groupby("t0").apply(
        lambda g: daily_returns(g, cost_bps, sizing))
    total = float(np.prod(1 + daily) - 1)
    sh = st.dual_sharpe(daily.to_numpy(dtype=float), dev_years)
    return {
        "trades": len(traded), "trade_days": len(daily),
        "mean_day_bps": round(float(daily.mean()) * 1e4, 1),
        "hit_rate": round(float((daily > 0).mean()), 2),
        "ann_pct": round(((1 + total) ** (1 / dev_years) - 1) * 100, 1),
        "sharpe_trade_day": round(sh["sharpe_trade_day"], 2)
                            if np.isfinite(sh["sharpe_trade_day"]) else None,
        "sharpe_calendar": round(sh["sharpe_calendar"], 2)
                           if np.isfinite(sh["sharpe_calendar"]) else None,
    }


def main() -> None:
    assert bs.V3, "signal layer runs on v3 artifacts: set EARNINGS_V3=1"
    cfg = bs.load_config()
    sl = cfg["signal_layer"]
    panel = study.load_panel(cfg)
    ew = study.assemble_events(cfg, panel)
    prices = pd.read_parquet(bs.art_path("prices"))

    base = study.apply_liquidity(
        study.dev_sample(ew, cfg),
        cfg["liquidity"]["primary_min_median_peso_volume"])
    tr = trade_returns(base, prices)
    tr = tr[tr["s_ts"].notna()].copy()
    tr["action"] = [sg.classify(s, cfg)[1] for s in tr["s_ts"]]

    quarters = sorted(tr["period"].unique(), key=lambda p: (p[:4], p[5]))
    cal_q = int(sl["conviction"]["calibration_quarters"])

    decided_rows = []
    for qi, q in enumerate(quarters):
        train_q = quarters[max(0, qi - cal_q):qi]
        train = tr[tr["period"].isin(train_q) & (tr["action"] != "neutral")]
        model = sg.fit_conviction(train, cfg) if len(train) else None
        for _, r in tr[tr["period"] == q].iterrows():
            d = sg.decide(r["s_ts"], model, cfg, n_calibration=len(train))
            decided_rows.append({**r.to_dict(), "label": d.label,
                                 "action": d.action, "p_hat": d.p_hat,
                                 "kelly_f": d.kelly_f})
    dec = pd.DataFrame(decided_rows)

    t0s = pd.to_datetime(dec["t0"])
    dev_years = max((t0s.max() - t0s.min()).days / 365.25, 1e-9)

    rows = []
    for legs, name in ((("buy",), "long only"), (("short",), "short only"),
                       (("buy", "short"), "combined")):
        sub = dec[dec["action"].isin(legs)]
        for sizing in ("equal", "kelly"):
            for cost in sl["costs_bps"]:
                rows.append({"legs": name, "sizing": sizing, "cost_bps": cost,
                             **sim(sub, cost, sizing, dev_years)})
    res = pd.DataFrame(rows)

    # within-quarter permutation of s_ts (pre-committed seed offset); the null
    # keeps the fitted p(|s|) maps and reassigns signals across same-quarter
    # events, breaking signal-return alignment
    rng = np.random.default_rng(cfg["validation"]["seed"] + sl["seed_offset"])
    n_perm = cfg["validation"]["n_perm_shuffle"]
    real_row = res[(res["legs"] == "combined") & (res["sizing"] == "kelly")
                   & (res["cost_bps"] == 25)]
    real_stat = float(real_row["mean_day_bps"].iloc[0]) if len(real_row) and \
        "mean_day_bps" in real_row else np.nan
    perm_ge = 0
    for _ in range(n_perm):
        p = dec.copy()
        p["s_perm"] = p.groupby("period")["s_ts"].transform(rng.permutation)
        p["action"] = [sg.classify(s, cfg)[1] for s in p["s_perm"]]
        traded = p[p["action"] != "neutral"]
        if not len(traded):
            continue
        daily = traded.groupby("t0").apply(
            lambda g: daily_returns(g, 25, "kelly"))
        if float(daily.mean()) * 1e4 >= real_stat:
            perm_ge += 1
    p_perm = (perm_ge + 1) / (n_perm + 1)

    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    res.to_csv(bs.RESULTS_DIR / "signal_layer.csv", index=False)

    # incumbent comparison (pre-committed adoption bar)
    inc_path = bs.RESULTS_DIR / "strategy_sim.csv"
    inc_line = ""
    verdict = "UNDETERMINED (incumbent sim missing)"
    if inc_path.exists():
        inc = pd.read_csv(inc_path)
        inc25 = inc[(inc["leg"].str.startswith("long")) & (inc["cost_bps_rt"] == 25)]
        if len(inc25):
            inc_sh = float(inc25["sharpe_calendar"].iloc[0])
            cand = res[(res["legs"] == "combined") & (res["sizing"] == "kelly")
                       & (res["cost_bps"] == 25)]
            cand_sh = float(cand["sharpe_calendar"].iloc[0]) if len(cand) else np.nan
            bar = inc_sh + 0.10
            ok = np.isfinite(cand_sh) and cand_sh >= bar and p_perm < 0.05
            verdict = ("CANDIDATE MEETS DEV BAR — arbiter remains the one-shot "
                       "2026-3T walk-forward" if ok else
                       "NOT ADOPTED (pre-committed bar not met; reported, not iterated)")
            inc_line = (f"incumbent long-only Sharpe(25bps, calendar) = {inc_sh:.2f}; "
                        f"bar = {bar:.2f}; candidate = {cand_sh:.2f}; "
                        f"shuffle p = {p_perm:.3f}")

    lines = [
        "# Signal layer — walk-forward dev evaluation (v3 artifacts)",
        "",
        "Spec pre-committed in study.yaml `signal_layer` before this run;",
        "dev quarters only; conviction fit strictly on trailing quarters.",
        "",
        md_table(res),
        "",
        f"Within-quarter shuffle p (combined/kelly/25bps mean day return): "
        f"{p_perm:.3f} ({n_perm} permutations)",
        "",
        inc_line,
        "",
        f"**Verdict: {verdict}**",
        "",
        "Notes: short leg assumes borrow availability on BMV large caps — "
        "reported, not asserted; fallback (pre-calibration) trades use the "
        "day's median Kelly weight.",
    ]
    (bs.OUTPUTS_DIR / "SIGNAL_LAYER.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()

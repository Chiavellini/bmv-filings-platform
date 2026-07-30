#!/usr/bin/env python3
"""simulate_direction_magnitude.py — direction x magnitude ensemble:
the ANALYST supplies the trade direction, the SYSTEM supplies the size.

POST-HOC (motivated 2026-07-29 after seeing the analyst eval + overlay
results) and DIAGNOSTIC ONLY: nothing here enters the traded spec. The
dev-selected winner is pre-registered in study.yaml `analyst_direction`;
its arbiter is the one-shot 2026-3T walk-forward.

Sizing battery (weights are absolute capital fractions; idle capital earns 0;
gross normalized only if > 1; f_cap mirrors signal_layer.kelly):
  equal        f = f_cap                     (pure-direction baseline)
  model        f = f_cap * min(|s_ts|/2, 1)  (the literal "model magnitude")
  vol          f = f_cap * min(sigma_pre/0.03, 1)  (validated size predictor)
  kelly_class  PIT Kelly per analyst conviction class (trailing 8 quarters,
               min 10 class trades else equal fallback — documented deviation
               from signal_layer's min 60: only 67 directional calls exist)

Outputs: results_v3/dirmag_{grid,shuffle}.csv + outputs/DIRMAG.md
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
from simulate_strategy import trade_returns  # noqa: E402

F_CAP = 0.10
S_REF = 2.0        # |s_ts| at which model-sizing saturates (a priori)
SIG_REF = 0.03     # sigma_pre at which vol-sizing saturates (~90th pct)
MIN_CLASS = 10
SEED_OFFSET = 5


def size_weights(df: pd.DataFrame, sizing: str) -> np.ndarray:
    """Absolute capital fraction per trade (pre gross-normalization)."""
    if sizing == "equal":
        return np.full(len(df), F_CAP)
    if sizing == "model":
        return F_CAP * np.minimum(df["s_ts"].abs().to_numpy() / S_REF, 1.0)
    if sizing == "vol":
        return F_CAP * np.minimum(df["sigma_pre"].to_numpy() / SIG_REF, 1.0)
    if sizing == "kelly_class":
        return df["kelly_f"].to_numpy(dtype=float)
    raise ValueError(sizing)


def fit_class_kelly(d: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """PIT quarter loop: kelly_f per trade from trailing-8q stats of the
    trade's conviction class (fuerte=1.0 / suave=0.5). Fallback = F_CAP."""
    lam = float(cfg["signal_layer"]["kelly"]["lambda"])
    quarters = sorted(d["period"].unique(), key=lambda p: (p[:4], p[5]))
    out = d.copy()
    out["kelly_f"] = F_CAP
    for qi, q in enumerate(quarters):
        train_q = quarters[max(0, qi - 8):qi]
        train = d[d["period"].isin(train_q)]
        for cls in (1.0, 0.5):
            leg = train[train["strength"] == cls]
            mask = (out["period"] == q) & (out["strength"] == cls)
            if len(leg) < MIN_CLASS or not mask.any():
                continue
            ret = (leg["direction"] * leg["raw_ret"]).to_numpy(dtype=float)
            wins = ret > 0
            if not wins.any() or wins.all():
                continue
            p_hat = float(wins.mean())
            b = float(ret[wins].mean() / abs(ret[~wins].mean()))
            out.loc[mask, "kelly_f"] = sg.kelly_fraction(p_hat, b, lam, F_CAP)
    return out


def day_returns(d: pd.DataFrame, sizing: str, cost_bps: int) -> pd.Series:
    def one(g: pd.DataFrame) -> float:
        f = size_weights(g, sizing)
        if f.sum() > 1.0:
            f = f / f.sum()
        net = g["direction"].to_numpy() * g["raw_ret"].to_numpy() - cost_bps / 1e4
        return float(np.sum(f * net))
    return d.groupby("t0").apply(one)


def stats_row(d: pd.DataFrame, sizing: str, cost: int,
              dev_years: float) -> dict:
    daily = day_returns(d, sizing, cost)
    per_trade = (d["direction"] * d["raw_ret"] - cost / 1e4)
    mt = st.mean_t(per_trade.to_numpy(dtype=float))
    sh = st.dual_sharpe(daily.to_numpy(dtype=float), dev_years)
    return {"sizing": sizing, "cost_bps": cost, "trades": len(d),
            "trade_days": len(daily),
            "bps_per_trade": round(mt["mean"] * 1e4, 1),
            "t_per_trade": round(mt["t"], 2),
            "hit_rate": round(float((per_trade > 0).mean()), 2),
            "mean_day_bps": round(float(daily.mean()) * 1e4, 1),
            "sharpe_calendar": round(sh["sharpe_calendar"], 2)
                               if np.isfinite(sh["sharpe_calendar"]) else None}


def main() -> None:
    assert bs.V3, "set EARNINGS_V3=1"
    cfg = bs.load_config()

    panel = study.load_panel(cfg)
    ew = study.assemble_events(cfg, panel)
    prices = pd.read_parquet(bs.art_path("prices"))
    base = study.apply_liquidity(
        study.dev_sample(ew, cfg),
        cfg["liquidity"]["primary_min_median_peso_volume"])
    tr = trade_returns(base, prices)
    tr = tr[tr["s_ts"].notna() & tr["sigma_pre"].notna()
            & tr["raw_ret"].notna()].copy()
    holdout_q = set(cfg["holdout"]["quarters"]) | set(
        cfg.get("phase_d", {}).get("historical_holdout_quarters", []))
    assert not set(tr["period"]) & holdout_q

    pred = pd.read_parquet(bs.OUTPUTS_DIR / "analyst_predictions.parquet")
    pred = pred[pred["slug"].notna() & (pred["direction"] != 0)]
    d = pred.merge(
        tr[["slug", "period", "raw_ret", "s_ts", "sigma_pre", "t0"]],
        on=["slug", "period"], how="inner", validate="many_to_one")
    n_fs = int((d["strength"] == 1.0).sum())
    print(f"covered directional in traded frame: {len(d)} ({n_fs} full-strength)")
    assert n_fs >= 25 and len(d) >= 60

    t0s = pd.to_datetime(tr["t0"])
    dev_years = max((t0s.max() - t0s.min()).days / 365.25, 1e-9)

    d = fit_class_kelly(d, cfg)
    fs = d[d["strength"] == 1.0]

    rows = []
    for sleeve, dd in (("full-strength", fs), ("all directional", d)):
        for sizing in ("equal", "model", "vol", "kelly_class"):
            for cost in (0, 25, 50):
                rows.append({"sleeve": sleeve,
                             **stats_row(dd, sizing, cost, dev_years)})
    grid = pd.DataFrame(rows)

    # permutation nulls at the primary cell: full-strength / kelly_class / 25
    rng = np.random.default_rng(cfg["validation"]["seed"] + SEED_OFFSET)
    n_perm = cfg["validation"]["n_perm_shuffle"]
    real = float(day_returns(fs, "kelly_class", 25).mean()) * 1e4
    ge_dir = ge_size = 0
    fs_idx = fs.reset_index(drop=True)
    period_idx = fs_idx.groupby("period").indices
    for _ in range(n_perm):
        # (a) permute directions within quarter (sizes fixed)
        p = fs_idx.copy()
        dirs = p["direction"].to_numpy().copy()
        for _, idx in period_idx.items():
            if len(idx) > 1:
                dirs[idx] = rng.permutation(dirs[idx])
        p["direction"] = dirs
        if float(day_returns(p, "kelly_class", 25).mean()) * 1e4 >= real:
            ge_dir += 1
        # (b) permute sizes across trades within quarter (directions fixed)
        p2 = fs_idx.copy()
        ks = p2["kelly_f"].to_numpy().copy()
        for _, idx in period_idx.items():
            if len(idx) > 1:
                ks[idx] = rng.permutation(ks[idx])
        p2["kelly_f"] = ks
        if float(day_returns(p2, "kelly_class", 25).mean()) * 1e4 >= real:
            ge_size += 1
    shuffle = pd.DataFrame([
        {"null": "permute directions (within quarter)", "real_mean_day_bps":
         round(real, 1), "p": round((ge_dir + 1) / (n_perm + 1), 4),
         "n_perm": n_perm, "seed": cfg["validation"]["seed"] + SEED_OFFSET},
        {"null": "permute sizes (within quarter, directions fixed)",
         "real_mean_day_bps": round(real, 1),
         "p": round((ge_size + 1) / (n_perm + 1), 4),
         "n_perm": n_perm, "seed": cfg["validation"]["seed"] + SEED_OFFSET}])

    # benchmarks on disk (25 bps calendar Sharpe)
    inc = pd.read_csv(bs.RESULTS_DIR / "strategy_sim.csv")
    sl = pd.read_csv(bs.RESULTS_DIR / "signal_layer.csv")
    ov = pd.read_csv(bs.RESULTS_DIR / "analyst_overlay_grid.csv")
    bench = pd.DataFrame([
        {"benchmark": "incumbent model long-only",
         "sharpe_calendar": float(inc[(inc.leg.str.startswith("long"))
                                      & (inc.cost_bps_rt == 25)]
                                  ["sharpe_calendar"].iloc[0])},
        {"benchmark": "signal layer combined/kelly",
         "sharpe_calendar": float(sl[(sl.legs == "combined")
                                     & (sl.sizing == "kelly")
                                     & (sl.cost_bps == 25)]
                                  ["sharpe_calendar"].iloc[0])},
        {"benchmark": "overlay w=0.5 combined/kelly",
         "sharpe_calendar": float(ov[(ov.w == 0.5) & (ov.legs == "combined")
                                     & (ov.sizing == "kelly")
                                     & (ov.cost_bps == 25)]
                                  ["sharpe_calendar"].iloc[0])}])

    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    grid.to_csv(bs.RESULTS_DIR / "dirmag_grid.csv", index=False)
    shuffle.to_csv(bs.RESULTS_DIR / "dirmag_shuffle.csv", index=False)

    lines = [
        "# Direction x magnitude — analyst direction, system-sized "
        "(dev, v3 artifacts)",
        "",
        "**POST-HOC + sample-size caveat first**: this design was motivated "
        "AFTER seeing the analyst evaluation (researcher degree of freedom, "
        f"logged). {len(d)} directional covered trades, {n_fs} full-strength; "
        "calls exist only from 2024-3T. Per-trade t is the honest headline; "
        "annualization uses the full dev span (capital idle pre-coverage).",
        "",
        f"Sizing refs (a priori): f_cap {F_CAP}, |s_ts| saturation {S_REF}, "
        f"sigma_pre saturation {SIG_REF}, kelly lambda 0.25, min class "
        f"trades {MIN_CLASS}.",
        "",
        "## Grid", "", md_table(grid), "",
        "## Permutation nulls (full-strength / kelly_class / 25 bps)",
        "", md_table(shuffle), "",
        "## Benchmarks (Sharpe calendar, 25 bps)", "", md_table(bench), "",
        "**Verdict: DIAGNOSTIC ONLY — not adopted.** Pre-registered as "
        "study.yaml `analyst_direction`; arbiter one-shot 2026-3T "
        "walk-forward with prospectively logged calls.",
    ]
    (bs.OUTPUTS_DIR / "DIRMAG.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()

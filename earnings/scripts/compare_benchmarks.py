#!/usr/bin/env python3
"""compare_benchmarks.py — the earnings strategy vs buy & hold, one clean panel.

Curves (daily compounding equity, common calendar):
  EVT-SIGN 100%    every scored fresh event: s_ts>0 long / s_ts<0 short, 100% of
                   capital (same-day signals split equally), validated entry
                   rules, exit close(t0), 25 bps/rt; cash days flat
  EVT-SIGN cutoff  same but only |s_ts|>1
  EVT-LONG 100%    long side only (BMV shorting is mostly impractical)
  EVT + CETES 9%   EVT-SIGN 100% with idle cash earning a flat 9%/yr MXN proxy
  B&H-EW           equal-weight buy&hold of the SAME companies (adjclose)
  B&H-MXX          the IPC index
  B&H-EW + overlay B&H-EW plus the event tilt: on a long-signal day the book
                   doubles that name's weight; on a short-signal day it exits it
                   for the session (capital never idle)

Descriptive benchmarking of the validated incumbent spec — no new inference.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib import study

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simulate_strategy import trade_returns

COST = 25e-4
CETES = 0.09  # flat MXN cash-yield proxy (Banxico CETES-28 avg 2022-2025 ~9-11%)


def curve_stats(eq: pd.Series, exposed: pd.Series, trades: int, rf: float = 0.0) -> dict:
    r = eq.pct_change().dropna()
    years = len(r) / 252
    total = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1
    vol = r.std(ddof=1) * np.sqrt(252)
    sharpe = (r.mean() * 252 - rf) / vol if vol > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    exp_frac = float(exposed.mean())
    n_exp = int(exposed.sum())
    ret_per_exposed = (np.log1p(total) / n_exp * 1e4) if n_exp else np.nan
    return {"total_pct": round(total * 100, 1), "cagr_pct": round(cagr * 100, 1),
            "ann_vol_pct": round(vol * 100, 1), "sharpe_rf0": round(sharpe, 2),
            "max_dd_pct": round(dd * 100, 1), "pct_days_exposed": round(exp_frac * 100),
            "trades": trades, "log_bps_per_exposed_day": round(ret_per_exposed, 1)}


def main() -> None:
    cfg = bs.load_config()
    panel = study.load_panel(cfg)
    ew = study.assemble_events(cfg, panel)
    prices = pd.read_parquet(bs.art_path("prices"))
    index_df = pd.read_parquet(bs.OUTPUTS_DIR / "index_mxx.parquet").sort_values("date")

    dev = study.apply_liquidity(
        study.dev_sample(ew, cfg),
        cfg["liquidity"]["primary_min_median_peso_volume"])
    tr = trade_returns(dev, prices)
    tr = tr[tr["s_ts"].notna() & (tr["s_ts"] != 0)]

    start, end = tr["t0"].min(), pd.Timestamp("2026-07-21").date()
    cal = [d for d in panel.cal.dates if start <= d <= end]
    caldx = pd.Index(cal)
    universe_syms = sorted(dev["symbol"].unique())
    print(f"window: {start} .. {end} ({len(cal)} trading days), "
          f"{len(universe_syms)} companies, {len(tr)} signals")

    # ---- event-strategy daily returns ----
    def evt_daily(leg: pd.DataFrame, signed: bool) -> tuple[pd.Series, pd.Series]:
        r = leg.copy()
        r["ret"] = np.where(signed & (r["s_ts"] < 0), -r["raw_ret"], r["raw_ret"])
        r["ret"] -= COST
        daily = r.groupby("t0")["ret"].mean()
        s = pd.Series(0.0, index=caldx)
        s.loc[[d for d in daily.index if d in caldx]] = daily[
            [d for d in daily.index if d in caldx]].to_numpy()
        exposed = pd.Series(False, index=caldx)
        exposed.loc[[d for d in daily.index if d in caldx]] = True
        return s, exposed

    sign_all = tr
    sign_cut = tr[tr["s_ts"].abs() > 1.0]
    long_all = tr[tr["s_ts"] > 0]

    curves, rows = {}, []
    for name, leg, signed, cash_y in [
        ("EVT-SIGN 100%", sign_all, True, 0.0),
        ("EVT-SIGN |s|>1", sign_cut, True, 0.0),
        ("EVT-LONG 100%", long_all, False, 0.0),
        ("EVT-SIGN + CETES 9% cash", sign_all, True, CETES),
    ]:
        d, exp = evt_daily(leg, signed)
        if cash_y:
            d = d + np.where(exp, 0.0, cash_y / 252)
        eq = (1 + d).cumprod()
        curves[name] = eq
        rows.append({"strategy": name, **curve_stats(eq, exp, len(leg))})

    # ---- buy & hold: equal weight the same companies ----
    wide = (prices[prices["symbol"].isin(universe_syms)]
            .pivot_table(index="date", columns="symbol", values="adjclose")
            .reindex(caldx).ffill())
    rets = wide.pct_change()
    bh_ew = rets.mean(axis=1).fillna(0.0)
    eq = (1 + bh_ew).cumprod()
    curves["B&H equal-weight (same cos)"] = eq
    rows.append({"strategy": "B&H equal-weight (same cos)",
                 **curve_stats(eq, pd.Series(True, index=caldx), len(universe_syms))})

    idx = (index_df.set_index("date")["adjclose"].reindex(caldx).ffill())
    eq = idx / idx.iloc[0]
    curves["B&H ^MXX"] = eq
    rows.append({"strategy": "B&H ^MXX",
                 **curve_stats(eq, pd.Series(True, index=caldx), 1)})

    # ---- overlay: B&H-EW plus the event tilt ----
    n = len(universe_syms)
    tilt = pd.Series(0.0, index=caldx)
    for _, r in tr.iterrows():
        if r["t0"] in caldx.to_list():
            sym_ret = r["raw_ret"]
            if r["s_ts"] > 0:      # double this name's weight for the session
                tilt.loc[r["t0"]] += (sym_ret - COST) / n
            else:                  # step aside from the name for the session
                tilt.loc[r["t0"]] -= (sym_ret + COST) / n
    ov = bh_ew + tilt
    eq = (1 + ov).cumprod()
    curves["B&H-EW + event overlay"] = eq
    rows.append({"strategy": "B&H-EW + event overlay",
                 **curve_stats(eq, pd.Series(True, index=caldx), len(tr))})

    out = pd.DataFrame(rows)
    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(bs.RESULTS_DIR / "benchmark_panel.csv", index=False)
    cdf = pd.DataFrame({k: v.to_numpy() for k, v in curves.items()},
                       index=caldx)
    cdf.index.name = "date"
    cdf.to_csv(bs.RESULTS_DIR / "benchmark_curves.csv")

    pd.set_option("display.width", 220)
    print("\n=== PROFIT PANEL: earnings strategy vs buy & hold ===")
    print(out.to_string(index=False))
    print(f"\nsanity: B&H-MXX endpoints {idx.iloc[0]:.0f} -> {idx.iloc[-1]:.0f}")


if __name__ == "__main__":
    main()

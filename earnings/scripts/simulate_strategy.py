#!/usr/bin/env python3
"""simulate_strategy.py — historical simulation of the tradable rule (phase C).

Entry rules (fixed a priori; descriptive simulation, not signal tuning):
  * after-close / pre-open events: enter open(t0), exit close(t0)
  * in-session events (info public before the close): enter close(filing day),
    exit close(t0)
  * LONG when s_ts > cutoff (headline cutoff +1.0; frontier over pre-listed
    cutoffs {0, 0.5, 1.0, 1.5, 2.0} — reported as a curve, no post-hoc pick)
  * liquidity: primary MXN 5M filter (1M variant reported)
  * development quarters only; sacred holdout untouched; RAW unhedged returns.
Costs: 0 / 25 / 50 bps per round trip.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib import study, stats as st

import numpy as np
import pandas as pd

CUTOFFS = [0.0, 0.5, 1.0, 1.5, 2.0]
HEADLINE_CUT = 1.0
COSTS = [0, 25, 50]


def trade_returns(df: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Raw per-event trade return under the entry rules. Adds raw_ret, entry_rule."""
    px = prices.set_index(["symbol", "date"]).sort_index()
    raw, rule = [], []
    for _, r in df.iterrows():
        try:
            bar0 = px.loc[(r["symbol"], r["t0"])]
        except KeyError:
            raw.append(np.nan); rule.append(None)
            continue
        if r["filed_in_session"] and r["filing_day"] is not None:
            try:
                cin = px.loc[(r["symbol"], r["filing_day"])]["close"]
                raw.append(float(bar0["close"] / cin - 1) if cin else np.nan)
                rule.append("close_entry")
            except KeyError:
                raw.append(np.nan); rule.append(None)
        else:
            o = bar0["open"]
            raw.append(float(bar0["close"] / o - 1)
                       if o and np.isfinite(o) else np.nan)
            rule.append("open_entry")
    out = df.copy()
    out["raw_ret"] = raw
    out["entry_rule"] = rule
    return out[out["raw_ret"].notna()]


def leg_stats(leg: pd.DataFrame, dev_years: float, cost_bps: int,
              sign: int = 1) -> dict:
    daily = leg.assign(r=sign * leg["raw_ret"]).groupby("t0")["r"].mean()
    r_net = daily - cost_bps / 1e4
    total = float(np.prod(1 + r_net) - 1)
    # audit_v2 issue F: both Sharpe bases, labeled. Only sharpe_calendar is
    # comparable to a buy-and-hold Sharpe.
    sh = st.dual_sharpe(r_net.to_numpy(), dev_years)
    return {
        "trades": len(leg), "trade_days": len(daily),
        "trades_per_yr": round(len(leg) / dev_years, 1),
        "mean_per_trade_day_bps": round(float(r_net.mean()) * 1e4),
        "hit_rate": round(float((r_net > 0).mean()), 2),
        "total_return_pct": round(total * 100, 1),
        "annualized_pct": round(((1 + total) ** (1 / dev_years) - 1) * 100, 1),
        "sharpe_trade_day": round(sh["sharpe_trade_day"], 2)
                            if np.isfinite(sh["sharpe_trade_day"]) else None,
        "sharpe_calendar": round(sh["sharpe_calendar"], 2)
                           if np.isfinite(sh["sharpe_calendar"]) else None,
    }


def main() -> None:
    cfg = bs.load_config()
    panel = study.load_panel(cfg)
    ew = study.assemble_events(cfg, panel)
    prices = pd.read_parquet(bs.art_path("prices"))

    base = study.dev_sample(ew, cfg)
    t0s = pd.to_datetime(base["t0"])
    dev_years = max((t0s.max() - t0s.min()).days / 365.25, 1e-9)

    variants = {
        "liq5M": study.apply_liquidity(base, cfg["liquidity"]["primary_min_median_peso_volume"]),
        "liq1M": study.apply_liquidity(base, cfg["liquidity"]["variants"][0]),
    }

    frontier_rows, sim_rows = [], []
    for vname, dfv in variants.items():
        tr = trade_returns(dfv, prices)
        for cut in CUTOFFS:
            longs = tr[tr["s_ts"] > cut]
            if len(longs) < 5:
                continue
            for cost in COSTS:
                row = {"variant": vname, "cutoff": cut, "cost_bps_rt": cost,
                       **leg_stats(longs, dev_years, cost)}
                frontier_rows.append(row)
                if vname == "liq5M" and cut == HEADLINE_CUT:
                    sim_rows.append({"leg": f"long s_ts>+{cut}", **{k: v for k, v in row.items()
                                     if k not in ("variant", "cutoff")}})
        if vname == "liq5M":
            shorts = tr[tr["s_ts"] < -HEADLINE_CUT]
            for cost in COSTS:
                if len(shorts) >= 5:
                    sim_rows.append({"leg": f"short s_ts<-{HEADLINE_CUT}",
                                     "cost_bps_rt": cost,
                                     **{k: v for k, v in
                                        leg_stats(shorts, dev_years, cost, sign=-1).items()}})
            # entry-rule split at the headline cutoff (gross)
            print("entry-rule split (long s_ts>+1, gross, liq5M):")
            for rule, sub in tr[tr["s_ts"] > HEADLINE_CUT].groupby("entry_rule"):
                m = st.mean_t(sub["raw_ret"].to_numpy())
                print(f"  {rule:12s}: trades={len(sub):3d} mean={m['mean']*1e4:+.0f}bps t={m['t']:.2f}")

    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    fr = pd.DataFrame(frontier_rows)
    fr.to_csv(bs.RESULTS_DIR / "strategy_frontier.csv", index=False)
    pd.DataFrame(sim_rows).to_csv(bs.RESULTS_DIR / "strategy_sim.csv", index=False)

    # ---- phase E execution variants (X1-X4) on the incumbent long leg ----
    tr5 = trade_returns(variants["liq5M"], prices)
    longs = tr5[tr5["s_ts"] > HEADLINE_CUT].copy()
    px = prices.set_index(["symbol", "date"]).sort_index()
    cal = sorted(prices[prices["symbol"] == prices["symbol"].iloc[0]]["date"].unique())

    def stats_of(leg, weights=None, ret_col="raw_ret"):
        d = leg.assign(r=leg[ret_col] if weights is None
                       else leg[ret_col] * weights)
        if weights is not None:
            daily = d.groupby("t0").apply(
                lambda g: np.average(g[ret_col], weights=weights.loc[g.index])
                if weights.loc[g.index].sum() > 0 else np.nan)
        else:
            daily = d.groupby("t0")["r"].mean()
        daily = daily.dropna() - 25e-4
        total = float(np.prod(1 + daily) - 1)
        sh = st.dual_sharpe(np.asarray(daily, dtype=float), dev_years)
        return {"trades": len(leg), "trade_days": len(daily),
                "ann_pct": round(((1 + total) ** (1 / dev_years) - 1) * 100, 1),
                "sharpe_trade_day": round(sh["sharpe_trade_day"], 2)
                                    if np.isfinite(sh["sharpe_trade_day"]) else None,
                "sharpe_calendar": round(sh["sharpe_calendar"], 2)
                                   if np.isfinite(sh["sharpe_calendar"]) else None}

    xrows = [{"variant": "incumbent (equal-weight, exit close t0)",
              **stats_of(longs)}]
    w = 1.0 / longs["sigma_pre"].replace(0, np.nan)
    w = w.clip(upper=3 * w.median()).fillna(0)
    xrows.append({"variant": "X1 vol-scaled sizing", **stats_of(longs, weights=w)})
    for k in cfg["phase_e"]["gap_skip_ks"]:
        keep = longs[(longs["ar0_gap"].abs() <= k * longs["sigma_pre"]) |
                     longs["ar0_gap"].isna()]
        xrows.append({"variant": f"X2 skip if |gap|>{k}sigma", **stats_of(keep)})
    capped = (longs.assign(absig=longs["s_ts"].abs())
                    .sort_values("absig", ascending=False)
                    .groupby("t0").head(3))
    xrows.append({"variant": "X3 max 3 positions/day", **stats_of(capped)})
    # X4: exit at close(t+1)
    ext = []
    for _, r in longs.iterrows():
        i = np.searchsorted(cal, r["t0"])
        nxt = cal[i + 1] if i + 1 < len(cal) else None
        try:
            c1 = px.loc[(r["symbol"], nxt)]["close"] if nxt else np.nan
            c0 = px.loc[(r["symbol"], r["t0"])]["close"]
            ext.append(r["raw_ret"] + (c1 / c0 - 1) if np.isfinite(c1) else np.nan)
        except KeyError:
            ext.append(np.nan)
    longs["ret_t1"] = ext
    xrows.append({"variant": "X4 exit close(t+1)",
                  **stats_of(longs[longs["ret_t1"].notna()], ret_col="ret_t1")})
    xdf = pd.DataFrame(xrows)
    xdf.to_csv(bs.RESULTS_DIR / "strategy_execution_variants.csv", index=False)
    print("\n=== execution variants (long s_ts>+1, net 25bps) ===")
    print(xdf.to_string(index=False))

    print(f"\ndev span ~{dev_years:.1f} yrs; events with trade return: "
          f"liq5M={len(trade_returns(variants['liq5M'], prices))} "
          f"liq1M={len(trade_returns(variants['liq1M'], prices))}")
    print("\n=== frontier (long side) ===")
    show = fr[fr["cost_bps_rt"] == 25]
    print(show.to_string(index=False))
    print("\n=== headline (liq5M, cutoff +1.0) & short leg ===")
    print(pd.DataFrame(sim_rows).to_string(index=False))


if __name__ == "__main__":
    main()

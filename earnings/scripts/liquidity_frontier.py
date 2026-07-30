#!/usr/bin/env python3
"""liquidity_frontier.py — how far down-cap does the BMV edge survive?

Buckets every analyzable dev event by median peso volume, reports the effect
(FM spread, IC) and the NET strategy economics under the pre-committed cost
ladder, plus stale-price symptoms. Verdict rule (pre-committed): deepest
bucket with net mean/trade > 0 and FM t >= 2.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simulate_strategy import trade_returns


def main() -> None:
    cfg = bs.load_config()
    ladder = cfg["phase_g"]["liquidity_ladder"]
    panel = study.load_panel(cfg)
    ew = study.assemble_events(cfg, panel)
    dev = study.dev_sample(ew, cfg)                    # NO liquidity filter
    prices = pd.read_parquet(bs.art_path("prices"))
    tr = trade_returns(dev, prices)

    # stale-price symptom per EVENT: share of zero-move days over the 250
    # trading days ending at t0-11 (pre-event window; audit_v2 issue E — the
    # old version measured the last 250 days of the whole panel and attributed
    # them to events from years earlier)
    wide = prices.pivot_table(index="date", columns="symbol", values="adjclose")
    zero_move = wide.pct_change() == 0
    dates = wide.index.to_numpy()

    def prestale(symbol, t0):
        if symbol not in zero_move.columns:
            return np.nan
        i = int(np.searchsorted(dates, t0))
        lo, hi = max(0, i - 261), max(0, i - 11)
        if hi - lo < 60:
            return np.nan
        return float(zero_move[symbol].iloc[lo:hi].mean())

    dev = dev.copy()
    dev["prestale"] = [prestale(s, t) for s, t in zip(dev["symbol"], dev["t0"])]

    rows = []
    edges = [lvl["min_pv"] for lvl in ladder] + [np.inf]
    for i, lvl in enumerate(ladder):
        lo, hi = lvl["min_pv"], (edges[i - 1] if i > 0 else np.inf)
        b = dev[(dev["median_peso_volume"] >= lo) & (dev["median_peso_volume"] < hi)]
        tb = tr[(tr["median_peso_volume"] >= lo) & (tr["median_peso_volume"] < hi)]
        if len(b) < 20:
            rows.append({"bucket": f"{lo/1e6:g}-{'' if hi==np.inf else hi/1e6}M",
                         "n_events": len(b), "note": "too few"})
            continue
        d = study.add_terciles(b.copy())
        per_q = []
        for _, sub in d.groupby("period"):
            m3 = sub.loc[sub["tercile"] == 3.0, "ar0_cc"].mean()
            m1 = sub.loc[sub["tercile"] == 1.0, "ar0_cc"].mean()
            if np.isfinite(m3) and np.isfinite(m1):
                per_q.append(m3 - m1)
        fm = st.fama_macbeth(np.array(per_q))
        ic = st.spearman_ic(b["s_cs"].to_numpy(), b["ar0_cc"].to_numpy())
        longs = tb[tb["s_ts"] > 1.0]
        net = longs["raw_ret"].to_numpy() - lvl["cost_bps"] / 1e4
        nt = st.mean_t(net)
        stale = b["prestale"].mean()
        rows.append({
            "bucket": f"{lo/1e6:g}-{'' if hi == np.inf else f'{hi/1e6:g}'}M",
            "cost_bps": lvl["cost_bps"], "n_events": len(b),
            "n_companies": b["slug"].nunique(),
            "fm_spread_bps": round(fm["fm_mean"] * 1e4), "fm_t": round(fm["fm_t"], 2),
            "ic": round(ic["ic"], 3),
            "long_trades": len(longs),
            "net_mean_bps": round(float(np.nanmean(net)) * 1e4) if len(net) else None,
            "net_t": round(nt["t"], 2),
            "zero_move_share_pre": round(float(stale), 3),
        })
    out = pd.DataFrame(rows)
    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(bs.RESULTS_DIR / "liquidity_frontier.csv", index=False)
    print(out.to_string(index=False))

    ok = out[(out.get("net_mean_bps").notna()) & (out["net_mean_bps"] > 0) &
             (out["fm_t"] >= 2.0)] if "net_mean_bps" in out else out.iloc[0:0]
    if len(ok):
        deepest = ok.iloc[-1]
        print(f"\nVERDICT (pre-committed rule): tradable down to the "
              f"{deepest['bucket']} bucket (cost {deepest['cost_bps']}bps) — "
              f"net {deepest['net_mean_bps']}bps/trade, FM t={deepest['fm_t']}")
    else:
        print("\nVERDICT: no bucket passes both net>0 and FM t>=2")


if __name__ == "__main__":
    main()

"""Event-window return math over the frozen price panel.

All abnormal returns are market-adjusted: stock daily return minus ^MXX daily
return, computed on the ^MXX trading calendar (missing stock bars stay NaN and
propagate to any measure that needs them).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from earnlib.calendar import TradingCalendar


class PricePanel:
    def __init__(self, prices: pd.DataFrame, index_df: pd.DataFrame,
                 min_coverage: float = 0.8, **cal_kwargs):
        self.min_coverage = min_coverage
        index_df = index_df.sort_values("date").reset_index(drop=True)
        self.cal = TradingCalendar(list(index_df["date"]), **cal_kwargs)
        n = len(self.cal)
        self._pos = {d: i for i, d in enumerate(self.cal.dates)}

        def reindexed(sub: pd.DataFrame, col: str) -> np.ndarray:
            arr = np.full(n, np.nan)
            pos = [self._pos.get(d) for d in sub["date"]]
            vals = sub[col].to_numpy(dtype=float)
            for p, v in zip(pos, vals):
                if p is not None:
                    arr[p] = v
            return arr

        self.i_open = reindexed(index_df, "open")
        self.i_close = reindexed(index_df, "close")
        i_adj = reindexed(index_df, "adjclose")
        self.i_ret = np.full(n, np.nan)
        self.i_ret[1:] = i_adj[1:] / i_adj[:-1] - 1

        self.stocks: dict[str, dict[str, np.ndarray]] = {}
        for symbol, sub in prices.groupby("symbol"):
            sub = sub.sort_values("date")
            s = {c: reindexed(sub, c) for c in
                 ("open", "close", "adjclose", "adj_factor", "volume")}
            ret = np.full(n, np.nan)
            ret[1:] = s["adjclose"][1:] / s["adjclose"][:-1] - 1
            s["ar"] = ret - self.i_ret          # market-adjusted daily AR
            s["peso_volume"] = s["close"] * np.nan_to_num(s["volume"])
            self.stocks[symbol] = s

    def _car(self, ar: np.ndarray, p0: int, a: int, b: int) -> float:
        lo, hi = p0 + a, p0 + b
        if lo < 1 or hi >= len(ar):
            return np.nan
        window = ar[lo:hi + 1]
        need = hi - lo + 1
        if np.isfinite(window).sum() < self.min_coverage * need:
            return np.nan
        return float(np.nansum(window))

    def measures(self, symbol: str, t0, cfg: dict,
                 filing_day=None) -> dict | None:
        """All event measures for one (symbol, t0). Returns None if the symbol
        has no price data or t0 is off-calendar."""
        s = self.stocks.get(symbol)
        p0 = self._pos.get(t0)
        if s is None or p0 is None or p0 < 1:
            return None

        out: dict = {"t0": t0}
        out["ar0_cc"] = float(s["ar"][p0]) if np.isfinite(s["ar"][p0]) else np.nan
        out["i_ret0"] = float(self.i_ret[p0]) if np.isfinite(self.i_ret[p0]) else np.nan

        o0, c1 = s["open"][p0], s["close"][p0 - 1]
        f0, f1 = s["adj_factor"][p0], s["adj_factor"][p0 - 1]
        io0, ic1 = self.i_open[p0], self.i_close[p0 - 1]
        if all(np.isfinite(v) for v in (o0, c1, f0, f1, io0, ic1)) and c1 * f1 != 0:
            out["ar0_gap"] = float((o0 * f0) / (c1 * f1) - 1 - (io0 / ic1 - 1))
        else:
            out["ar0_gap"] = np.nan

        c0 = s["close"][p0]
        ic0, iopen0 = self.i_close[p0], self.i_open[p0]
        if all(np.isfinite(v) for v in (c0, o0, ic0, iopen0)) and o0 != 0:
            out["ar0_intra"] = float(c0 / o0 - 1 - (ic0 / iopen0 - 1))
        else:
            out["ar0_intra"] = np.nan

        for a, b in cfg["windows"]["pre"]:
            out[f"car_pre{abs(a)}"] = self._car(s["ar"], p0, a, b)
        for a, b in cfg["windows"]["post"]:
            out[f"car_post{b}"] = self._car(s["ar"], p0, a, b)

        lo_off, hi_off = cfg["windows"]["profile"]
        for off in range(lo_off, hi_off + 1):
            p = p0 + off
            out[f"ar_d{off}"] = (
                float(s["ar"][p]) if 1 <= p < len(s["ar"]) and np.isfinite(s["ar"][p])
                else np.nan
            )

        # at-announcement measure for in-session filings
        if filing_day is not None:
            pf = self._pos.get(filing_day)
            out["ar_filing_day"] = (
                float(s["ar"][pf]) if pf is not None and pf >= 1 and np.isfinite(s["ar"][pf])
                else np.nan
            )
        else:
            out["ar_filing_day"] = np.nan

        la, lb = cfg["liquidity"]["measure_window"]
        lo, hi = p0 + la, p0 + lb
        if lo >= 0:
            pv = s["peso_volume"][lo:hi + 1]
            pv = pv[np.isfinite(pv) & (pv > 0)]
            out["median_peso_volume"] = float(np.median(pv)) if len(pv) >= (hi - lo + 1) * 0.5 else np.nan
            # pre-event daily AR volatility (vol-standardized measures)
            ars = s["ar"][lo:hi + 1]
            ars = ars[np.isfinite(ars)]
            min_obs = cfg.get("refinements", {}).get("sar_min_obs", 30)
            out["sigma_pre"] = float(np.std(ars, ddof=1)) if len(ars) >= min_obs else np.nan
            # abnormal pre-report volume: last 5 sessions vs the baseline window
            pv5 = s["peso_volume"][p0 - 5:p0]
            pv5 = pv5[np.isfinite(pv5) & (pv5 > 0)]
            out["prevol_ratio"] = (
                float(np.median(pv5) / out["median_peso_volume"])
                if len(pv5) >= 3 and out["median_peso_volume"] and
                np.isfinite(out["median_peso_volume"]) and out["median_peso_volume"] > 0
                else np.nan)
        else:
            out["median_peso_volume"] = np.nan
            out["sigma_pre"] = np.nan

        # beta over [t0-11-250, t0-11] for the robustness variant
        be = p0 + cfg["beta"]["end_offset"]
        bs_ = be - cfg["beta"]["estimation_days"]
        if bs_ >= 1:
            ri = s["ar"][bs_:be + 1] + self.i_ret[bs_:be + 1]   # raw stock return
            rm = self.i_ret[bs_:be + 1]
            mask = np.isfinite(ri) & np.isfinite(rm)
            if mask.sum() >= cfg["beta"]["min_days"] and np.var(rm[mask]) > 0:
                out["beta"] = float(np.cov(ri[mask], rm[mask])[0, 1] / np.var(rm[mask], ddof=1))
            else:
                out["beta"] = np.nan
        else:
            out["beta"] = np.nan
        return out

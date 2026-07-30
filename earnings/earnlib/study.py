"""Event-study assembly and analysis, shared by run_event_study and run_validation."""
from __future__ import annotations

import numpy as np
import pandas as pd

from earnlib import bootstrap as bs
from earnlib import stats as st
from earnlib.calendar import TradingCalendar
from earnlib.eventmath import PricePanel

MEASURES = ["ar0_cc", "ar0_gap", "ar0_intra", "car_pre5", "car_pre10",
            "car_post5", "car_post20", "ar_filing_day"]


def load_panel(cfg: dict) -> PricePanel:
    prices = pd.read_parquet(bs.art_path("prices"))
    index_df = pd.read_parquet(bs.OUTPUTS_DIR / "index_mxx.parquet")
    return PricePanel(prices, index_df, cfg["windows"]["min_coverage"])


def merge_hist_currents(wide_cur: pd.DataFrame,
                        hist_cur: pd.DataFrame) -> pd.DataFrame:
    """Union of the XBRL and historical 'current' panels. XBRL wins on
    (slug, period) collisions — the caller-priority direction of
    combine_first was inverted before the v2 audit (issue B)."""
    return wide_cur.combine_first(hist_cur)


def availability_map() -> pd.Series:
    """(slug, period) -> information-availability timestamp (info_dt, else
    filed_dt) from the events table. Feed through availability_array for
    trailing_sue's filed= mask — every masked SUE shares this source."""
    ev_path = bs.art_path("events_all")
    if not ev_path.exists():
        ev_path = bs.art_path("events")
    events = pd.read_parquet(ev_path)
    events = events[events["in_universe"] & events["has_facts"]]
    date_col = "info_dt" if "info_dt" in events.columns else "filed_dt"
    return events.set_index(["slug", "period"])[date_col]


def assemble_events(cfg: dict, panel: PricePanel,
                    date_col: str = "info_dt") -> pd.DataFrame:
    """One row per analyzable event: surprise scores + all event-window measures.

    date_col: which timestamp is the information-arrival event. Default is
    info_dt (earlier of XBRL filing and matching press release); pass
    "filed_dt" for the XBRL-only robustness variant.
    """
    ev_path = bs.art_path("events_all")
    if not ev_path.exists():
        ev_path = bs.art_path("events")
    events = pd.read_parquet(ev_path)
    surprises = pd.read_parquet(bs.art_path("surprises"))
    if date_col not in events.columns:      # backward compat with old parquet
        date_col = "filed_dt"
    if date_col == "filed_dt" and "date_source" in events.columns:
        # xbrl-dates robustness variant: historical rows have no filed_dt
        events = events[events["filed_dt"].notna()]

    ev = events[events["in_universe"] & events["has_facts"] & events["has_prices"]].copy()
    ev["event_dt"] = ev[date_col]
    qe = {"1": "-03-31", "2": "-06-30", "3": "-09-30", "4": "-12-31"}
    ev["period_end"] = pd.to_datetime(
        ev["period"].str[:4] + ev["period"].str[5].map(qe))
    ev["filing_lag_days"] = (
        ev["event_dt"].dt.tz_localize(None) - ev["period_end"]).dt.days
    ev["is_stale"] = ev["filing_lag_days"] > cfg["event"]["max_filing_lag_days"]

    df = ev.merge(
        surprises.drop(columns=[c for c in ("filed_dt", "info_dt", "ticker")
                                if c in surprises.columns]),
        on=["slug", "period"], how="left",
    )

    rows = []
    for _, r in df.iterrows():
        al = panel.cal.align(r["event_dt"].to_pydatetime())
        if al is None:
            continue
        m = panel.measures(
            r["symbol"], al.t0, cfg,
            filing_day=al.filing_day if al.filed_in_session else None,
        )
        if m is None:
            continue
        rows.append({**r.to_dict(), **m,
                     "filed_in_session": al.filed_in_session,
                     "filing_day": al.filing_day})
    out = pd.DataFrame(rows)
    out["fiscal_q"] = out["period"].str[5]
    out["era"] = np.where(out["period"] < "2021-2T", "historical", "modern")

    # Refinement A: combined announcement return. In-session filings split the
    # reaction across the filing session and t0; after-hours: ar_react = ar0_cc.
    fd = out["ar_filing_day"].to_numpy(dtype=float)
    a0 = out["ar0_cc"].to_numpy(dtype=float)
    in_sess = out["filed_in_session"].to_numpy(dtype=bool)
    react = np.where(in_sess, a0 + fd, a0)   # NaN fd propagates (correct: unknown)
    out["ar_react"] = react

    # Refinement B: vol-standardized measures
    sig = out["sigma_pre"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        out["sar0_cc"] = np.where(sig > 0, a0 / sig, np.nan)
        out["sar_react"] = np.where(sig > 0, react / sig, np.nan)
    return out


def dev_sample(df: pd.DataFrame, cfg: dict, which: str = "dev",
               universe: str = "full", new_only: bool = False,
               era: str = "all") -> pd.DataFrame:
    """Analyzable events: scored, fresh filing, valid t0 bar; dev/holdout split;
    optional universe restriction (phase_a), new-companies-only replication,
    or era restriction (modern/historical/all)."""
    holdout = set(cfg["holdout"]["quarters"])
    holdout |= set(cfg.get("phase_d", {}).get("historical_holdout_quarters", []))
    if era != "all" and "era" in df.columns:
        df = df[df["era"] == era]
    d = df[df["s_cs"].notna() & ~df["is_stale"] & df["ar0_cc"].notna()].copy()
    phase_a_slugs = set(cfg["universe"].keys())
    if universe == "phase_a":
        d = d[d["slug"].isin(phase_a_slugs)]
    if new_only:
        d = d[~d["slug"].isin(phase_a_slugs)]
    if which == "dev":
        d = d[~d["period"].isin(holdout)]
    elif which == "holdout":
        d = d[d["period"].isin(holdout)]
    return d.reset_index(drop=True)


def apply_liquidity(df: pd.DataFrame, min_peso_volume: float | None) -> pd.DataFrame:
    if min_peso_volume is None:
        return df
    return df[df["median_peso_volume"] >= min_peso_volume].reset_index(drop=True)


def add_terciles(df: pd.DataFrame, col: str = "s_cs") -> pd.DataFrame:
    """Within-quarter terciles (T1 = most negative surprise, T3 = most positive)."""
    df = df.copy()
    df["tercile"] = np.nan
    for _, idx in df.groupby("period").groups.items():
        vals = df.loc[idx, col]
        if vals.notna().sum() < 6:
            continue
        df.loc[idx, "tercile"] = pd.qcut(vals.rank(method="first"), 3,
                                         labels=[1, 2, 3]).astype(float)
    return df


def add_quintiles(df: pd.DataFrame, cfg: dict, col: str = "s_cs") -> pd.DataFrame:
    """Within-quarter quintiles; only when the quarter pool is wide enough."""
    min_pool = cfg.get("refinements", {}).get("quintile_min_pool", 25)
    df = df.copy()
    df["quintile"] = np.nan
    for _, idx in df.groupby("period").groups.items():
        vals = df.loc[idx, col]
        if vals.notna().sum() < min_pool:
            continue
        df.loc[idx, "quintile"] = pd.qcut(vals.rank(method="first"), 5,
                                          labels=[1, 2, 3, 4, 5]).astype(float)
    return df


def bucket_table(df: pd.DataFrame, measure: str, bucket_col: str,
                 buckets: tuple[float, ...]) -> pd.DataFrame:
    """Per-bucket mean with plain t and Fama-MacBeth t; top-bottom spread."""
    rows = []
    for b in buckets:
        sub = df[df[bucket_col] == b]
        plain = st.mean_t(sub[measure].to_numpy())
        fm = st.fama_macbeth(sub.groupby("period")[measure].mean().to_numpy())
        rows.append({"bucket": f"{bucket_col[0].upper()}{int(b)}", **plain, **fm})
    hi, lo = buckets[-1], buckets[0]
    spread_q = []
    for _, sub in df.groupby("period"):
        mh = sub.loc[sub[bucket_col] == hi, measure].mean()
        ml = sub.loc[sub[bucket_col] == lo, measure].mean()
        if np.isfinite(mh) and np.isfinite(ml):
            spread_q.append(mh - ml)
    fm = st.fama_macbeth(np.array(spread_q))
    ph = df[df[bucket_col] == hi][measure].to_numpy()
    pl = df[df[bucket_col] == lo][measure].to_numpy()
    rows.append({
        "bucket": f"{bucket_col[0].upper()}{int(hi)}-{bucket_col[0].upper()}{int(lo)}",
        "mean": np.nanmean(ph) - np.nanmean(pl), "t": np.nan, "p": np.nan,
        "n": int(np.isfinite(ph).sum() + np.isfinite(pl).sum()), **fm,
    })
    return pd.DataFrame(rows)


def tradability_table(df: pd.DataFrame) -> pd.DataFrame:
    """Decompose the bucket spread into capturable pieces: overnight gap
    (NOT capturable for off-hours filings), open->close intraday (enter at
    open t0), and post-event drift (enter at close t0)."""
    rows = []
    pieces = [("ar0_cc", "total next-day (cc)"),
              ("ar0_gap", "overnight gap (not capturable)"),
              ("ar0_intra", "open->close t0 (capturable)"),
              ("car_post5", "close t0 -> t+5 (capturable)")]
    for measure, label in pieces:
        for t in (1.0, 3.0):
            sub = df[df["tercile"] == t]
            plain = st.mean_t(sub[measure].to_numpy())
            fm = st.fama_macbeth(sub.groupby("period")[measure].mean().to_numpy())
            rows.append({"piece": label, "bucket": f"T{int(t)}",
                         "mean_bps": plain["mean"] * 1e4, "fm_t": fm["fm_t"],
                         "n": plain["n"]})
        spread_q = []
        for _, sub in df.groupby("period"):
            m3 = sub.loc[sub["tercile"] == 3.0, measure].mean()
            m1 = sub.loc[sub["tercile"] == 1.0, measure].mean()
            if np.isfinite(m3) and np.isfinite(m1):
                spread_q.append(m3 - m1)
        fm = st.fama_macbeth(np.array(spread_q))
        rows.append({"piece": label, "bucket": "T3-T1",
                     "mean_bps": fm["fm_mean"] * 1e4, "fm_t": fm["fm_t"],
                     "n": len(spread_q)})
    return pd.DataFrame(rows)


def tercile_table(df: pd.DataFrame, measure: str) -> pd.DataFrame:
    """Per-tercile mean with plain t and Fama-MacBeth (per-quarter) t; T3-T1 spread."""
    rows = []
    for t in (1.0, 2.0, 3.0):
        sub = df[df["tercile"] == t]
        plain = st.mean_t(sub[measure].to_numpy())
        per_q = sub.groupby("period")[measure].mean().to_numpy()
        fm = st.fama_macbeth(per_q)
        rows.append({"bucket": f"T{int(t)}", **plain, **fm})

    spread_q = []
    for _, sub in df.groupby("period"):
        m3 = sub.loc[sub["tercile"] == 3.0, measure].mean()
        m1 = sub.loc[sub["tercile"] == 1.0, measure].mean()
        if np.isfinite(m3) and np.isfinite(m1):
            spread_q.append(m3 - m1)
    fm = st.fama_macbeth(np.array(spread_q))
    pooled = df[df["tercile"] == 3.0][measure].to_numpy()
    pooled1 = df[df["tercile"] == 1.0][measure].to_numpy()
    rows.append({
        "bucket": "T3-T1",
        "mean": np.nanmean(pooled) - np.nanmean(pooled1),
        "t": np.nan, "p": np.nan,
        "n": int(np.isfinite(pooled).sum() + np.isfinite(pooled1).sum()),
        **fm,
    })
    return pd.DataFrame(rows)


def spread_t3t1(df: pd.DataFrame, measure: str) -> float:
    """Pooled T3-T1 spread (the permutation-test statistic)."""
    m3 = df.loc[df["tercile"] == 3.0, measure].mean()
    m1 = df.loc[df["tercile"] == 1.0, measure].mean()
    return float(m3 - m1)


def sign_table(df: pd.DataFrame, measure: str, col: str = "s_ts",
               cutoff: float = 0.0) -> pd.DataFrame:
    """Tradable sign/cutoff buckets on the time-series-only score."""
    rows = []
    for name, mask in (
        (f"{col}>+{cutoff}", df[col] > cutoff),
        (f"{col}<-{cutoff}", df[col] < -cutoff),
    ):
        sub = df[mask]
        plain = st.mean_t(sub[measure].to_numpy())
        fm = st.fama_macbeth(sub.groupby("period")[measure].mean().to_numpy())
        rows.append({"bucket": name, **plain, **fm})
    return pd.DataFrame(rows)


def regression_table(df: pd.DataFrame, measures: list[str],
                     signal: str = "s_cs") -> pd.DataFrame:
    rows = []
    for m in measures:
        r = st.ols_fe_clustered(df[m].to_numpy(), df[signal].to_numpy(),
                                df["period"].to_numpy())
        ic = st.spearman_ic(df[signal].to_numpy(), df[m].to_numpy())
        rows.append({"measure": m, **r, "ic": ic["ic"], "ic_t": ic["t"]})
    return pd.DataFrame(rows)


def profile_table(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Day-by-day mean AR (bps) around the event, by tercile."""
    lo, hi = cfg["windows"]["profile"]
    rows = []
    for off in range(lo, hi + 1):
        col = f"ar_d{off}"
        row = {"day": off}
        for t in (1.0, 2.0, 3.0):
            row[f"T{int(t)}_bps"] = df.loc[df["tercile"] == t, col].mean() * 1e4
        rows.append(row)
    return pd.DataFrame(rows)

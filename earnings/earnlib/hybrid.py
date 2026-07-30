"""HYBRID-1: the system + analyst + AI decision cascade.

Spec frozen in study.yaml `hybrid.cascade` / `hybrid.sizing`, committed before
any AI read ran. Nothing here is fitted: 67 directional dev events cannot support
a learned blend, so every threshold is a constant declared in the config.

The cascade answers one question per report — buy, short, or stay neutral on the
next trading session — by consulting three sources in a fixed order of measured
precision:

    T1  the analyst's full-strength call     92.6% dev (25/27), and unpredictable
                                             from any structured feature
    T2  the AI's high-conviction read,       the text-encapsulated edge, required
        corroborated by a number             to be seconded by s_ts or margin-SUE
    T3  the system's precision ladder        79.3% dev (n=29) at 6% coverage
    T4  abstain                              the default

Abstention is not a failure mode here — it is most of the distribution, and the
tiers exist to buy precision with coverage.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from earnlib import signal as sg

T1, T2, T3, T4 = ("T1_analyst_full", "T2_ai_corroborated",
                  "T3_system_ladder", "T4_abstain")
TIERS = (T1, T2, T3, T4)
TRADING_TIERS = (T1, T2, T3)

# Thresholds are stated in prose in study.yaml `hybrid.cascade`; they are
# mirrored here as constants rather than re-declared as config keys, so the
# frozen block stays the single authority and this file cannot drift into
# holding a *different* number under a plausible-looking name.
S_TS_MIN = 2.0                  # T3: |s_ts| >= 2.0
AI_CONVICTION_REQUIRED = "high"  # T2: only high-conviction reads trade
FULL_STRENGTH = 1.0              # T1: analyst Positive / Negative
SOFT_STRENGTH = 0.5              # veto-only: "N to P" / "N to N"


@dataclass(frozen=True)
class Decision:
    tier: str
    action: str            # buy | short | neutral
    direction: int         # +1 | -1 | 0
    vetoed: bool           # a soft analyst call opposed a T2/T3 signal
    conflict: str | None   # inside T1: which sources opposed the analyst


def _sign(value) -> int:
    """Sign as an int, with NaN mapping to 0 (no opinion) rather than NaN."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return 0
    if not np.isfinite(x) or x == 0:
        return 0
    return 1 if x > 0 else -1


def _action(direction: int) -> str:
    return {1: "buy", -1: "short"}.get(direction, "neutral")


T1B = "T1b_analyst_soft"
SOFT_POLICIES = ("veto", "corroborated", "always")


def decide(row, cfg: dict, sigma_median: float, ai_enabled: bool = True,
           soft_policy: str = "veto") -> Decision:
    """One report in, one buy/short/neutral decision out.

    ``row`` needs the analyst's call (direction, strength), the system's signal
    (s_ts, margin_sue_z, sigma_pre) and — when the AI tier is enabled — the
    reader's verdict (ai_dir, ai_conviction). ``sigma_median`` is this event's
    point-in-time volatility cutoff (see :func:`pit_sigma_median`).
    """
    s_ts = row.get("s_ts")
    margin = row.get("margin_sue_z")
    sigma = row.get("sigma_pre")

    a_dir = int(row.get("direction") or 0)
    a_strength = float(row.get("strength") or 0.0)

    # ---- T1: the analyst commits ----------------------------------------
    if a_strength == FULL_STRENGTH and a_dir != 0:
        opposed = []
        if _sign(s_ts) != 0 and _sign(s_ts) != a_dir:
            opposed.append("system")
        ai_dir = int(row.get("ai_dir") or 0)
        if ai_enabled and ai_dir != 0 and ai_dir != a_dir:
            opposed.append("ai")
        # frozen conflict rule: the analyst wins, the conflict is recorded
        return Decision(T1, _action(a_dir), a_dir, False,
                        ",".join(opposed) or None)

    # ---- T1b: what a SOFT call is allowed to do -------------------------
    # Under the frozen HYBRID-1 ("veto") a soft call never creates a trade.
    # The other policies are the conviction-tiered diagnostic (study.yaml
    # `hybrid_conviction`), and exist only to measure whether the analyst's
    # weaker calls carry anything once corroborated — soft calls hit 52.5% on
    # their own, so the prior is that they dilute.
    if soft_policy not in SOFT_POLICIES:
        raise ValueError(f"unknown soft_policy {soft_policy!r}")
    soft = a_dir if a_strength == SOFT_STRENGTH else 0

    if soft != 0 and soft_policy != "veto":
        ai_dir = int(row.get("ai_dir") or 0)
        conviction = str(row.get("ai_conviction") or "").lower()
        corroborated = (
            _sign(margin) == soft
            or (ai_enabled and ai_dir == soft
                and conviction == AI_CONVICTION_REQUIRED)
        )
        if soft_policy == "always" or corroborated:
            return Decision(T1B, _action(soft), soft, False, None)

    # ---- T2: the AI reads the report, a number seconds it ----------------
    if ai_enabled:
        ai_dir = int(row.get("ai_dir") or 0)
        conviction = str(row.get("ai_conviction") or "").lower()
        if ai_dir != 0 and conviction == AI_CONVICTION_REQUIRED:
            corroborated = (_sign(s_ts) == ai_dir) or (_sign(margin) == ai_dir)
            if corroborated:
                if soft != 0 and soft != ai_dir:
                    return Decision(T2, "neutral", 0, True, None)
                return Decision(T2, _action(ai_dir), ai_dir, False, None)

    # ---- T3: the system's precision ladder -------------------------------
    d = _sign(s_ts)
    if (d != 0 and abs(float(s_ts)) >= S_TS_MIN and _sign(margin) == d
            and np.isfinite(sigma) and np.isfinite(sigma_median)
            and float(sigma) <= sigma_median):
        if soft != 0 and soft != d:
            return Decision(T3, "neutral", 0, True, None)
        return Decision(T3, _action(d), d, False, None)

    # ---- T4 ---------------------------------------------------------------
    return Decision(T4, "neutral", 0, False, None)


def pit_sigma_median(df: pd.DataFrame, trailing_quarters: int = 8,
                     min_events: int = 40) -> pd.Series:
    """Per-event volatility cutoff, computed only from earlier quarters.

    The A3 ladder that T3 inherits used "sigma_pre <= the dev median" — a
    whole-sample statistic no one could have known in advance. Here the cutoff
    is the median over the trailing 8 quarters of scope events that were already
    public, falling back to the full-scope median when too few exist to be
    stable. Quarter granularity is deliberate: every event in quarter q-1 is
    public before any event in quarter q, so the boundary needs no timestamp
    arithmetic to be point-in-time.
    """
    periods = sorted(df["period"].unique())
    fallback = float(df["sigma_pre"].median())
    out = pd.Series(fallback, index=df.index, dtype=float)
    for i, period in enumerate(periods):
        past = df[df["period"].isin(periods[max(0, i - trailing_quarters):i])]
        values = past["sigma_pre"].dropna()
        if len(values) >= min_events:
            out.loc[df["period"] == period] = float(values.median())
    return out


def apply_cascade(df: pd.DataFrame, cfg: dict, ai_enabled: bool = True,
                  soft_policy: str = "veto") -> pd.DataFrame:
    """Run the cascade over a frame of events; returns it with decision columns."""
    out = df.copy()
    sigma_med = pit_sigma_median(out)
    out["sigma_median_pit"] = sigma_med
    decisions = [decide(row, cfg, float(sigma_med.loc[idx]), ai_enabled,
                        soft_policy)
                 for idx, row in out.iterrows()]
    out["tier"] = [d.tier for d in decisions]
    out["action"] = [d.action for d in decisions]
    out["hybrid_dir"] = [d.direction for d in decisions]
    out["vetoed"] = [d.vetoed for d in decisions]
    out["conflict"] = [d.conflict for d in decisions]
    return out


def fit_tier_kelly(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Per-tier point-in-time fractional Kelly weights.

    Mirrors ``simulate_direction_magnitude.fit_class_kelly`` with the tier as the
    class: for each quarter, each tier's win rate and payoff are measured on the
    trailing 8 quarters of that tier's own trades, and the Kelly fraction follows
    from ``earnlib.signal.kelly_fraction``. Below ``min_tier_trades`` of history
    the pre-committed fallback is equal weight at ``f_cap`` — with 27 T1 trades
    across the dev sample that fallback is the common case, not the exception.
    """
    sizing = cfg["hybrid"]["sizing"]
    lam = float(sizing["lambda"])
    cap = float(sizing["f_cap"])
    trailing = int(sizing["calibration_quarters"])
    min_trades = int(sizing["min_tier_trades"])

    out = df.copy()
    out["kelly_f"] = cap
    traded = out[out["hybrid_dir"] != 0]
    if traded.empty:
        return out
    periods = sorted(out["period"].unique())
    for i, period in enumerate(periods):
        window = periods[max(0, i - trailing):i]
        train = traded[traded["period"].isin(window)]
        for tier in TRADING_TIERS:
            leg = train[train["tier"] == tier]
            mask = (out["period"] == period) & (out["tier"] == tier)
            if len(leg) < min_trades or not mask.any():
                continue
            ret = (leg["hybrid_dir"] * leg["raw_ret"]).to_numpy(dtype=float)
            ret = ret[np.isfinite(ret)]
            wins = ret > 0
            if len(ret) < min_trades or not wins.any() or wins.all():
                continue
            p_hat = float(wins.mean())
            b = float(ret[wins].mean() / abs(ret[~wins].mean()))
            out.loc[mask, "kelly_f"] = sg.kelly_fraction(p_hat, b, lam, cap)
    return out

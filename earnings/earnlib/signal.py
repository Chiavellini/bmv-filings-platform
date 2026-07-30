"""Signal layer: three-way classification, PIT conviction, fractional Kelly.

Spec pre-committed in study.yaml `signal_layer` (2026-07-28) BEFORE the first
evaluation run. All fitting is point-in-time: quarter q decisions come from
models fit strictly on earlier dev quarters.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Decision:
    label: str            # good | neutral | bad  (report quality)
    action: str           # buy | neutral | short
    p_hat: float | None   # calibrated win probability (None -> fallback)
    kelly_f: float        # fractional-Kelly weight in [0, f_cap]


@dataclass
class LegModel:
    intercept: float
    coef: float
    payoff_b: float       # mean(win) / |mean(loss)| on the calibration window
    n: int

    def p_win(self, abs_s: float) -> float:
        z = self.intercept + self.coef * abs_s
        return float(1.0 / (1.0 + np.exp(-z)))


@dataclass
class ConvictionModel:
    legs: dict            # action -> LegModel | None
    min_events: int

    def leg(self, action: str) -> LegModel | None:
        return self.legs.get(action)


def classify(s_ts: float, cfg: dict) -> tuple[str, str]:
    """(report label, trade action) from the tradable point-in-time signal.

    Strict inequalities match the incumbent rule (s_ts > +1.0 long)."""
    sl = cfg["signal_layer"]["classify"]
    if pd.isna(s_ts):
        return ("neutral", "neutral")
    if s_ts > sl["buy_min"]:
        return ("good", "buy")
    if s_ts < sl["short_max"]:
        return ("bad", "short")
    return ("neutral", "neutral")


def _fit_logistic_1d(x: np.ndarray, y: np.ndarray,
                     n_iter: int = 50) -> tuple[float, float]:
    """Newton-IRLS logistic fit of y on [1, x]; small-sample-safe."""
    X = np.column_stack([np.ones_like(x), x])
    beta = np.zeros(2)
    for _ in range(n_iter):
        z = X @ beta
        p = 1.0 / (1.0 + np.exp(-z))
        w = np.clip(p * (1 - p), 1e-6, None)
        H = X.T @ (X * w[:, None]) + 1e-6 * np.eye(2)   # ridge for separation
        g = X.T @ (y - p)
        step = np.linalg.solve(H, g)
        beta = beta + step
        if np.max(np.abs(step)) < 1e-10:
            break
    return float(beta[0]), float(beta[1])


def fit_conviction(past: pd.DataFrame, cfg: dict) -> ConvictionModel:
    """Fit per-leg win-probability + payoff on STRICTLY past traded events.

    ``past`` needs columns s_ts, raw_ret; rows are events the classifier would
    have traded (action != neutral) in the calibration window."""
    sl = cfg["signal_layer"]
    min_ev = int(sl["conviction"]["min_calibration_events"])
    legs: dict = {}
    for action, sign in (("buy", 1), ("short", -1)):
        leg = past[past["action"] == action]
        if len(leg) < max(10, min_ev // 4):
            legs[action] = None
            continue
        ret = sign * leg["raw_ret"].to_numpy(dtype=float)
        wins = (ret > 0).astype(float)
        mean_win = ret[ret > 0].mean() if (ret > 0).any() else np.nan
        mean_loss = ret[ret <= 0].mean() if (ret <= 0).any() else np.nan
        if not (np.isfinite(mean_win) and np.isfinite(mean_loss) and mean_loss != 0):
            legs[action] = None
            continue
        b = float(mean_win / abs(mean_loss))
        icpt, coef = _fit_logistic_1d(leg["s_ts"].abs().to_numpy(dtype=float), wins)
        legs[action] = LegModel(icpt, coef, b, len(leg))
    return ConvictionModel(legs=legs, min_events=min_ev)


def kelly_fraction(p_hat: float, b: float, lam: float, cap: float) -> float:
    """Fractional Kelly: f = lam * max(0, (p(b+1) - 1) / b), clipped to cap."""
    if not (np.isfinite(p_hat) and np.isfinite(b)) or b <= 0:
        return 0.0
    f = (p_hat * (b + 1.0) - 1.0) / b
    return float(np.clip(lam * max(0.0, f), 0.0, cap))


def analyst_overlay(df: pd.DataFrame, pred: pd.DataFrame,
                    w: float) -> pd.DataFrame:
    """Overlay analyst calls on the tradable signal (study.yaml `analyst`).

    Left-joins ``pred`` (analyst_predictions) on (slug, period) and rewrites
    s_ts <- s_ts + w * direction * strength. Rows without a call keep s_ts
    unchanged. ``df`` must already be dev-only (join after study.dev_sample —
    holdout predictions must never meet returns). Adds columns:
    s_ts_base (original signal), analyst_signal (0.0 when uncovered), covered.
    """
    out = df.merge(pred[["slug", "period", "direction", "strength"]],
                   on=["slug", "period"], how="left", validate="many_to_one")
    out["covered"] = out["direction"].notna()
    out["analyst_signal"] = (out["direction"] * out["strength"]).fillna(0.0)
    out["s_ts_base"] = out["s_ts"]
    out["s_ts"] = out["s_ts_base"] + w * out["analyst_signal"]
    return out.drop(columns=["direction", "strength"])


def decide(s_ts: float, model: ConvictionModel | None, cfg: dict,
           n_calibration: int = 0) -> Decision:
    sl = cfg["signal_layer"]
    label, action = classify(s_ts, cfg)
    if action == "neutral":
        return Decision(label, action, None, 0.0)
    lam, cap = float(sl["kelly"]["lambda"]), float(sl["kelly"]["f_cap"])
    leg = model.leg(action) if model is not None else None
    if leg is None or n_calibration < model.min_events:
        # pre-committed fallback: trade the leg equal-weight (weight resolved
        # per-day by the simulator; sentinel kelly_f < 0 marks fallback)
        return Decision(label, action, None, -1.0)
    p = leg.p_win(abs(s_ts))
    return Decision(label, action, p, kelly_fraction(p, leg.payoff_b, lam, cap))

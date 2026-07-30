"""HYBRID-1 cascade: tier precedence, the soft-call veto, and PIT sizing.

The cascade is the whole traded spec, and every threshold in it is frozen in
study.yaml. These tests pin the behaviour the config describes in prose, so a
refactor cannot quietly move a threshold or reorder a tier.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
import pytest

from earnlib import bootstrap as bs
from earnlib import hybrid as hy

CFG = bs.load_config()


def row(**kw) -> dict:
    base = {"direction": 0, "strength": 0.0, "s_ts": 0.0, "margin_sue_z": 0.0,
            "sigma_pre": 0.01, "ai_dir": 0, "ai_conviction": None}
    base.update(kw)
    return base


def decide(r: dict, sigma_median: float = 0.02, ai: bool = True):
    return hy.decide(r, CFG, sigma_median, ai_enabled=ai)


# --------------------------------------------------------------------------
# tier precedence
# --------------------------------------------------------------------------

def test_full_strength_analyst_wins_over_everything():
    """T1 fires even when the AI and the system both point the other way —
    the frozen conflict rule, backed by dev's 15 analyst-right/model-wrong
    events vs 8 the other way."""
    d = decide(row(direction=-1, strength=1.0, s_ts=3.0, margin_sue_z=3.0,
                   ai_dir=1, ai_conviction="high"))
    assert d.tier == hy.T1
    assert d.direction == -1 and d.action == "short"
    assert d.conflict is not None
    assert "system" in d.conflict and "ai" in d.conflict


def test_conflict_is_none_when_sources_agree():
    d = decide(row(direction=1, strength=1.0, s_ts=2.5, ai_dir=1,
                   ai_conviction="high"))
    assert d.tier == hy.T1 and d.conflict is None


def test_ai_tier_needs_high_conviction():
    medium = decide(row(ai_dir=1, ai_conviction="medium", s_ts=1.0))
    assert medium.tier != hy.T2


def test_ai_tier_needs_corroboration():
    """A high-conviction read with neither s_ts nor margin-SUE agreeing does
    not trade — the tier is 'AI corroborated', not 'AI alone'."""
    alone = decide(row(ai_dir=1, ai_conviction="high", s_ts=-1.0,
                       margin_sue_z=-1.0))
    assert alone.tier != hy.T2

    by_s_ts = decide(row(ai_dir=1, ai_conviction="high", s_ts=0.4,
                         margin_sue_z=-1.0))
    assert by_s_ts.tier == hy.T2 and by_s_ts.direction == 1

    by_margin = decide(row(ai_dir=-1, ai_conviction="high", s_ts=0.4,
                           margin_sue_z=-1.0))
    assert by_margin.tier == hy.T2 and by_margin.direction == -1


def test_ai_tier_absent_when_channel_disabled():
    """With the AI channel off the same row falls through to the system tiers,
    which is the reduced cascade the contamination gate pre-commits to."""
    r = row(ai_dir=1, ai_conviction="high", s_ts=0.4)
    assert decide(r, ai=True).tier == hy.T2
    assert decide(r, ai=False).tier == hy.T4


def test_system_ladder_requires_all_three_conditions():
    ok = row(s_ts=2.5, margin_sue_z=1.0, sigma_pre=0.01)
    assert decide(ok).tier == hy.T3

    assert decide({**ok, "s_ts": 1.9}).tier == hy.T4          # below |s_ts|
    assert decide({**ok, "margin_sue_z": -1.0}).tier == hy.T4  # margin opposes
    assert decide({**ok, "sigma_pre": 0.05}).tier == hy.T4     # too volatile


def test_ladder_threshold_is_inclusive_at_two():
    assert decide(row(s_ts=2.0, margin_sue_z=1.0)).tier == hy.T3


def test_abstain_is_the_default():
    assert decide(row()).tier == hy.T4
    assert decide(row()).action == "neutral"


# --------------------------------------------------------------------------
# the soft-call veto
# --------------------------------------------------------------------------

def test_soft_call_never_creates_a_trade():
    """'N to P' / 'N to N' carry no standalone edge — on their own they abstain."""
    d = decide(row(direction=1, strength=0.5))
    assert d.tier == hy.T4 and d.direction == 0


def test_soft_call_vetoes_an_opposing_system_signal():
    d = decide(row(direction=-1, strength=0.5, s_ts=2.5, margin_sue_z=1.0))
    assert d.tier == hy.T3 and d.action == "neutral"
    assert d.vetoed and d.direction == 0


def test_soft_call_vetoes_an_opposing_ai_signal():
    d = decide(row(direction=-1, strength=0.5, ai_dir=1, ai_conviction="high",
                   s_ts=0.4))
    assert d.tier == hy.T2 and d.vetoed and d.direction == 0


def test_agreeing_soft_call_does_not_veto():
    d = decide(row(direction=1, strength=0.5, s_ts=2.5, margin_sue_z=1.0))
    assert d.tier == hy.T3 and not d.vetoed and d.direction == 1


# --------------------------------------------------------------------------
# missing data must abstain, never guess
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["s_ts", "margin_sue_z", "sigma_pre"])
def test_nan_inputs_abstain(field):
    r = row(s_ts=2.5, margin_sue_z=1.0, sigma_pre=0.01)
    r[field] = float("nan")
    assert decide(r).tier == hy.T4


# --------------------------------------------------------------------------
# point-in-time sizing and the volatility cutoff
# --------------------------------------------------------------------------

def test_sigma_median_uses_only_earlier_quarters():
    """The A3 ladder's 'dev median' was a whole-sample statistic. The traded
    version may only look backwards — a later quarter's volatility cannot move
    an earlier quarter's cutoff."""
    df = pd.DataFrame({
        "period": ["2024-3T"] * 50 + ["2024-4T"] * 50,
        "sigma_pre": [0.01] * 50 + [0.99] * 50,
    })
    med = hy.pit_sigma_median(df, trailing_quarters=8, min_events=40)
    first = med[df["period"] == "2024-3T"].unique()
    second = med[df["period"] == "2024-4T"].unique()
    assert len(first) == 1 and len(second) == 1
    # quarter 2 sees only quarter 1's calm regime
    assert second[0] == pytest.approx(0.01)
    # quarter 1 has no history, so it falls back to the whole-scope median
    assert first[0] == pytest.approx(float(df["sigma_pre"].median()))


def test_kelly_falls_back_to_cap_without_enough_history():
    cap = float(CFG["hybrid"]["sizing"]["f_cap"])
    df = pd.DataFrame({
        "period": ["2025-1T", "2025-2T"],
        "tier": [hy.T1, hy.T1],
        "hybrid_dir": [1, 1],
        "raw_ret": [0.01, 0.02],
    })
    out = hy.fit_tier_kelly(df, CFG)
    assert (out["kelly_f"] == cap).all()


def test_kelly_never_exceeds_the_cap():
    cap = float(CFG["hybrid"]["sizing"]["f_cap"])
    rng = np.random.default_rng(0)
    quarters = [f"202{y}-{q}T" for y in (4, 5) for q in (1, 2, 3, 4)]
    df = pd.DataFrame({
        "period": np.repeat(quarters, 20),
        "tier": hy.T1,
        "hybrid_dir": 1,
        "raw_ret": rng.normal(0.05, 0.01, size=20 * len(quarters)),
    })
    out = hy.fit_tier_kelly(df, CFG)
    assert out["kelly_f"].max() <= cap + 1e-12
    assert (out["kelly_f"] >= 0).all()


# --------------------------------------------------------------------------
# reduced-cascade invariant
# --------------------------------------------------------------------------

def test_without_analyst_or_ai_the_cascade_is_exactly_the_ladder():
    """With both overlays off, every decision must be reproducible from the
    system predicate alone — the guard that the overlays are additive and
    cannot silently alter the incumbent signal path."""
    rng = np.random.default_rng(7)
    n = 300
    df = pd.DataFrame({
        "period": rng.choice(["2025-1T", "2025-2T", "2025-3T"], n),
        "s_ts": rng.normal(0, 1.5, n),
        "margin_sue_z": rng.normal(0, 1.0, n),
        "sigma_pre": rng.uniform(0.005, 0.04, n),
        "direction": 0, "strength": 0.0,
    })
    out = hy.apply_cascade(df, CFG, ai_enabled=False)
    expected = (
        (out["s_ts"].abs() >= hy.S_TS_MIN)
        & (np.sign(out["margin_sue_z"]) == np.sign(out["s_ts"]))
        & (out["sigma_pre"] <= out["sigma_median_pit"])
    )
    assert ((out["tier"] == hy.T3) == expected).all()
    assert (out.loc[expected, "hybrid_dir"]
            == np.sign(out.loc[expected, "s_ts"])).all()
    assert (out.loc[~expected, "hybrid_dir"] == 0).all()

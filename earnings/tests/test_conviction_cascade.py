"""Soft-call policies (study.yaml `hybrid_conviction`).

The frozen HYBRID-1 cascade lets a soft analyst call veto a trade but never
create one. The conviction-tiered diagnostic adds two more policies. The tests
that matter most are the ones pinning `veto` to *exactly* its old behaviour —
a diagnostic must not be able to move the spec it is measured against.
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


def decide(r, policy="veto", sigma_median=0.02, ai=True):
    return hy.decide(r, CFG, sigma_median, ai_enabled=ai, soft_policy=policy)


# --------------------------------------------------------------------------
# the frozen path must not move
# --------------------------------------------------------------------------

def test_veto_is_the_default():
    r = row(direction=1, strength=hy.SOFT_STRENGTH, margin_sue_z=1.0)
    assert decide(r).tier == hy.T4
    assert hy.decide(r, CFG, 0.02).tier == hy.T4      # no policy argument at all


def test_veto_never_lets_a_soft_call_trade_however_corroborated():
    """With every corroborating signal present but no independent tier able to
    fire, a soft call must still produce nothing under the frozen policy."""
    r = row(direction=1, strength=hy.SOFT_STRENGTH, margin_sue_z=2.0,
            ai_dir=1, ai_conviction="high", s_ts=1.0)
    # AI channel off and |s_ts| below the ladder: no other tier can fire, so
    # anything that trades here could only have come from the soft call.
    d = decide(r, "veto", ai=False)
    assert d.tier == hy.T4 and d.direction == 0
    # with the AI channel on, the trade that appears is T2's, not the analyst's
    assert decide(r, "veto", ai=True).tier == hy.T2


def test_veto_still_vetoes_an_opposing_system_signal():
    r = row(direction=-1, strength=hy.SOFT_STRENGTH, s_ts=2.5, margin_sue_z=1.0)
    d = decide(r, "veto")
    assert d.tier == hy.T3 and d.vetoed and d.direction == 0


# --------------------------------------------------------------------------
# corroborated
# --------------------------------------------------------------------------

def test_corroborated_trades_a_soft_call_when_margin_sue_agrees():
    d = decide(row(direction=1, strength=hy.SOFT_STRENGTH, margin_sue_z=1.5),
               "corroborated")
    assert d.tier == hy.T1B and d.direction == 1 and d.action == "buy"


def test_corroborated_trades_a_soft_call_when_the_ai_agrees_at_high_conviction():
    d = decide(row(direction=-1, strength=hy.SOFT_STRENGTH, margin_sue_z=1.0,
                   ai_dir=-1, ai_conviction="high"), "corroborated")
    assert d.tier == hy.T1B and d.direction == -1


def test_corroborated_ignores_a_medium_conviction_ai_agreement():
    d = decide(row(direction=-1, strength=hy.SOFT_STRENGTH, margin_sue_z=1.0,
                   ai_dir=-1, ai_conviction="medium"), "corroborated")
    assert d.tier != hy.T1B


def test_uncorroborated_soft_call_still_vetoes_under_corroborated():
    d = decide(row(direction=-1, strength=hy.SOFT_STRENGTH, s_ts=2.5,
                   margin_sue_z=1.0), "corroborated")
    assert d.tier == hy.T3 and d.vetoed and d.direction == 0


def test_ai_corroboration_is_ignored_when_the_ai_channel_is_disabled():
    """The gate disables the AI tier on dev; a soft call must not sneak in
    through AI agreement when the channel it came from is switched off."""
    r = row(direction=1, strength=hy.SOFT_STRENGTH, margin_sue_z=-1.0,
            ai_dir=1, ai_conviction="high")
    assert decide(r, "corroborated", ai=True).tier == hy.T1B
    assert decide(r, "corroborated", ai=False).tier != hy.T1B


# --------------------------------------------------------------------------
# always
# --------------------------------------------------------------------------

def test_always_trades_every_directional_call():
    d = decide(row(direction=1, strength=hy.SOFT_STRENGTH, margin_sue_z=-2.0),
               "always")
    assert d.tier == hy.T1B and d.direction == 1


def test_always_does_not_invent_a_direction_from_a_neutral_call():
    assert decide(row(direction=0, strength=0.0), "always").tier == hy.T4


# --------------------------------------------------------------------------
# precedence and validation
# --------------------------------------------------------------------------

def test_full_strength_still_outranks_a_soft_tier():
    d = decide(row(direction=-1, strength=hy.FULL_STRENGTH, margin_sue_z=2.0),
               "always")
    assert d.tier == hy.T1


def test_soft_tier_outranks_the_ai_and_system_tiers():
    r = row(direction=1, strength=hy.SOFT_STRENGTH, margin_sue_z=1.5,
            s_ts=2.5, ai_dir=1, ai_conviction="high")
    assert decide(r, "corroborated").tier == hy.T1B
    # without T1b the same row falls through to the next tier that qualifies,
    # which is T2 — the AI is high-conviction and seconded by s_ts
    assert decide(r, "veto").tier == hy.T2


def test_unknown_policy_is_rejected():
    with pytest.raises(ValueError):
        decide(row(), "sometimes")


def test_apply_cascade_veto_matches_the_no_argument_call():
    """Whole-frame guard: adding the policy argument changed nothing for the
    frozen path."""
    rng = np.random.default_rng(11)
    n = 200
    df = pd.DataFrame({
        "period": rng.choice(["2025-1T", "2025-2T", "2025-3T"], n),
        "s_ts": rng.normal(0, 1.5, n),
        "margin_sue_z": rng.normal(0, 1.0, n),
        "sigma_pre": rng.uniform(0.005, 0.04, n),
        "direction": rng.choice([-1, 0, 1], n),
        "strength": rng.choice([0.0, 0.5, 1.0], n),
        "ai_dir": rng.choice([-1, 0, 1], n),
        "ai_conviction": rng.choice(["high", "medium", None], n),
    })
    a = hy.apply_cascade(df, CFG)
    b = hy.apply_cascade(df, CFG, soft_policy="veto")
    for col in ("tier", "action", "hybrid_dir", "vetoed"):
        assert a[col].equals(b[col])
    assert (a["tier"] != hy.T1B).all(), "the frozen path must never emit T1b"


def test_policies_are_ordered_by_how_much_they_trade():
    """always >= corroborated >= veto in coverage, by construction."""
    rng = np.random.default_rng(3)
    n = 300
    df = pd.DataFrame({
        "period": "2025-2T",
        "s_ts": rng.normal(0, 1.5, n),
        "margin_sue_z": rng.normal(0, 1.0, n),
        "sigma_pre": rng.uniform(0.005, 0.04, n),
        "direction": rng.choice([-1, 1], n),
        "strength": rng.choice([0.5, 1.0], n),
        "ai_dir": 0, "ai_conviction": None,
    })
    counts = [int((hy.apply_cascade(df, CFG, soft_policy=p)["hybrid_dir"] != 0).sum())
              for p in ("veto", "corroborated", "always")]
    assert counts[0] <= counts[1] <= counts[2]

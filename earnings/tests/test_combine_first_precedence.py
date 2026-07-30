"""Issue B — XBRL must win over historical backfill on (slug, period) collisions.
compute_surprises.py:57 currently has the precedence inverted (inert today:
zero key overlap), pinned here via the merge helper the fix extracts."""
import pandas as pd
import pytest


def test_xbrl_wins_on_collision():
    from earnlib.study import merge_hist_currents

    idx = pd.MultiIndex.from_tuples(
        [("ac", "2021-1T"), ("ac", "2021-2T")], names=["slug", "period"])
    xbrl = pd.DataFrame({"revenue": [100.0, 110.0]}, index=idx)
    hist_idx = pd.MultiIndex.from_tuples(
        [("ac", "2021-1T"), ("ac", "2015-1T")], names=["slug", "period"])
    hist = pd.DataFrame({"revenue": [999.0, 50.0]}, index=hist_idx)

    merged = merge_hist_currents(xbrl, hist)
    assert merged.loc[("ac", "2021-1T"), "revenue"] == 100.0  # XBRL wins
    assert merged.loc[("ac", "2015-1T"), "revenue"] == 50.0   # hist-only kept
    assert merged.loc[("ac", "2021-2T"), "revenue"] == 110.0

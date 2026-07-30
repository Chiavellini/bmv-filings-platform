"""Issue F — strategy Sharpe must be reported on both bases, labeled:
trade-day basis (when-invested) and calendar basis (flat cash otherwise),
so it is comparable to buy-and-hold Sharpe."""
import numpy as np
import pytest


def test_dual_sharpe_values():
    from earnlib.stats import dual_sharpe

    daily = np.array([0.01] * 10 + [-0.005] * 10)
    years = 2.0
    out = dual_sharpe(daily, years)

    sd = daily.std(ddof=1)
    expect_trade = daily.mean() / sd * np.sqrt(len(daily) / years)
    cal = np.zeros(int(round(252 * years)))
    cal[:len(daily)] = daily
    expect_cal = cal.mean() / cal.std(ddof=1) * np.sqrt(252)

    assert np.isclose(out["sharpe_trade_day"], expect_trade)
    assert np.isclose(out["sharpe_calendar"], expect_cal)
    assert out["sharpe_calendar"] < out["sharpe_trade_day"]

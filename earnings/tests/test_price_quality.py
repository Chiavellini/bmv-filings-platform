"""Issue D — price panel quality: negative/zero adj_factor rows and
symbols with < min_bars must be dropped AND logged (never silently)."""
import pandas as pd
import pytest


def test_filter_drops_and_logs():
    from earnlib.quality import filter_price_quality

    good = pd.DataFrame({
        "symbol": "GOOD.MX", "date": pd.date_range("2024-01-01", periods=80),
        "close": 10.0, "adjclose": 9.5, "adj_factor": 0.95})
    neg = pd.DataFrame({
        "symbol": "URBI.MX", "date": pd.date_range("2024-01-01", periods=80),
        "close": 4.8, "adjclose": -0.26, "adj_factor": -0.055})
    single = pd.DataFrame({
        "symbol": ["TLEVISAB.MX"], "date": [pd.Timestamp("2024-01-02")],
        "close": [9.0], "adjclose": [9.0], "adj_factor": [1.0]})
    df = pd.concat([good, neg, single], ignore_index=True)

    clean, dropped = filter_price_quality(df, min_bars=60)
    assert set(clean["symbol"]) == {"GOOD.MX"}
    assert (clean["adj_factor"] > 0).all()
    assert {"URBI.MX", "TLEVISAB.MX"} <= set(dropped["symbol"])
    assert set(dropped.columns) >= {"symbol", "reason", "n_rows"}

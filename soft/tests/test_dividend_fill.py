"""Dividend-yield fills — offline. `trailing_dividends` de-spikes a lone special/artifact dividend so
the recurring yield shows (Herdez's 15.0-peso event among ~0.75 regulars would otherwise blank it)."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.download import market_data


def _ev(days_ago: float, amount: float) -> dict:
    return {"date": (dt.datetime.utcnow() - dt.timedelta(days=days_ago)).timestamp(), "amount": amount}


def _result(*events) -> dict:
    return {"events": {"dividends": {str(i): e for i, e in enumerate(events)}}}


def test_despikes_lone_special():
    # Herdez-shaped: two ~0.75 regulars + one 15.0 special in the trailing year → keep the recurring 1.5.
    r = _result(_ev(200, 0.75), _ev(100, 0.75), _ev(50, 15.0))
    assert market_data.trailing_dividends(r) == 1.5


def test_keeps_normal_stream():
    r = _result(_ev(300, 0.75), _ev(180, 0.80), _ev(60, 0.70))   # no outlier → sum all
    assert abs(market_data.trailing_dividends(r) - 2.25) < 1e-9


def test_two_events_not_despiked():
    # Only two payments → can't judge one against peers; keep both (conservative, no false de-spike).
    r = _result(_ev(120, 1.0), _ev(40, 10.0))
    assert market_data.trailing_dividends(r) == 11.0


def test_moderate_special_kept():
    # A 3× "special" is below the 5× threshold → kept (real semi-special, not an artifact).
    r = _result(_ev(200, 1.0), _ev(100, 1.0), _ev(50, 3.0))
    assert market_data.trailing_dividends(r) == 5.0


def test_excludes_out_of_window():
    r = _result(_ev(500, 5.0), _ev(30, 1.0))                      # 500d ago is outside 366d window
    assert market_data.trailing_dividends(r) == 1.0


def test_none_when_no_events():
    assert market_data.trailing_dividends({"events": {"dividends": {}}}) is None
    assert market_data.trailing_dividends({}) is None

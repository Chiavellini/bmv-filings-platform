"""Excel-calendar session semantics against the trading calendar."""
from __future__ import annotations

import sys
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from earnlib.calendar import CDMX, TradingCalendar

# Mon 2025-07-14 .. Fri 2025-07-25, with Wed 2025-07-16 a fake holiday
DATES = [date(2025, 7, d) for d in (14, 15, 17, 18, 21, 22, 23, 24, 25)]
CAL = TradingCalendar(DATES)


def _t0(dt: datetime) -> date:
    al = CAL.align(dt)
    assert al is not None
    return al.t0


def excel_dt(cal_date: date, session: str) -> datetime:
    return datetime.combine(
        cal_date, time(6, 0) if session == "pre_open" else time(16, 0),
        tzinfo=CDMX)


def test_cierre_maps_to_next_trading_day():
    assert _t0(excel_dt(date(2025, 7, 14), "after_close")) == date(2025, 7, 15)


def test_apertura_maps_to_same_day():
    assert _t0(excel_dt(date(2025, 7, 15), "pre_open")) == date(2025, 7, 15)


def test_friday_after_close_rolls_to_monday():
    assert _t0(excel_dt(date(2025, 7, 18), "after_close")) == date(2025, 7, 21)


def test_cierre_before_holiday_skips_it():
    # Tue 15 after close; Wed 16 is not a trading day -> t0 Thu 17
    assert _t0(excel_dt(date(2025, 7, 15), "after_close")) == date(2025, 7, 17)


def test_weekend_calendar_date_rolls_forward():
    assert _t0(excel_dt(date(2025, 7, 19), "after_close")) == date(2025, 7, 21)


def test_year_corrected_dates_stay_in_period_window():
    """The 2Q25/3Q25 blocks' corrected dates must obey the hard bound the
    builder enforces: period_end < cal_date <= period_end + 120d."""
    import pandas as pd

    out = Path(__file__).resolve().parents[1] / "outputs" / "report_calendar.parquet"
    if not out.exists():
        import pytest

        pytest.skip("report_calendar.parquet not built")
    df = pd.read_parquet(out)
    corr = df[df["year_corrected"]]
    assert len(corr) > 0
    qend = {"1": (3, 31), "2": (6, 30), "3": (9, 30), "4": (12, 31)}
    for _, r in corr.iterrows():
        y, q = int(r["period"][:4]), r["period"][5]
        pend = date(y, *qend[q])
        delta = (r["cal_date"] - pend).days
        assert 0 < delta <= 120, (r["ticker"], r["period"], r["cal_date"])

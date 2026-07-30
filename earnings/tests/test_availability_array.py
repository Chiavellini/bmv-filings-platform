"""Shared availability-timestamp helper (issue A fix): real filing timestamps
pass through; undated quarters fall back to period_end + 90d @ 23:59 CDMX,
usable for masking only (never as events)."""
from datetime import datetime

import pandas as pd
import pytest


def test_fallback_and_passthrough():
    from earnlib.calendar import CDMX
    from earnlib.surprises import availability_array

    filed_real = pd.Timestamp("2020-04-28 16:00", tz="America/Mexico_City")
    arr = availability_array(["2020-1T", "2020-2T"], [filed_real, pd.NaT])
    assert arr[0] == filed_real.to_pydatetime()
    # 2020-2T ends 2020-06-30; +90d = 2020-09-28
    assert arr[1] == datetime(2020, 9, 28, 23, 59, tzinfo=CDMX)


def test_q4_fallback():
    from earnlib.calendar import CDMX
    from earnlib.surprises import availability_array

    arr = availability_array(["2019-4T"], [pd.NaT])
    # 2019-12-31 + 90d = 2020-03-30
    assert arr[0] == datetime(2020, 3, 30, 23, 59, tzinfo=CDMX)

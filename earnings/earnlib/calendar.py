"""BMV trading calendar + event-day (t0) alignment.

The calendar is the sorted date index of the ^MXX daily bars — the set of days
the Mexican market actually traded. t0 for a filing is the first trading day
whose session open (08:30 CDMX) is strictly after the filing timestamp, so an
in-session or post-close filing on day D maps to D+1's session.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

CDMX = ZoneInfo("America/Mexico_City")
SESSION_OPEN = time(8, 30)


@dataclass
class Alignment:
    t0: date               # reaction day
    filing_day: date | None  # trading day containing filed_dt's session, if any
    filed_in_session: bool


class TradingCalendar:
    def __init__(self, dates: list[date], tz: ZoneInfo = CDMX,
                 session_open: time = SESSION_OPEN,
                 session_close: time = time(15, 0)):
        self.tz = tz
        self.session_open_t = session_open
        self.session_close_t = session_close
        self.dates = sorted(set(dates))
        self._session_opens = [
            datetime.combine(d, session_open, tzinfo=tz) for d in self.dates
        ]

    def __len__(self) -> int:
        return len(self.dates)

    def index_of(self, d: date) -> int | None:
        i = bisect.bisect_left(self.dates, d)
        if i < len(self.dates) and self.dates[i] == d:
            return i
        return None

    def shift(self, d: date, n: int) -> date | None:
        """Trading day n steps from d (d must be a trading day)."""
        i = self.index_of(d)
        if i is None:
            return None
        j = i + n
        if 0 <= j < len(self.dates):
            return self.dates[j]
        return None

    def align(self, filed_dt: datetime) -> Alignment | None:
        """Map a tz-aware filing timestamp to its reaction day t0."""
        if filed_dt.tzinfo is None:
            raise ValueError("filed_dt must be tz-aware")
        i = bisect.bisect_right(self._session_opens, filed_dt)
        if i >= len(self.dates):
            return None
        t0 = self.dates[i]
        filed_local = filed_dt.astimezone(self.tz)
        filing_day = filed_local.date() if self.index_of(filed_local.date()) is not None else None
        in_session = (
            filing_day is not None
            and self.session_open_t <= filed_local.time() <= self.session_close_t
        )
        return Alignment(t0=t0, filing_day=filing_day, filed_in_session=in_session)

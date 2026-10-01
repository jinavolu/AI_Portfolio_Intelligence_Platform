"""Injectable clock so freshness gates, holding periods and tax flags are testable."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def ist_day_start(now: datetime) -> datetime:
    """Midnight IST of `now`'s IST day, in UTC: where the daily AI caps reset."""
    return datetime.combine(now.astimezone(IST).date(), time(0), tzinfo=IST).astimezone(timezone.utc)


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today_ist(self) -> date:
        return self.now().astimezone(IST).date()


class FixedClock(Clock):
    def __init__(self, at: datetime):
        if at.tzinfo is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")
        self._at = at

    def now(self) -> datetime:
        return self._at

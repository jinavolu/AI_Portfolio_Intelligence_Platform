"""Deterministic synthetic market data for the fixture broker.

These prices are NOT real. They exist so the whole pipeline runs, and tests are
reproducible, before Phase 0 has produced real sample payloads.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

from app.broker.models import Candle

SERIES_START = date(2016, 1, 4)

# symbol -> (start price, drift per day in early regime, drift in late regime, daily vol, base volume)
SYNTHETIC_PARAMS: dict[str, tuple[float, float, float, float, int]] = {
    "INFY": (1500, 0.0003, 0.0004, 0.014, 6_000_000),
    "HDFCBANK": (1600, 0.0002, 0.0001, 0.011, 9_000_000),
    "TCS": (3300, 0.0004, -0.0012, 0.012, 2_500_000),
    "ITC": (330, 0.0006, 0.0002, 0.010, 12_000_000),
    "RELIANCE": (2500, 0.0001, 0.0009, 0.013, 7_000_000),
    "TATAMOTORS": (420, 0.0012, -0.0015, 0.020, 15_000_000),
    "ASIANPAINT": (3100, -0.0002, -0.0004, 0.012, 1_200_000),
    "SBIN": (600, 0.0005, 0.0010, 0.016, 14_000_000),
    "NIFTY 50": (18000, 0.0004, 0.0003, 0.009, 250_000_000),  # benchmark for backtests
}


def trading_days(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def generate_candles(symbol: str, end: date) -> list[Candle]:
    """Full synthetic series from SERIES_START to `end`. Same symbol + date → same candle."""
    if symbol not in SYNTHETIC_PARAMS:
        return []
    start_price, drift_early, drift_late, vol, base_volume = SYNTHETIC_PARAMS[symbol]
    rng = random.Random(f"synthetic:{symbol}")
    days = trading_days(SERIES_START, end)
    regime_switch = date(2025, 6, 2)

    candles: list[Candle] = []
    close = start_price
    for d in days:
        drift = drift_early if d < regime_switch else drift_late
        open_ = close * (1 + rng.gauss(0, vol / 4))
        close = close * math.exp(drift + rng.gauss(0, vol))
        high = max(open_, close) * (1 + abs(rng.gauss(0, vol / 2)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, vol / 2)))
        volume = int(base_volume * math.exp(rng.gauss(0, 0.35)))
        candles.append(
            Candle(date=d, open=round(open_, 2), high=round(high, 2), low=round(low, 2),
                   close=round(close, 2), volume=volume)
        )
    return candles

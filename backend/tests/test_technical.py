from datetime import date, timedelta

import pytest

from app.broker.models import Candle
from app.engines.technical import (TechnicalParams, compute_technicals, ema, macd, rsi, sma, swing_highs,
                                   swing_lows)


def candles_from(closes, volumes=None, spread=1.0):
    start = date(2024, 1, 1)
    volumes = volumes or [1000] * len(closes)
    return [Candle(date=start + timedelta(days=i), open=c, high=c + spread, low=c - spread, close=c, volume=v)
            for i, (c, v) in enumerate(zip(closes, volumes))]


def test_sma():
    assert sma([1, 2, 3, 4, 5], 3) == [None, None, 2, 3, 4]


def test_ema_seeded_with_sma():
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None] and out[2] == 2
    assert out[3] == pytest.approx(0.5 * 4 + 0.5 * 2)
    assert out[4] == pytest.approx(0.5 * 5 + 0.5 * 3)


def test_rsi_extremes_and_wilder_value():
    assert rsi(list(range(1, 30)), 14)[-1] == 100.0
    assert rsi(list(range(30, 1, -1)), 14)[-1] == 0.0
    # Wilder's classic example, rounded to 2 decimals: gains 3.34, losses 1.40 over 14 changes,
    # RSI = 100 - 100 / (1 + 3.34 / 1.40) = 70.46. (The often-quoted 70.53 uses the unrounded closes.)
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
              46.28]
    assert rsi(closes, 14)[14] == pytest.approx(70.46, abs=0.01)


def test_macd_histogram_positive_in_uptrend():
    line, sig, hist = macd([100 + i * (1 + i / 50) for i in range(80)])
    assert line[24] is None and line[25] is not None
    assert sig[25 + 7] is None and sig[25 + 8] is not None
    assert hist[-1] > 0


def test_swing_points():
    lows = [5, 4, 3, 4, 5, 4, 5]
    assert swing_lows(lows, 2) == [2]
    assert swing_highs([1, 2, 5, 2, 1, 2, 1], 2) == [2]


def test_trend_up_long_term_above_and_breakout():
    closes = [100 + i * 0.5 for i in range(260)] + [260.0]
    volumes = [1000] * 260 + [3000]
    t = compute_technicals(candles_from(closes, volumes))
    assert t.trend == "UP" and t.long_term_trend == "ABOVE_SMA200"
    assert t.breakout is True and t.breakdown is False
    assert t.volume_ratio == 3.0
    assert t.high_52w == 261.0


def test_trend_down():
    t = compute_technicals(candles_from([400 - i * 0.5 for i in range(260)]))
    assert t.trend == "DOWN" and t.long_term_trend == "BELOW_SMA200"


def test_breakout_needs_volume():
    closes = [100 + i * 0.5 for i in range(260)] + [260.0]
    assert compute_technicals(candles_from(closes)).breakout is False


def test_support_and_resistance_definitions():
    # Oscillating series: swing lows at 90, swing highs at 110; current close 100.
    closes = [100 + 10 * ((-1) ** (i // 10)) * min(i % 10, 10 - i % 10) / 5 for i in range(260)]
    closes[-1] = 100.0
    t = compute_technicals(candles_from(closes, spread=0), TechnicalParams(pivot_window=3))
    assert t.support is not None and t.support < 100
    assert t.resistance is not None and t.resistance > 100


def test_short_history_yields_none_not_zero():
    t = compute_technicals(candles_from([100.0] * 60))
    assert t.sma200 is None and t.trend is None and t.long_term_trend is None
    assert t.ema50 is not None

"""Per-day indicator series using the SAME definitions and primitives as the Technical Engine.

compute_technicals() reports the last day only; a backtest needs every day. This module
evaluates the identical rules at each index i using data up to and including i (no
look-ahead). `check_parity` compares the last row with compute_technicals.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.broker.models import Candle
from app.engines.technical import TechnicalParams, compute_technicals, ema, macd, rsi, sma


def indicator_rows(candles: list[Candle], p: TechnicalParams | None = None) -> list[SimpleNamespace]:
    """One row per candle with the fields the Decision Engine's scoring reads."""
    p = p or TechnicalParams()
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    vols = [c.volume for c in candles]
    e_fast, e_slow = ema(closes, p.ema_fast), ema(closes, p.ema_slow)
    s_long = sma(closes, p.sma_long)
    r = rsi(closes, p.rsi_period)
    _, _, m_hist = macd(closes, p.macd_fast, p.macd_slow, p.macd_signal)

    rows = []
    for i, c in enumerate(candles):
        close, ema50, sma200 = closes[i], e_slow[i], s_long[i]
        trend = long_term = None
        if sma200 is not None:
            long_term = "ABOVE_SMA200" if close > sma200 else "BELOW_SMA200"
            past = s_long[i - p.trend_slope_lookback] if i >= p.trend_slope_lookback else None
            if ema50 is not None and past is not None:
                if close > ema50 > sma200 and sma200 > past:
                    trend = "UP"
                elif close < ema50 < sma200 and sma200 < past:
                    trend = "DOWN"
                else:
                    trend = "SIDEWAYS"
        volume_ratio = None
        if i >= p.volume_lookback:
            prev = vols[i - p.volume_lookback:i]
            avg = sum(prev) / len(prev)
            volume_ratio = vols[i] / avg if avg else None
        breakout = breakdown = False
        if i >= p.breakout_lookback and volume_ratio is not None:
            strong = volume_ratio >= p.breakout_volume_ratio
            breakout = close > max(highs[i - p.breakout_lookback:i]) and strong
            breakdown = close < min(lows[i - p.breakout_lookback:i]) and strong
        rows.append(SimpleNamespace(
            date=c.date, open=c.open, close=close,
            ema20=e_fast[i], ema50=ema50, sma200=sma200,
            # Rounded exactly like TechnicalReport, so scores match the live engine.
            rsi14=None if r[i] is None else round(r[i], 2),
            macd_hist=None if m_hist[i] is None else round(m_hist[i], 2),
            trend=trend, long_term_trend=long_term, breakout=breakout, breakdown=breakdown,
        ))
    return rows


def check_parity(candles: list[Candle]) -> dict:
    """Last row vs compute_technicals: every scoring field must agree."""
    live = compute_technicals(candles)
    last = indicator_rows(candles)[-1]
    fields = ["trend", "long_term_trend", "rsi14", "macd_hist", "breakout", "breakdown"]
    return {f: (getattr(live, f), getattr(last, f)) for f in fields if getattr(live, f) != getattr(last, f)}

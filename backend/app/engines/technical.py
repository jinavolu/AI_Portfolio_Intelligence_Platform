"""Technical Engine: indicators and written, parameterised definitions.

Definitions (every one has a unit test):

- EMA(n): seeded with the SMA of the first n closes, then alpha = 2 / (n + 1).
- RSI(14): Wilder's smoothing of average gain / average loss.
- MACD(12, 26, 9): EMA12 - EMA26; signal = EMA9 of MACD; histogram = MACD - signal.
- Volume ratio: today's volume / mean volume of the previous 20 sessions.
- 52-week range: high/low over the last 252 sessions (including today).
- Trend:
    UP        close > EMA50 > SMA200 and SMA200 higher than 20 sessions ago
    DOWN      close < EMA50 < SMA200 and SMA200 lower than 20 sessions ago
    SIDEWAYS  otherwise
- Long-term trend: ABOVE_SMA200 / BELOW_SMA200.
- Swing low/high: a session whose low (high) is the lowest (highest) of the
  `pivot_window` sessions on each side.
- Support: the highest swing low below today's close within `sr_lookback` sessions.
- Resistance: the lowest swing high above today's close within `sr_lookback` sessions.
- Breakout: close above the highest high of the previous `breakout_lookback`
  sessions with volume ratio >= `breakout_volume_ratio`.
  Breakdown: the mirror image using lows.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.broker.models import Candle

# 1.1.0: also reports the previous 20-session high/low (the breakout/breakdown levels).
# 1.2.0: also reports the 5-vs-20-session volume ratio (D9).
# 1.3.0: also reports the breakout/breakdown test for the latest and previous session (D10).
# Every 1.0.0 value is unchanged.
ENGINE_VERSION = "technical-1.3.0"


class TechnicalParams(BaseModel):
    ema_fast: int = 20
    ema_slow: int = 50
    sma_long: int = 200
    rsi_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    volume_lookback: int = 20
    range_sessions: int = 252
    trend_slope_lookback: int = 20
    pivot_window: int = 5
    sr_lookback: int = 120
    breakout_lookback: int = 20
    breakout_volume_ratio: float = 1.5


class TechnicalReport(BaseModel):
    engine_version: str = ENGINE_VERSION
    params: TechnicalParams
    as_of: str
    sessions: int
    close: float
    ema20: float | None
    ema50: float | None
    sma200: float | None
    rsi14: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    volume_ratio: float | None
    high_52w: float | None
    low_52w: float | None
    pct_from_52w_high: float | None
    trend: str | None
    long_term_trend: str | None
    support: float | None
    resistance: float | None
    breakout: bool
    breakdown: bool
    # Highest high / lowest low of the previous `breakout_lookback` sessions (excluding today).
    prior_high: float | None = None
    prior_low: float | None = None
    # Mean volume of the last 5 sessions / mean of the last 20 (rules-1.2.0, D9); the second leaves out
    # the latest candle, for use while today's session is still open.
    volume_5d_ratio: float | None = None
    volume_5d_ratio_excl_last: float | None = None
    # Breakout/breakdown test of the latest session and of the one before it (rules-1.3.0, D10), so
    # the decision can use the last COMPLETED session while today's is still open.
    volume_event: dict | None = None
    volume_event_prev: dict | None = None


# --------------------------------------------------------------------------- primitives


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    s = sum(values[:n])
    out[n - 1] = s / n
    for i in range(n, len(values)):
        s += values[i] - values[i - n]
        out[i] = s / n
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    alpha = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def rsi(values: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= n:
        return out
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n

    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[n] = _rsi(avg_g, avg_l)
    for i in range(n + 1, len(values)):
        avg_g = (avg_g * (n - 1) + gains[i - 1]) / n
        avg_l = (avg_l * (n - 1) + losses[i - 1]) / n
        out[i] = _rsi(avg_g, avg_l)
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = ema(values, fast), ema(values, slow)
    line = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    defined = [x for x in line if x is not None]
    sig_defined = ema(defined, signal)
    sig: list[float | None] = [None] * (len(line) - len(defined)) + sig_defined
    hist = [l - s if l is not None and s is not None else None for l, s in zip(line, sig)]
    return line, sig, hist


def swing_lows(lows: list[float], w: int) -> list[int]:
    return [i for i in range(w, len(lows) - w)
            if lows[i] == min(lows[i - w:i + w + 1])]


def swing_highs(highs: list[float], w: int) -> list[int]:
    return [i for i in range(w, len(highs) - w)
            if highs[i] == max(highs[i - w:i + w + 1])]


# --------------------------------------------------------------------------- engine


def _last(xs: list[float | None]) -> float | None:
    return xs[-1] if xs else None


def _round(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(x, nd)


def volume_event(candles: list[Candle], p: TechnicalParams) -> dict | None:
    """Breakout/breakdown test for the LAST candle of `candles` (same definitions as `breakout`),
    with the numbers behind it. {"type": None, ...} when the session was neither."""
    n = max(p.breakout_lookback, p.volume_lookback)
    if len(candles) <= n:
        return None
    last = candles[-1]
    prior = candles[-p.breakout_lookback - 1:-1]
    hi, lo = max(c.high for c in prior), min(c.low for c in prior)
    vols = [c.volume for c in candles[-p.volume_lookback - 1:-1]]
    avg = sum(vols) / len(vols)
    vr = last.volume / avg if avg else None
    heavy = vr is not None and vr >= p.breakout_volume_ratio
    kind = "BREAKOUT" if heavy and last.close > hi else "BREAKDOWN" if heavy and last.close < lo else None
    return {"type": kind, "date": last.date.isoformat(), "close": _round(last.close),
            "level": _round(hi if kind == "BREAKOUT" else lo if kind == "BREAKDOWN" else None),
            "volume_ratio": _round(vr)}


def compute_technicals(candles: list[Candle], p: TechnicalParams | None = None) -> TechnicalReport | None:
    p = p or TechnicalParams()
    if not candles:
        return None
    candles = sorted(candles, key=lambda c: c.date)
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    vols = [c.volume for c in candles]
    close = closes[-1]

    e_fast = ema(closes, p.ema_fast)
    e_slow = ema(closes, p.ema_slow)
    s_long = sma(closes, p.sma_long)
    r = rsi(closes, p.rsi_period)
    m_line, m_sig, m_hist = macd(closes, p.macd_fast, p.macd_slow, p.macd_signal)

    volume_ratio = None
    if len(vols) > p.volume_lookback:
        prev = vols[-p.volume_lookback - 1:-1]
        avg = sum(prev) / len(prev)
        volume_ratio = vols[-1] / avg if avg else None

    def vol5_ratio(v: list[float]) -> float | None:
        """Mean volume of the last 5 sessions / mean of the last 20 (those 5 included)."""
        if len(v) < p.volume_lookback:
            return None
        avg20 = sum(v[-p.volume_lookback:]) / p.volume_lookback
        return (sum(v[-5:]) / 5) / avg20 if avg20 else None

    window = candles[-p.range_sessions:]
    high_52w = max(c.high for c in window)
    low_52w = min(c.low for c in window)

    ema50, sma200 = _last(e_slow), _last(s_long)
    trend = long_term = None
    if sma200 is not None:
        long_term = "ABOVE_SMA200" if close > sma200 else "BELOW_SMA200"
        past = s_long[-p.trend_slope_lookback - 1] if len(s_long) > p.trend_slope_lookback else None
        if ema50 is not None and past is not None:
            if close > ema50 > sma200 and sma200 > past:
                trend = "UP"
            elif close < ema50 < sma200 and sma200 < past:
                trend = "DOWN"
            else:
                trend = "SIDEWAYS"

    start = max(0, len(candles) - p.sr_lookback)
    lo_idx = [i for i in swing_lows(lows, p.pivot_window) if i >= start]
    hi_idx = [i for i in swing_highs(highs, p.pivot_window) if i >= start]
    supports = [lows[i] for i in lo_idx if lows[i] < close]
    resistances = [highs[i] for i in hi_idx if highs[i] > close]

    breakout = breakdown = False
    prior_high = prior_low = None
    if len(candles) > p.breakout_lookback:
        prior = candles[-p.breakout_lookback - 1:-1]
        prior_high, prior_low = max(c.high for c in prior), min(c.low for c in prior)
        if volume_ratio is not None:
            strong_volume = volume_ratio >= p.breakout_volume_ratio
            breakout = close > prior_high and strong_volume
            breakdown = close < prior_low and strong_volume

    return TechnicalReport(
        params=p,
        as_of=candles[-1].date.isoformat(),
        sessions=len(candles),
        close=close,
        ema20=_round(_last(e_fast)),
        ema50=_round(ema50),
        sma200=_round(sma200),
        rsi14=_round(_last(r)),
        macd=_round(_last(m_line)),
        macd_signal=_round(_last(m_sig)),
        macd_hist=_round(_last(m_hist)),
        volume_ratio=_round(volume_ratio),
        high_52w=_round(high_52w),
        low_52w=_round(low_52w),
        pct_from_52w_high=_round((close - high_52w) / high_52w, 4) if high_52w else None,
        trend=trend,
        long_term_trend=long_term,
        support=_round(max(supports)) if supports else None,
        resistance=_round(min(resistances)) if resistances else None,
        breakout=breakout,
        breakdown=breakdown,
        prior_high=_round(prior_high),
        prior_low=_round(prior_low),
        volume_5d_ratio=_round(vol5_ratio(vols), 3),
        volume_5d_ratio_excl_last=_round(vol5_ratio(vols[:-1]), 3),
        volume_event=volume_event(candles, p),
        volume_event_prev=volume_event(candles[:-1], p),
    )

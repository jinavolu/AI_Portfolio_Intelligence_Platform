"""Broker adapter backed by the sample tradebook and synthetic candles.

Used for development and tests until Phase 0 yields real payloads. Holdings are
derived from the sample tradebook so the Portfolio Engine reconciles cleanly.
"""

from __future__ import annotations

from datetime import date

from app.broker.base import BrokerAdapter
from app.broker.models import OHLC, Candle, Holding, Margins, Position, Quote, Trade
from app.broker.synthetic import SYNTHETIC_PARAMS, generate_candles
from app.broker.tradebook import parse_tradebook_csv
from app.clock import Clock
from app.config import SAMPLE_DATA_DIR
from app.engines.portfolio import fifo_open_lots


def load_sample_trades() -> list[Trade]:
    return parse_tradebook_csv((SAMPLE_DATA_DIR / "tradebook.csv").read_text(encoding="utf-8"))


class FixtureBrokerAdapter(BrokerAdapter):
    name = "fixture"

    def __init__(self, clock: Clock, trades: list[Trade] | None = None):
        self._clock = clock
        self._trades = trades if trades is not None else load_sample_trades()

    def _series(self, symbol: str) -> list[Candle]:
        return generate_candles(symbol, self._clock.today_ist())

    def get_holdings(self) -> list[Holding]:
        now = self._clock.now()
        holdings = []
        for symbol, lots in sorted(fifo_open_lots(self._trades).items()):
            qty = sum(l.quantity for l in lots)
            series = self._series(symbol)
            if qty <= 0 or len(series) < 2:
                continue
            avg = sum(l.quantity * l.price for l in lots) / qty
            holdings.append(
                Holding(symbol=symbol, exchange="NSE", quantity=int(qty), average_price=round(avg, 2),
                        last_price=series[-1].close, close_price=series[-2].close, as_of=now)
            )
        return holdings

    def get_positions(self) -> list[Position]:
        return []

    def get_margins(self) -> Margins:
        return Margins(available_cash=50_000.0, utilised=0.0, net=50_000.0, as_of=self._clock.now())

    def get_quote(self, symbols: list[str]) -> dict[str, Quote]:
        out = {}
        for s in symbols:
            series = self._series(s)
            if len(series) < 2:
                continue
            c = series[-1]
            out[s] = Quote(symbol=s, last_price=c.close, volume=c.volume, open=c.open, high=c.high,
                           low=c.low, close=series[-2].close, as_of=self._clock.now())
        return out

    def get_ohlc(self, symbols: list[str]) -> dict[str, OHLC]:
        return {s: OHLC(symbol=s, open=q.open, high=q.high, low=q.low, close=q.close,
                        last_price=q.last_price, as_of=q.as_of)
                for s, q in self.get_quote(symbols).items()}

    def get_historical(self, symbol: str, start: date, end: date) -> list[Candle]:
        if symbol not in SYNTHETIC_PARAMS:
            return []
        return [c for c in generate_candles(symbol, end) if start <= c.date <= end]

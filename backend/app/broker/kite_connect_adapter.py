"""Read-only adapter over the official `kiteconnect` client.

The raw client is wrapped in `_ReadOnlyKite`, which only forwards an allowlist of
read methods, so even code inside this module cannot reach order endpoints.
"""

from __future__ import annotations

from datetime import date

from app.broker.base import BrokerAdapter, BrokerError
from app.broker.kite_mapping import (
    candle_from_kite,
    holding_from_kite,
    margins_from_kite,
    ohlc_from_kite,
    position_from_kite,
    quote_from_kite,
)
from app.broker.models import OHLC, Candle, Holding, Margins, Position, Quote
from app.clock import Clock

READ_ONLY_KITE_METHODS = frozenset(
    {"holdings", "positions", "margins", "quote", "ohlc", "ltp", "historical_data", "instruments", "profile"}
)


class _ReadOnlyKite:
    def __init__(self, client):
        self.__client = client

    def __getattr__(self, name: str):
        if name not in READ_ONLY_KITE_METHODS:
            raise PermissionError(f"Kite method {name!r} is not on the read-only allowlist")
        method = getattr(self.__client, name)

        def call(*args, **kwargs):
            try:
                return method(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - TokenException (daily expiry), network errors, ...
                # As BrokerError the API answers "log in again" (503) instead of a bare 500.
                raise BrokerError(f"Kite {name} failed: {e}") from e

        return call


class KiteConnectAdapter(BrokerAdapter):
    name = "kite_connect"

    def __init__(self, api_key: str, access_token: str, clock: Clock, client=None):
        if client is None:
            try:
                from kiteconnect import KiteConnect
            except ImportError as e:  # pragma: no cover
                raise BrokerError("Install the 'kite' extra: uv pip install -e .[kite]") from e
            client = KiteConnect(api_key=api_key)
            client.set_access_token(access_token)
        self._kite = _ReadOnlyKite(client)
        self._clock = clock
        self._tokens: dict[str, int] | None = None

    def get_holdings(self) -> list[Holding]:
        now = self._clock.now()
        return [holding_from_kite(h, now) for h in self._kite.holdings()]

    def get_positions(self) -> list[Position]:
        now = self._clock.now()
        return [position_from_kite(p, now) for p in self._kite.positions().get("net", [])]

    def get_margins(self) -> Margins:
        return margins_from_kite(self._kite.margins(), self._clock.now())

    def account_id(self) -> str | None:
        return self._kite.profile().get("user_id") or None

    def get_quote(self, symbols: list[str]) -> dict[str, Quote]:
        now = self._clock.now()
        raw = self._kite.quote([f"NSE:{s}" for s in symbols])
        return {q.symbol: q for q in (quote_from_kite(k, v, now) for k, v in raw.items())}

    def get_ohlc(self, symbols: list[str]) -> dict[str, OHLC]:
        now = self._clock.now()
        raw = self._kite.ohlc([f"NSE:{s}" for s in symbols])
        return {o.symbol: o for o in (ohlc_from_kite(k, v, now) for k, v in raw.items())}

    def _instrument_token(self, symbol: str) -> int:
        if self._tokens is None:
            self._tokens = {"NIFTY 50": 256265,
                            **{i["tradingsymbol"]: i["instrument_token"] for i in self._kite.instruments("NSE")}}
        if symbol not in self._tokens:
            raise BrokerError(f"No NSE instrument token for {symbol}")
        return self._tokens[symbol]

    def get_historical(self, symbol: str, start: date, end: date) -> list[Candle]:
        raw = self._kite.historical_data(self._instrument_token(symbol), start, end, "day")
        return [candle_from_kite(c) for c in raw]

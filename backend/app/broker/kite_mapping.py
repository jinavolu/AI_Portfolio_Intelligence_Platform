"""Map Kite Connect JSON shapes (also what Kite MCP wraps) into normalized models.

Field names follow the Kite Connect v3 docs. Phase 0 must confirm them against
saved sample payloads; tests/test_kite_mapping.py pins the expected shapes.
"""

from __future__ import annotations

from datetime import date, datetime

from app.broker.models import OHLC, Candle, Holding, Margins, Position, Quote


def holding_from_kite(raw: dict, as_of: datetime) -> Holding:
    return Holding(
        symbol=raw["tradingsymbol"],
        exchange=raw["exchange"],
        isin=raw.get("isin"),
        quantity=int(raw.get("quantity", 0)) + int(raw.get("t1_quantity", 0)),
        average_price=float(raw["average_price"]),
        last_price=float(raw["last_price"]),
        close_price=float(raw["close_price"]) if raw.get("close_price") else None,
        as_of=as_of,
    )


def position_from_kite(raw: dict, as_of: datetime) -> Position:
    return Position(
        symbol=raw["tradingsymbol"],
        exchange=raw["exchange"],
        product=raw.get("product", ""),
        quantity=int(raw["quantity"]),
        average_price=float(raw["average_price"]),
        last_price=float(raw["last_price"]),
        pnl=float(raw.get("pnl", 0.0)),
        as_of=as_of,
    )


def margins_from_kite(raw: dict, as_of: datetime) -> Margins:
    equity = raw.get("equity", raw)
    return Margins(
        available_cash=float(equity.get("available", {}).get("cash", 0.0)),
        utilised=float(equity.get("utilised", {}).get("debits", 0.0)),
        net=float(equity.get("net", 0.0)),
        as_of=as_of,
    )


def _symbol(key: str) -> str:
    return key.split(":", 1)[1] if ":" in key else key


def quote_from_kite(key: str, raw: dict, as_of: datetime) -> Quote:
    ohlc = raw.get("ohlc", {})
    return Quote(
        symbol=_symbol(key),
        last_price=float(raw["last_price"]),
        volume=raw.get("volume") or raw.get("volume_traded"),
        open=ohlc.get("open"),
        high=ohlc.get("high"),
        low=ohlc.get("low"),
        close=ohlc.get("close"),
        as_of=as_of,
    )


def ohlc_from_kite(key: str, raw: dict, as_of: datetime) -> OHLC:
    o = raw["ohlc"]
    return OHLC(symbol=_symbol(key), open=o["open"], high=o["high"], low=o["low"], close=o["close"],
                last_price=float(raw["last_price"]), as_of=as_of)


def candle_from_kite(raw: dict) -> Candle:
    d = raw["date"]
    if isinstance(d, str):
        d = datetime.fromisoformat(d.replace("Z", "+00:00"))
    if isinstance(d, datetime):
        d = d.date()
    assert isinstance(d, date)
    return Candle(date=d, open=raw["open"], high=raw["high"], low=raw["low"], close=raw["close"],
                  volume=int(raw.get("volume", 0)))

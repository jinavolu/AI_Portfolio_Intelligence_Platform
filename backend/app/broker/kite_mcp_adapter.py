"""Read-only adapter over the Kite MCP server.

The MCP session is injected as a `call_tool(name, arguments) -> payload` callable
(see mcp_session.McpSessionBridge). Only tools on READ_TOOLS can be called;
anything else - including every order tool the server exposes - raises before a
request is made.

Verified in Phase 0 against https://mcp.kite.trade/mcp: tool names, argument
names, and that payloads are Kite Connect JSON inside a single text block.
Historical candles are requested in windows of at most MAX_DAILY_WINDOW days.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from app.broker.base import BrokerAdapter, BrokerError
from app.broker.instruments import nse_tokens
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

READ_TOOLS = frozenset(
    {"get_holdings", "get_positions", "get_margins", "get_quotes", "get_ohlc", "get_ltp",
     "get_historical_data", "search_instruments", "get_profile", "get_trades"}
)

# Kite limits a daily-candle request to 2000 days; stay just under it (10 years = 2 requests).
MAX_DAILY_WINDOW = 1950
# Kite's historical API allows about 3 requests/second.
HISTORICAL_MIN_INTERVAL = 0.4
HISTORICAL_RETRIES = 3  # waits 1, 2, 4 s between attempts
# Index tokens are stable in Kite's instrument master; search results for indices are noisy.
KNOWN_TOKENS = {"NIFTY 50": 256265}

CallTool = Callable[[str, dict[str, Any]], Any]
# Errors worth retrying: rate limits, timeouts and one failed request (McpSessionBridge words these
# "Kite MCP call <tool> failed/timed out"). Not a pending login or a logged-out session.
_TRANSIENT = re.compile(r"too many|rate.?limit|429|timed out|timeout|try again|temporar|unavailable|"
                        r"Kite MCP call \S+ failed", re.I)


def _decode(payload: Any) -> Any:
    """MCP tools return content blocks; Kite MCP puts JSON in a text block."""
    if isinstance(payload, (list, dict)) and not (isinstance(payload, dict) and "content" in payload):
        return payload
    blocks = payload.get("content", []) if isinstance(payload, dict) else getattr(payload, "content", [])
    texts = [b.get("text") if isinstance(b, dict) else getattr(b, "text", None) for b in blocks]
    texts = [t for t in texts if t]
    is_error = payload.get("is_error") if isinstance(payload, dict) else getattr(payload, "is_error", False)
    if is_error:
        raise BrokerError("Kite MCP error: " + (" ".join(texts)[:200] or "unknown error")
                          + " (is the Kite session logged in?)")
    for text in texts:
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise BrokerError(f"Kite MCP returned non-JSON text: {text[:120]!r}") from e
    raise BrokerError("Kite MCP returned no parseable content")


class KiteMcpAdapter(BrokerAdapter):
    name = "kite_mcp"

    def __init__(self, call_tool: CallTool, clock: Clock):
        self.__call_tool = call_tool
        self._clock = clock
        self._tokens: dict[str, int] = dict(KNOWN_TOKENS)
        self._last_historical = 0.0
        self._pace_lock = threading.Lock()

    def _call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        if tool not in READ_TOOLS:
            raise PermissionError(f"MCP tool {tool!r} is not on the read-only allowlist")
        return _decode(self.__call_tool(tool, arguments or {}))

    def get_holdings(self) -> list[Holding]:
        now = self._clock.now()
        raw = self._call("get_holdings")
        rows = (raw.get("data", raw) if isinstance(raw, dict) else raw) or []  # paginated responses wrap rows
        for h in rows:
            if h.get("instrument_token"):
                self._tokens[h["tradingsymbol"]] = int(h["instrument_token"])
        return [holding_from_kite(h, now) for h in rows]

    def get_positions(self) -> list[Position]:
        now = self._clock.now()
        raw = self._call("get_positions")
        rows = (raw.get("net", raw.get("data", [])) if isinstance(raw, dict) else raw) or []
        return [position_from_kite(p, now) for p in rows]

    def get_margins(self) -> Margins:
        return margins_from_kite(self._call("get_margins"), self._clock.now())

    def account_id(self) -> str | None:
        raw = self._call("get_profile")
        profile = raw.get("data", raw) if isinstance(raw, dict) else {}
        return profile.get("user_id") or None

    def get_quote(self, symbols: list[str]) -> dict[str, Quote]:
        now = self._clock.now()
        raw = self._call("get_quotes", {"instruments": [f"NSE:{s}" for s in symbols]})
        return {q.symbol: q for q in (quote_from_kite(k, v, now) for k, v in raw.items())}

    def get_ohlc(self, symbols: list[str]) -> dict[str, OHLC]:
        now = self._clock.now()
        raw = self._call("get_ohlc", {"instruments": [f"NSE:{s}" for s in symbols]})
        return {o.symbol: o for o in (ohlc_from_kite(k, v, now) for k, v in raw.items())}

    def _instrument_token(self, symbol: str) -> int:
        if symbol not in self._tokens:
            # Public instrument master: no MCP call at all. Stocks moved to the trade-to-trade segment
            # are listed as SYMBOL-BE while index lists still say SYMBOL.
            master = nse_tokens(self._clock.today_ist())
            token = master.get(symbol) or master.get(f"{symbol}-BE")
            if token:
                self._tokens[symbol] = token
        if symbol not in self._tokens:
            # Exact id lookup returns one row. A tradingsymbol search also returns every F&O contract
            # on the stock (~120 rows, ~80 KB for a Nifty name), so it is only the fallback.
            for args in ({"query": f"NSE:{symbol}", "filter_on": "id"},
                         {"query": symbol, "filter_on": "tradingsymbol"}):
                found = self._paced_call("search_instruments", args)
                rows = found.get("data", found) if isinstance(found, dict) else found
                tokens = [i["instrument_token"] for i in rows or []
                          if i.get("tradingsymbol") == symbol and i.get("exchange") == "NSE"]
                if tokens:
                    self._tokens[symbol] = int(tokens[0])
                    break
            else:
                raise BrokerError(f"No NSE instrument token for {symbol}")
        return self._tokens[symbol]

    def _paced_call(self, tool: str, args: dict[str, Any]) -> Any:
        """Bulk tools (history, instrument search) share Kite's ~3 requests/second budget, across every
        thread (the scanner and the history download can run together). Space calls out and back off
        on errors that can clear up; a logged-out session or a pending login fails at once instead of
        waiting 7 s per stock."""
        delay = 1.0
        for attempt in range(HISTORICAL_RETRIES + 1):
            with self._pace_lock:
                wait = self._last_historical + HISTORICAL_MIN_INTERVAL - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last_historical = time.monotonic()
            try:
                return self._call(tool, args)
            except BrokerError as e:
                if attempt == HISTORICAL_RETRIES or not _TRANSIENT.search(str(e)):
                    raise
                time.sleep(delay)
                delay *= 2

    def get_historical(self, symbol: str, start: date, end: date) -> list[Candle]:
        token = self._instrument_token(symbol)
        candles: dict[date, Candle] = {}
        window_start = start
        while window_start <= end:
            window_end = min(end, window_start + timedelta(days=MAX_DAILY_WINDOW))
            raw = self._paced_call("get_historical_data", {
                "instrument_token": token, "from_date": f"{window_start} 00:00:00",
                "to_date": f"{window_end} 23:59:59", "interval": "day",
            })
            rows = raw.get("data", raw.get("candles", [])) if isinstance(raw, dict) else raw
            # Kite returns null (not []) for a window entirely before the listing date.
            for c in (candle_from_kite(r) for r in rows or []):
                candles[c.date] = c
            window_start = window_end + timedelta(days=1)
        return [candles[d] for d in sorted(candles)]

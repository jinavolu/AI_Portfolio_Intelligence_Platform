"""NSE instrument master (tradingsymbol → instrument_token) from Kite's public dump.

https://api.kite.trade/instruments/NSE needs no login and returns every NSE instrument in one
~0.7 MB CSV. Using it replaces one search_instruments call per symbol (500 for Nifty 500),
which the MCP server starts rejecting under load. Cached on disk for the day.
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import urllib.request
from datetime import date
from pathlib import Path

from app.config import BACKEND_DIR

URL = "https://api.kite.trade/instruments/NSE"
CACHE = BACKEND_DIR / "data" / "instruments_NSE.csv"
log = logging.getLogger("pi.instruments")
_lock = threading.Lock()
_memo: tuple[date, dict[str, int]] | None = None


def _parse(text: str) -> dict[str, int]:
    tokens = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("exchange") == "NSE" and row.get("segment") in ("NSE", "INDICES"):
            tokens[row["tradingsymbol"]] = int(row["instrument_token"])
    return tokens


def nse_equity_names(today: date, cache: Path = CACHE) -> dict[str, str]:
    """Listed NSE equities: tradingsymbol → Kite's (abbreviated) company name, e.g. IRCTC →
    INDIAN RAIL TOUR CORP. Main board, trade-to-trade and SME series; no bonds or indices."""
    nse_tokens(today, cache)  # makes sure today's dump is on disk
    if not cache.exists():
        return {}
    return {row["tradingsymbol"]: row["name"] for row in csv.DictReader(io.StringIO(cache.read_text(encoding="utf-8")))
            if row.get("segment") == "NSE" and row.get("instrument_type") == "EQ" and row.get("name")
            and _equity_series(row["tradingsymbol"])}


_EQUITY_SERIES = {"BE", "SM", "ST", "BZ"}  # plus plain symbols (EQ series)


def _equity_series(symbol: str) -> bool:
    """A two-letter suffix is a series (-SG bonds, -N0 NCDs, -BE trade-to-trade); a longer one is part
    of the name (BAJAJ-AUTO, NAM-INDIA)."""
    suffix = symbol.rsplit("-", 1)[-1] if "-" in symbol else None
    return suffix is None or len(suffix) != 2 or suffix in _EQUITY_SERIES


def nse_tokens(today: date, cache: Path = CACHE) -> dict[str, int]:
    """Today's map; downloads at most once a day. Returns {} if the dump is unreachable."""
    global _memo
    with _lock:
        if _memo and _memo[0] == today:
            return _memo[1]
        text = None
        if cache.exists() and date.fromtimestamp(cache.stat().st_mtime) == today:
            text = cache.read_text(encoding="utf-8")
        else:
            try:
                req = urllib.request.Request(URL, headers={"User-Agent": "portfolio-intelligence"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    text = r.read().decode("utf-8")
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(text, encoding="utf-8")
            except Exception as e:  # noqa: BLE001 - fall back to yesterday's file or per-symbol search
                log.warning("instrument dump download failed: %s", e)
                if cache.exists():
                    text = cache.read_text(encoding="utf-8")
        tokens = _parse(text) if text else {}
        if tokens:  # an empty map is not remembered: the next call tries the download again
            _memo = (today, tokens)
        return tokens

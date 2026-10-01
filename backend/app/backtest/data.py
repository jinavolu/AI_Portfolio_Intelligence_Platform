"""EOD history store: one Parquet file per symbol, queried with DuckDB (plan §12).

Candles come from the broker's historical API (Kite: split/bonus adjusted, not dividend
adjusted). Universe lists are NSE's published index constituent CSVs, which are CURRENT
constituents: backtests on them carry survivorship bias, and results say so.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import duckdb

from app.broker.base import BrokerAdapter, BrokerError
from app.broker.models import Candle
from app.config import BACKEND_DIR

DATA_DIR = BACKEND_DIR / "data" / "eod_fixture"
BENCHMARK = "NIFTY 50"


def configure(broker_name: str, base: Path | None = None) -> None:
    """One store per broker, so synthetic sample prices never mix with real Kite candles."""
    global DATA_DIR
    DATA_DIR = (base or BACKEND_DIR / "data") / f"eod_{broker_name}"

UNIVERSE_FILES = {"nifty50": "ind_nifty50list.csv", "nifty100": "ind_nifty100list.csv",
                  "nifty500": "ind_nifty500list.csv"}
# niftyindices.com answers quickly; NSE's archive often stalls non-browser clients, so it is the fallback.
UNIVERSE_SOURCES = ["https://www.niftyindices.com/IndexConstituent/", "https://archives.nseindia.com/content/indices/"]
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


def _file(symbol: str) -> Path:
    safe = symbol.replace(" ", "_").replace("/", "_").replace("&", "and")
    return DATA_DIR / f"{safe}.parquet"


def fetch_universe(name: str) -> list[dict]:
    """[{symbol, company, industry}] from the published index constituent CSV."""
    errors = []
    for base in UNIVERSE_SOURCES:
        try:
            req = urllib.request.Request(base + UNIVERSE_FILES[name], headers={"User-Agent": _UA, "Accept": "text/csv,*/*"})
            with urllib.request.urlopen(req, timeout=20) as r:
                text = r.read().decode("utf-8-sig")
            if "Symbol" in text.splitlines()[0]:
                break
            errors.append(f"{base}: not a constituent CSV")
        except Exception as e:  # noqa: BLE001 - try the next source
            errors.append(f"{base}: {e}")
    else:
        raise RuntimeError("Could not download the index constituent list: " + "; ".join(errors))
    rows = list(csv.DictReader(io.StringIO(text)))
    # NSE's lists sometimes carry placeholder rows (e.g. DUMMYHEG) that are not tradable stocks.
    return [{"symbol": row["Symbol"].strip(), "company": row.get("Company Name", "").strip(),
             "industry": row.get("Industry", "").strip()} for row in rows
            if row.get("Symbol") and not row["Symbol"].strip().upper().startswith("DUMMY")]


def save_candles(symbol: str, candles: list[Candle]) -> None:
    """Bulk write through a temporary CSV: DuckDB's row-by-row executemany takes ~25 s per symbol."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / f".{_file(symbol).stem}.csv"
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        w.writerows((k.date.isoformat(), k.open, k.high, k.low, k.close, k.volume) for k in candles)
    con = duckdb.connect()
    try:
        con.execute(
            f"copy (select * from read_csv('{tmp.as_posix()}', header=true, columns={{'date': 'DATE', "
            f"'open': 'DOUBLE', 'high': 'DOUBLE', 'low': 'DOUBLE', 'close': 'DOUBLE', 'volume': 'BIGINT'}}) "
            f"order by date) to '{_file(symbol).as_posix()}' (format parquet)")
    finally:
        con.close()
        tmp.unlink(missing_ok=True)


def load_candles(symbol: str) -> list[Candle]:
    f = _file(symbol)
    if not f.exists():
        return []
    rows = duckdb.connect().execute(
        f"select date, open, high, low, close, volume from read_parquet('{f.as_posix()}') order by date").fetchall()
    return [Candle(date=d, open=o, high=h, low=l, close=c, volume=int(v or 0)) for d, o, h, l, c, v in rows]


def stored_last_date(symbol: str) -> date | None:
    r = stored_range(symbol)
    return r[1] if r else None


def stored_range(symbol: str) -> tuple[date, date] | None:
    f = _file(symbol)
    if not f.exists():
        return None
    return duckdb.connect().execute(f"select min(date), max(date) from read_parquet('{f.as_posix()}')").fetchone()


class HistoryFetchJob:
    """Background download of `years` of daily candles for a universe plus the benchmark."""

    def __init__(self, broker: BrokerAdapter, today: date):
        self.broker, self.today = broker, today
        self.state: dict = {"running": False}
        self._lock = threading.Lock()

    def start(self, universe: str, years: int) -> dict:
        with self._lock:
            if self.state.get("running"):
                return self.state
            self.state = {"running": True, "universe": universe, "years": years, "done": 0, "total": None,
                          "skipped": 0, "failed": {}, "error": None, "symbols": []}
        threading.Thread(target=self._run, args=(universe, years), daemon=True, name="history-fetch").start()
        return self.state

    def _run(self, universe: str, years: int) -> None:
        try:
            if self.broker.name == "fixture":
                from app.broker.synthetic import SYNTHETIC_PARAMS

                members = [{"symbol": s, "company": s, "industry": "synthetic"} for s in SYNTHETIC_PARAMS
                           if s != BENCHMARK]
            else:
                members = fetch_universe(universe)
            symbols = [BENCHMARK] + [m["symbol"] for m in members]
            self.state.update(total=len(symbols), symbols=symbols, members=members)
            start = self.today - timedelta(days=int(years * 365.25))
            fresh_after = self.today - timedelta(days=4)
            previous = (read_manifest() or {}).get("requested_from", {})
            requested_from: dict[str, str] = dict(previous)
            for sym in symbols:
                have = stored_range(sym)
                # Up to date only if recent AND it reaches back as far as asked. A stock listed after
                # `start` legitimately begins later: that is fine if we already asked from `start` or earlier.
                covers = bool(have) and have[0] <= start + timedelta(days=10)
                asked_before = sym in previous and date.fromisoformat(previous[sym]) <= start
                if have and have[1] >= fresh_after and (covers or asked_before):
                    self.state["skipped"] += 1
                else:
                    try:
                        candles = self.broker.get_historical(sym, start, self.today)
                        if candles:
                            save_candles(sym, candles)
                            requested_from[sym] = start.isoformat()
                        else:
                            self.state["failed"][sym] = "no candles returned"
                    except BrokerError as e:
                        self.state["failed"][sym] = str(e)[:200]
                    except Exception as e:  # noqa: BLE001 - one bad payload must not stop the whole download
                        self.state["failed"][sym] = f"{type(e).__name__}: {str(e)[:180]}"
                self.state["done"] += 1
            write_manifest({"universe": universe, "years": years, "fetched_on": self.today.isoformat(),
                            "benchmark": BENCHMARK, "members": members,
                            "missing": sorted(self.state["failed"]), "requested_from": requested_from})
        except Exception as e:  # noqa: BLE001 - reported to the UI
            self.state["error"] = str(e)[:300]
        finally:
            self.state["running"] = False


def write_manifest(m: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "manifest.json").write_text(json.dumps(m, indent=1), encoding="utf-8")


def read_manifest() -> dict | None:
    f = DATA_DIR / "manifest.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None

"""Regenerate sample_data/tradebook.csv from the synthetic candles (prices = that day's close).

    uv run python scripts/make_sample_data.py
"""

from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.broker.synthetic import generate_candles  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "sample_data" / "tradebook.csv"

# (symbol, trade date, side, quantity)
TRADES = [
    ("INFY", date(2024, 3, 12), "buy", 40),
    ("INFY", date(2025, 11, 4), "buy", 20),
    ("HDFCBANK", date(2024, 1, 9), "buy", 60),
    ("HDFCBANK", date(2025, 2, 18), "sell", 15),
    ("TCS", date(2024, 7, 22), "buy", 25),
    ("ITC", date(2023, 8, 16), "buy", 300),
    ("RELIANCE", date(2024, 10, 3), "buy", 30),
    ("RELIANCE", date(2026, 4, 7), "buy", 15),
    ("TATAMOTORS", date(2025, 1, 14), "buy", 120),
    ("ASIANPAINT", date(2024, 5, 20), "buy", 12),
    ("SBIN", date(2025, 12, 1), "buy", 80),
]


def close_on(symbol: str, d: date) -> float:
    candles = generate_candles(symbol, d)
    return next(c.close for c in reversed(candles) if c.date <= d)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "isin", "trade_date", "exchange", "segment", "series", "trade_type", "auction",
                    "quantity", "price", "trade_id", "order_id", "order_execution_time"])
        for i, (sym, d, side, qty) in enumerate(TRADES, start=1):
            w.writerow([sym, "", d.isoformat(), "NSE", "EQ", "EQ", side, "false", qty,
                        f"{close_on(sym, d):.2f}", f"SAMPLE{i:04d}", f"ORD{i:04d}", f"{d.isoformat()}T10:15:00"])
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

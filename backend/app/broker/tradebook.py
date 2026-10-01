"""Import Zerodha Console tradebook CSV exports (Reports → Tradebook → Equity)."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

from app.broker.models import Trade

REQUIRED_COLUMNS = {"symbol", "trade_date", "exchange", "trade_type", "quantity", "price", "trade_id"}


class TradebookError(ValueError):
    pass


def _parse_date(value: str) -> date:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise TradebookError(f"Unrecognised trade_date: {value!r}")


def _number(row: dict, column: str, line: int) -> float:
    try:
        n = float(row[column].replace(",", ""))
    except ValueError:
        raise TradebookError(f"Line {line}: {column} must be a number, got {row[column]!r}") from None
    if n < 0 or (n == 0 and column == "quantity"):
        raise TradebookError(f"Line {line}: {column} can't be {row[column]!r}")
    return n


def parse_tradebook_csv(text: str) -> list[Trade]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if reader.fieldnames is None:
        raise TradebookError("Empty tradebook")
    columns = {c.strip().lower() for c in reader.fieldnames}
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise TradebookError(f"Tradebook is missing columns: {sorted(missing)}")

    trades: list[Trade] = []
    seen: set[str] = set()
    for i, raw in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        side = row["trade_type"].upper()
        if side not in ("BUY", "SELL"):
            raise TradebookError(f"Line {i}: trade_type must be buy/sell, got {row['trade_type']!r}")
        # Console splits one order into several fills sharing a trade_id only rarely; keep them distinct.
        trade_id = row["trade_id"]
        key = f"{trade_id}:{row.get('order_execution_time', '')}:{row['quantity']}"
        if key in seen:
            continue
        seen.add(key)
        trades.append(
            Trade(
                trade_id=trade_id,
                symbol=row["symbol"].upper(),
                exchange=row["exchange"].upper(),
                isin=row.get("isin") or None,
                trade_date=_parse_date(row["trade_date"]),
                side=side,
                quantity=_number(row, "quantity", i),
                price=_number(row, "price", i),
            )
        )
    return trades

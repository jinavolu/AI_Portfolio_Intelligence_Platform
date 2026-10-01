"""Normalized broker data. Every adapter maps its raw payloads into these shapes."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel


class Holding(BaseModel):
    symbol: str
    exchange: str
    isin: str | None = None
    quantity: int  # settled + T1
    average_price: float
    last_price: float
    close_price: float | None = None  # previous session close, for day change
    as_of: datetime


class Position(BaseModel):
    symbol: str
    exchange: str
    product: str
    quantity: int
    average_price: float
    last_price: float
    pnl: float
    as_of: datetime


class Margins(BaseModel):
    available_cash: float
    utilised: float
    net: float
    as_of: datetime


class Quote(BaseModel):
    symbol: str
    last_price: float
    volume: int | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None  # previous close
    as_of: datetime


class OHLC(BaseModel):
    symbol: str
    open: float
    high: float
    low: float
    close: float
    last_price: float
    as_of: datetime


class Candle(BaseModel):
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


class Trade(BaseModel):
    trade_id: str
    symbol: str
    exchange: str
    isin: str | None = None
    trade_date: date
    side: Literal["BUY", "SELL"]
    quantity: float
    price: float


class Dividend(BaseModel):
    symbol: str
    date: date
    amount: float  # total rupees received


class MarketEvent(BaseModel):
    symbol: str
    type: Literal["RESULTS", "RESULTS_ANNOUNCED", "CORPORATE_ACTION", "MAJOR_EVENT"]
    date: date
    description: str
    major: bool = False
    # "file" = events_file / sample data (gate 5 as in rules-1.0.0); "news" = official NSE disclosures,
    # used only by rule sets with news_events (rules-1.4.0, D11.1).
    source: Literal["file", "news"] = "file"
    source_id: str | None = None
    # Explicit review window (days before / after the event date); None = the legacy gate-5 rule.
    before_days: int | None = None
    after_days: int | None = None

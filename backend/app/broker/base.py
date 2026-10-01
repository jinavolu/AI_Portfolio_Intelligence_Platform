"""The only broker interface the application uses.

It is read-only by construction: there is no order method on the interface, and
tests/test_no_order_functions.py fails the build if any adapter grows one.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from app.broker.models import OHLC, Candle, Holding, Margins, Position, Quote

# Substrings that must never appear in a public attribute of any broker adapter.
FORBIDDEN_NAME_FRAGMENTS = ("order", "gtt", "convert_position", "exit_")


class BrokerAdapter(ABC):
    name: str = "abstract"

    @abstractmethod
    def get_holdings(self) -> list[Holding]: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_margins(self) -> Margins: ...

    @abstractmethod
    def get_quote(self, symbols: list[str]) -> dict[str, Quote]: ...

    @abstractmethod
    def get_ohlc(self, symbols: list[str]) -> dict[str, OHLC]: ...

    @abstractmethod
    def get_historical(self, symbol: str, start: date, end: date) -> list[Candle]: ...

    def account_id(self) -> str | None:
        """The logged-in broker user (e.g. a Kite user id). Theses, snapshots and alerts are kept per
        account; None means the adapter has no notion of a user, so everything shares one account."""
        return None


class BrokerError(RuntimeError):
    pass

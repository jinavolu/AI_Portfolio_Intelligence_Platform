"""Portfolio Engine: P&L, weights, sector allocation, lots, XIRR, dividends, tax-term flags.

All numbers here are deterministic. Nothing in this module talks to an LLM.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel

from app.broker.models import Dividend, Holding, Trade

ENGINE_VERSION = "portfolio-1.0.0"
UNCLASSIFIED = "UNCLASSIFIED"


# --------------------------------------------------------------------------- lots


@dataclass
class OpenLot:
    symbol: str
    buy_date: date | None  # None when quantity could not be matched to the tradebook
    quantity: float
    price: float


def fifo_open_lots(trades: list[Trade]) -> dict[str, list[OpenLot]]:
    """Replay the tradebook FIFO and return the lots still open per symbol."""
    lots: dict[str, list[OpenLot]] = defaultdict(list)
    for t in sorted(trades, key=lambda t: (t.trade_date, t.trade_id)):
        if t.side == "BUY":
            lots[t.symbol].append(OpenLot(t.symbol, t.trade_date, t.quantity, t.price))
            continue
        remaining = t.quantity
        queue = lots[t.symbol]
        while remaining > 1e-9 and queue:
            lot = queue[0]
            used = min(lot.quantity, remaining)
            lot.quantity -= used
            remaining -= used
            if lot.quantity <= 1e-9:
                queue.pop(0)
    return {s: q for s, q in lots.items() if q}


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    last_day = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30,
                31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last_day))


def tax_term(buy_date: date | None, on: date) -> str:
    """Listed equity: long-term when held for more than 12 months."""
    if buy_date is None:
        return "UNKNOWN"
    return "LONG_TERM" if on > add_months(buy_date, 12) else "SHORT_TERM"


# --------------------------------------------------------------------------- XIRR


def xirr(cashflows: list[tuple[date, float]]) -> float | None:
    """Annualised IRR for dated cashflows (outflows negative). None if undefined."""
    flows = [(d, a) for d, a in cashflows if abs(a) > 1e-9]
    if not flows or all(a > 0 for _, a in flows) or all(a < 0 for _, a in flows):
        return None
    t0 = min(d for d, _ in flows)
    years = [((d - t0).days / 365.0, a) for d, a in flows]

    def npv(r: float) -> float:
        return sum(a / (1 + r) ** t for t, a in years)

    def d_npv(r: float) -> float:
        return sum(-t * a / (1 + r) ** (t + 1) for t, a in years)

    r = 0.1
    for _ in range(100):
        f, df = npv(r), d_npv(r)
        if df == 0:
            break
        nxt = r - f / df
        if nxt <= -0.9999 or nxt != nxt:
            break
        if abs(nxt - r) < 1e-10:
            return nxt
        r = nxt

    lo, hi = -0.9999, 100.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


# --------------------------------------------------------------------------- outputs


class LotReport(BaseModel):
    buy_date: date | None
    quantity: float
    buy_price: float
    holding_days: int | None
    tax_term: str
    unrealised_gain: float
    days_to_long_term: int | None


class HoldingReport(BaseModel):
    symbol: str
    exchange: str
    sector: str
    quantity: int
    average_price: float
    last_price: float
    invested: float
    current_value: float
    pnl: float
    pnl_pct: float | None
    day_change: float | None
    day_change_pct: float | None
    weight: float
    dividends: float
    xirr: float | None
    lots: list[LotReport]
    lots_reconciled: bool
    short_term_gain: float
    long_term_gain: float
    unknown_term_gain: float


class PortfolioReport(BaseModel):
    engine_version: str = ENGINE_VERSION
    as_of: date
    total_invested: float
    total_current_value: float
    total_pnl: float
    total_pnl_pct: float | None
    day_change: float | None
    day_change_pct: float | None
    total_dividends: float
    xirr: float | None
    sector_allocation: dict[str, float]
    holdings: list[HoldingReport]


# --------------------------------------------------------------------------- engine


def _r(x: float, nd: int = 2) -> float:
    return round(x, nd)


def compute_portfolio(
    holdings: list[Holding],
    trades: list[Trade],
    sector_map: dict[str, str],
    today: date,
    dividends: list[Dividend] | None = None,
) -> PortfolioReport:
    dividends = dividends or []
    lots_by_symbol = fifo_open_lots(trades)
    trades_by_symbol: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        trades_by_symbol[t.symbol].append(t)
    div_by_symbol: dict[str, list[Dividend]] = defaultdict(list)
    for dv in dividends:
        div_by_symbol[dv.symbol].append(dv)

    total_value = sum(h.quantity * h.last_price for h in holdings)
    reports: list[HoldingReport] = []
    sector_values: dict[str, float] = defaultdict(float)
    portfolio_flows: list[tuple[date, float]] = []

    for h in holdings:
        invested = h.quantity * h.average_price
        value = h.quantity * h.last_price
        pnl = value - invested
        sector = sector_map.get(h.symbol, UNCLASSIFIED)
        sector_values[sector] += value

        day_change = day_change_pct = None
        if h.close_price:
            day_change = h.quantity * (h.last_price - h.close_price)
            day_change_pct = (h.last_price - h.close_price) / h.close_price

        lot_reports, reconciled = _lots(h, lots_by_symbol.get(h.symbol, []), today, gains := {
            "SHORT_TERM": 0.0, "LONG_TERM": 0.0, "UNKNOWN": 0.0})

        symbol_divs = sum(d.amount for d in div_by_symbol.get(h.symbol, []))
        flows = [(t.trade_date, -t.quantity * t.price if t.side == "BUY" else t.quantity * t.price)
                 for t in trades_by_symbol.get(h.symbol, [])]
        flows += [(d.date, d.amount) for d in div_by_symbol.get(h.symbol, [])]
        holding_xirr = None
        if reconciled and flows:
            holding_xirr = xirr(flows + [(today, value)])
            portfolio_flows += flows + [(today, value)]

        reports.append(
            HoldingReport(
                symbol=h.symbol,
                exchange=h.exchange,
                sector=sector,
                quantity=h.quantity,
                average_price=_r(h.average_price),
                last_price=_r(h.last_price),
                invested=_r(invested),
                current_value=_r(value),
                pnl=_r(pnl),
                pnl_pct=_r(pnl / invested, 6) if invested else None,
                day_change=_r(day_change) if day_change is not None else None,
                day_change_pct=_r(day_change_pct, 6) if day_change_pct is not None else None,
                weight=_r(value / total_value, 6) if total_value else 0.0,
                dividends=_r(symbol_divs),
                xirr=_r(holding_xirr, 6) if holding_xirr is not None else None,
                lots=lot_reports,
                lots_reconciled=reconciled,
                short_term_gain=_r(gains["SHORT_TERM"]),
                long_term_gain=_r(gains["LONG_TERM"]),
                unknown_term_gain=_r(gains["UNKNOWN"]),
            )
        )

    # Fully exited symbols still belong in the portfolio's money-weighted return.
    held = {h.symbol for h in holdings}
    for symbol, ts in trades_by_symbol.items():
        if symbol not in held:
            portfolio_flows += [(t.trade_date, -t.quantity * t.price if t.side == "BUY" else t.quantity * t.price)
                                for t in ts]
            portfolio_flows += [(d.date, d.amount) for d in div_by_symbol.get(symbol, [])]

    total_invested = sum(r.invested for r in reports)
    total_pnl = total_value - total_invested
    # Only holdings with a previous close count, on both sides of the ratio; none at all is missing, not 0.
    covered = [r for r in reports if r.day_change is not None]
    day_change = sum(r.day_change for r in covered) if covered else None
    prev_value = sum(r.current_value - r.day_change for r in covered)
    all_reconciled = all(r.lots_reconciled for r in reports)
    return PortfolioReport(
        as_of=today,
        total_invested=_r(total_invested),
        total_current_value=_r(total_value),
        total_pnl=_r(total_pnl),
        total_pnl_pct=_r(total_pnl / total_invested, 6) if total_invested else None,
        day_change=_r(day_change) if day_change is not None else None,
        day_change_pct=_r(day_change / prev_value, 6) if day_change is not None and prev_value else None,
        total_dividends=_r(sum(d.amount for d in dividends)),
        xirr=_r(x, 6) if all_reconciled and (x := xirr(portfolio_flows)) is not None else None,
        sector_allocation={s: _r(v / total_value, 6) for s, v in sorted(sector_values.items())}
        if total_value else {},
        holdings=sorted(reports, key=lambda r: -r.current_value),
    )


CORPORATE_ACTION_PRICE = 0.01  # rest-of-holding cost under 1% of the average: bonus or split shares


def _lots(h: Holding, open_lots: list[OpenLot], today: date, gains: dict) -> tuple[list[LotReport], bool]:
    """The holding's lots and whether they reconcile with the broker, so that the lots' gains always
    add up to the holding's P&L.

    - Tradebook open quantity == broker quantity: the tradebook's lots, reconciled.
    - Fewer open shares in the tradebook (shares from before it, a transfer in): its lots plus one
      lot of unknown term for the rest, priced so the total cost matches the broker's average.
    - That rest costing (almost) nothing means shares the tradebook never shows: a bonus or a split,
      which change the old lots' quantity and price. A sale missing from the tradebook (MORE open
      shares than held) means we can't tell which lots were sold. In both cases the lots are not
      trusted: the whole holding is one lot of unknown term at the broker's average.
    """
    open_qty = sum(lot.quantity for lot in open_lots)
    rest = h.quantity - open_qty
    if abs(rest) < 1e-9:
        return [_lot_report(lot.buy_date, lot.quantity, lot.price, h.last_price, today, gains)
                for lot in open_lots], True
    if rest > 0:
        rest_price = (h.quantity * h.average_price - sum(lot.quantity * lot.price for lot in open_lots)) / rest
        if rest_price >= CORPORATE_ACTION_PRICE * h.average_price:
            return [*(_lot_report(lot.buy_date, lot.quantity, lot.price, h.last_price, today, gains)
                      for lot in open_lots),
                    _lot_report(None, rest, rest_price, h.last_price, today, gains)], False
    return [_lot_report(None, h.quantity, h.average_price, h.last_price, today, gains)], False


def _lot_report(buy_date, qty, price, ltp, today, gains) -> LotReport:
    term = tax_term(buy_date, today)
    gain = qty * (ltp - price)
    gains[term] += gain
    days_to_lt = None
    if term == "SHORT_TERM":
        days_to_lt = (add_months(buy_date, 12) - today).days + 1
    return LotReport(
        buy_date=buy_date,
        quantity=qty,
        buy_price=_r(price),
        holding_days=(today - buy_date).days if buy_date else None,
        tax_term=term,
        unrealised_gain=_r(gain),
        days_to_long_term=days_to_lt,
    )


def estimated_tax_if_sold(report: HoldingReport, stcg_rate: float, ltcg_rate: float) -> dict:
    """Rough tax impact of selling the whole holding now. Ignores the annual LTCG exemption,
    set-off of losses and STT; it is a flag for the user, not a tax computation."""
    st = max(report.short_term_gain, 0.0) * stcg_rate
    lt = max(report.long_term_gain, 0.0) * ltcg_rate
    return {
        "short_term_gain": report.short_term_gain,
        "long_term_gain": report.long_term_gain,
        "unknown_term_gain": report.unknown_term_gain,
        "estimated_tax": _r(st + lt),
        "note": "Excludes LTCG annual exemption, loss set-off and transaction costs.",
    }

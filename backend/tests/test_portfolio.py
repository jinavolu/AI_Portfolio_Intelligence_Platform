from datetime import date, datetime, timezone

import pytest

from app.broker.models import Dividend, Holding, Trade
from app.engines.portfolio import add_months, compute_portfolio, fifo_open_lots, tax_term, xirr

NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


def trade(i, sym, d, side, q, p):
    return Trade(trade_id=str(i), symbol=sym, exchange="NSE", trade_date=d, side=side, quantity=q, price=p)


def test_xirr_simple_doubling_in_one_year():
    assert xirr([(date(2025, 1, 1), -100), (date(2026, 1, 1), 200)]) == pytest.approx(1.0, abs=1e-6)


def test_xirr_matches_spreadsheet():
    # =XIRR({-10000,-5000,2000,14000},{2024-01-01,2024-07-01,2025-01-01,2025-12-31}) ≈ 0.03851
    # (16,000 back on 15,000 invested over about two years; at 7.82% the NPV is -918, not 0).
    flows = [(date(2024, 1, 1), -10000), (date(2024, 7, 1), -5000), (date(2025, 1, 1), 2000),
             (date(2025, 12, 31), 14000)]
    r = xirr(flows)
    npv = sum(a / (1 + r) ** ((d - flows[0][0]).days / 365) for d, a in flows)
    assert abs(npv) < 1e-4
    assert r == pytest.approx(0.0385, abs=5e-4)


def test_xirr_undefined_without_sign_change():
    assert xirr([(date(2025, 1, 1), -100)]) is None


def test_fifo_consumes_oldest_first():
    lots = fifo_open_lots([
        trade(1, "A", date(2024, 1, 1), "BUY", 10, 100),
        trade(2, "A", date(2024, 6, 1), "BUY", 10, 120),
        trade(3, "A", date(2025, 1, 1), "SELL", 15, 150),
    ])["A"]
    assert [(l.buy_date, l.quantity, l.price) for l in lots] == [(date(2024, 6, 1), 5, 120)]


def test_tax_term_boundary_is_more_than_twelve_months():
    assert tax_term(date(2025, 9, 27), date(2026, 9, 27)) == "SHORT_TERM"
    assert tax_term(date(2025, 9, 27), date(2026, 9, 28)) == "LONG_TERM"
    assert tax_term(None, date(2026, 9, 27)) == "UNKNOWN"
    assert add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)


def test_totals_weights_lots_and_tax_flags():
    holdings = [
        Holding(symbol="A", exchange="NSE", quantity=10, average_price=100, last_price=150, close_price=140, as_of=NOW),
        Holding(symbol="B", exchange="NSE", quantity=5, average_price=200, last_price=100, close_price=100, as_of=NOW),
    ]
    trades = [trade(1, "A", date(2024, 1, 1), "BUY", 6, 100), trade(2, "A", date(2026, 6, 1), "BUY", 4, 100)]
    r = compute_portfolio(holdings, trades, {"A": "IT"}, date(2026, 9, 27),
                          [Dividend(symbol="A", date=date(2025, 6, 1), amount=50)])
    assert r.total_invested == 2000 and r.total_current_value == 2000 and r.total_pnl == 0
    assert r.day_change == 100
    a = next(h for h in r.holdings if h.symbol == "A")
    b = next(h for h in r.holdings if h.symbol == "B")
    assert a.weight == 0.75 and a.pnl == 500 and a.pnl_pct == 0.5
    assert a.lots_reconciled and a.long_term_gain == 300 and a.short_term_gain == 200
    assert a.lots[1].days_to_long_term == 248
    assert a.xirr is not None and a.dividends == 50
    # B has no tradebook rows: quantity is kept but its term is UNKNOWN, never guessed.
    assert not b.lots_reconciled and b.lots[0].tax_term == "UNKNOWN" and b.sector == "UNCLASSIFIED"
    assert r.xirr is None  # portfolio XIRR needs every holding reconciled
    assert r.sector_allocation == {"IT": 0.75, "UNCLASSIFIED": 0.25}

from datetime import date, datetime, timezone

import pytest

from app.broker.models import Dividend, Holding, Trade
from app.engines.portfolio import (add_months, compute_portfolio, estimated_tax_if_sold, fifo_open_lots,
                                   tax_term, xirr)

NOW = datetime(2026, 3, 18, tzinfo=timezone.utc)


def trade(sym, d, side, q, p, tid):
    return Trade(trade_id=tid, symbol=sym, exchange="NSE", trade_date=d, side=side, quantity=q, price=p)


def test_xirr_matches_spreadsheet_reference():
    # Microsoft's documented XIRR example: result 0.373362535
    flows = [(date(2008, 1, 1), -10000), (date(2008, 3, 1), 2750), (date(2008, 10, 30), 4250),
             (date(2009, 2, 15), 3250), (date(2009, 4, 1), 2750)]
    assert xirr(flows) == pytest.approx(0.373362535, abs=1e-6)


def test_xirr_undefined_without_sign_change():
    assert xirr([(date(2024, 1, 1), -100), (date(2025, 1, 1), -5)]) is None


def test_fifo_consumes_oldest_lot_first():
    trades = [trade("A", date(2024, 1, 1), "BUY", 10, 100, "1"), trade("A", date(2024, 6, 1), "BUY", 10, 200, "2"),
              trade("A", date(2025, 1, 1), "SELL", 15, 300, "3")]
    lots = fifo_open_lots(trades)["A"]
    assert [(l.buy_date, l.quantity, l.price) for l in lots] == [(date(2024, 6, 1), 5, 200)]


@pytest.mark.parametrize("buy,on,expected", [
    (date(2025, 3, 18), date(2026, 3, 18), "SHORT_TERM"),  # exactly 12 months: not more than
    (date(2025, 3, 17), date(2026, 3, 18), "LONG_TERM"),
    (date(2024, 2, 29), date(2025, 2, 28), "SHORT_TERM"),
    (date(2024, 2, 29), date(2025, 3, 1), "LONG_TERM"),
    (None, date(2026, 3, 18), "UNKNOWN"),
])
def test_tax_term_boundary(buy, on, expected):
    assert tax_term(buy, on) == expected


def test_add_months_clamps_day():
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)


def test_totals_weights_sectors_and_lots():
    holdings = [
        Holding(symbol="A", exchange="NSE", quantity=10, average_price=100, last_price=150, close_price=140, as_of=NOW),
        Holding(symbol="B", exchange="NSE", quantity=5, average_price=200, last_price=100, close_price=110, as_of=NOW),
    ]
    trades = [trade("A", date(2024, 1, 10), "BUY", 6, 100, "1"), trade("A", date(2026, 1, 5), "BUY", 4, 100, "2"),
              trade("B", date(2025, 6, 1), "BUY", 5, 200, "3")]
    r = compute_portfolio(holdings, trades, {"A": "IT"}, date(2026, 3, 18),
                          dividends=[Dividend(symbol="A", date=date(2025, 7, 1), amount=30)])
    assert r.total_invested == 2000
    assert r.total_current_value == 2000
    assert r.total_pnl == 0
    assert r.day_change == 10 * 10 - 5 * 10
    assert r.sector_allocation == {"IT": 0.75, "UNCLASSIFIED": 0.25}
    a = next(h for h in r.holdings if h.symbol == "A")
    assert a.weight == 0.75 and a.pnl == 500 and a.pnl_pct == 0.5 and a.dividends == 30
    assert a.lots_reconciled
    assert a.long_term_gain == 300 and a.short_term_gain == 200
    st_lot = next(l for l in a.lots if l.tax_term == "SHORT_TERM")
    assert st_lot.days_to_long_term == (date(2027, 1, 5) - date(2026, 3, 18)).days + 1
    assert a.xirr is not None and r.xirr is not None
    b = next(h for h in r.holdings if h.symbol == "B")
    tax = estimated_tax_if_sold(b, 0.2, 0.125)
    assert tax["estimated_tax"] == 0  # a loss attracts no tax


def test_unreconciled_quantity_is_flagged_not_invented():
    holdings = [Holding(symbol="A", exchange="NSE", quantity=10, average_price=100, last_price=110, as_of=NOW)]
    r = compute_portfolio(holdings, [trade("A", date(2024, 1, 1), "BUY", 4, 100, "1")], {}, date(2026, 3, 18))
    a = r.holdings[0]
    assert not a.lots_reconciled
    assert [l.tax_term for l in a.lots] == ["LONG_TERM", "UNKNOWN"]
    assert a.xirr is None and r.xirr is None
    assert a.day_change is None  # no previous close: missing, not zero


def test_fixture_holdings_reconcile_with_sample_tradebook(broker, repo, clock):
    r = compute_portfolio(broker.get_holdings(), repo.get_trades(), {}, clock.today_ist())
    assert all(h.lots_reconciled for h in r.holdings)
    hdfc = next(h for h in r.holdings if h.symbol == "HDFCBANK")
    assert hdfc.quantity == 45

"""Backtest of the deterministic rules (plan Phase 10).

Same code as production: indicator series use the Technical Engine's definitions and the
score is decision.component_scores + decision.weighted_score for the horizon under test.

Controls:
- Look-ahead: the signal uses data up to the close of day t; orders fill at the OPEN of t+1.
- Costs: Zerodha-style delivery charges plus slippage on every fill.
- Taxes: STCG/LTCG on realised gains, netted per financial year, paid from cash.
- Overfitting: parameters are fixed at the rule version under test; the last 40% of the
  window is out-of-sample and is what the criteria are judged on.
- Not controlled (reported as limitations): survivorship bias (current index constituents),
  dividends, point-in-time fundamentals (not used by these rules), results/events gate.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.backtest.data import BENCHMARK, load_candles, read_manifest
from app.backtest.series import indicator_rows
from app.decision import RuleSet, component_scores, weighted_score
from app.engines.portfolio import add_months
from app.thesis import Horizon

# 1.1.0: baselines valued after exit tax every day (no last-day cliff), losses carried forward,
# a sell on a non-trading day waits for the next session, turnover no longer counts the split day twice.
ENGINE_VERSION = "backtest-1.1.0"
REQUIRED = ("ema20", "ema50", "sma200", "rsi14", "macd_hist", "trend")


class CostModel(BaseModel):
    """Equity delivery on NSE via a zero-brokerage broker (approximate, 2026)."""
    stt: float = 0.001
    exchange_txn: float = 0.0000297
    sebi: float = 0.000001
    stamp_buy: float = 0.00015
    gst: float = 0.18
    slippage: float = 0.001

    def buy_cost(self, value: float) -> float:
        charges = value * (self.exchange_txn + self.sebi)
        return value * (self.stt + self.stamp_buy + self.slippage) + charges * (1 + self.gst)

    def sell_cost(self, value: float) -> float:
        charges = value * (self.exchange_txn + self.sebi)
        return value * (self.stt + self.slippage) + charges * (1 + self.gst)


class BacktestParams(BaseModel):
    horizon: Horizon = Horizon.LONG_TERM
    initial_capital: float = 1_000_000
    max_positions: int = 10
    oos_fraction: float = 0.4
    warmup_sessions: int = 220  # SMA200 + trend slope lookback
    stcg_rate: float = 0.20
    ltcg_rate: float = 0.125
    costs: CostModel = CostModel()


@dataclass
class Position:
    symbol: str
    qty: float
    entry_date: date
    entry_price: float
    entry_cost: float


@dataclass
class ClosedTrade:
    symbol: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    qty: float
    pnl: float  # after costs, before tax


@dataclass
class TaxBook:
    st: float = 0.0
    lt: float = 0.0
    paid: float = 0.0
    by_fy: dict = field(default_factory=dict)
    # Net losses carried into later years (positive amounts): short-term ones offset any later gain,
    # long-term ones only long-term gains.
    st_loss_cf: float = 0.0
    lt_loss_cf: float = 0.0


def _fy(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def _settle_tax(book: TaxBook, fy: int, p: BacktestParams) -> float:
    # Short-term losses (this year's, then brought forward) offset short-term then long-term gains;
    # long-term losses only long-term gains. What is left over is carried forward by kind.
    st_loss = max(-book.st, 0.0) + book.st_loss_cf
    lt_loss = max(-book.lt, 0.0) + book.lt_loss_cf
    st, lt = max(book.st, 0.0), max(book.lt, 0.0)
    used = min(lt_loss, lt)
    lt, lt_loss = lt - used, lt_loss - used
    for gain in ("st", "lt"):
        g = st if gain == "st" else lt
        used = min(st_loss, g)
        st_loss -= used
        if gain == "st":
            st -= used
        else:
            lt -= used
    book.st_loss_cf, book.lt_loss_cf = st_loss, lt_loss
    tax = st * p.stcg_rate + lt * p.ltcg_rate
    book.by_fy[fy] = round(tax, 2)
    book.paid += tax
    book.st = book.lt = 0.0
    return tax


# --------------------------------------------------------------------------- metrics


def _cagr(v0: float, v1: float, days: int) -> float | None:
    if v0 <= 0 or days <= 0:
        return None
    return (v1 / v0) ** (365.25 / days) - 1


def _max_dd(values: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst


def _ratios(values: list[float]) -> tuple[float | None, float | None]:
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]
    if len(rets) < 2:
        return None, None
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    down = [min(r, 0.0) for r in rets]
    dsd = math.sqrt(sum(r * r for r in down) / len(down))
    sharpe = mean / sd * math.sqrt(252) if sd else None
    sortino = mean / dsd * math.sqrt(252) if dsd else None
    return sharpe, sortino


def period_metrics(dates: list[date], equity: list[float], bench: list[float], trades: list[ClosedTrade],
                   traded_value: float, equal_weight: list[float] | None = None) -> dict:
    days = (dates[-1] - dates[0]).days
    sharpe, sortino = _ratios(equity)
    b_sharpe, b_sortino = _ratios(bench)
    wins = [t for t in trades if t.pnl > 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in trades if t.pnl < 0)
    years = days / 365.25 if days else 0
    avg_equity = sum(equity) / len(equity)
    cagr, b_cagr = _cagr(equity[0], equity[-1], days), _cagr(bench[0], bench[-1], days)
    r = lambda x, n=4: None if x is None else round(x, n)  # noqa: E731
    return {
        "start": dates[0].isoformat(), "end": dates[-1].isoformat(), "years": round(years, 2),
        "strategy": {"cagr": r(cagr), "max_drawdown": r(_max_dd(equity)), "sharpe": r(sharpe, 2),
                     "sortino": r(sortino, 2), "final_value": round(equity[-1], 2)},
        "benchmark": {"cagr": r(b_cagr), "max_drawdown": r(_max_dd(bench)), "sharpe": r(b_sharpe, 2),
                      "sortino": r(b_sortino, 2), "final_value": round(bench[-1], 2)},
        "excess_cagr": r(cagr - b_cagr) if cagr is not None and b_cagr is not None else None,
        # Informational second baseline (not a criterion): the same universe bought equally and held.
        "equal_weight_hold": None if not equal_weight else {
            "cagr": r(_cagr(equal_weight[0], equal_weight[-1], days)), "max_drawdown": r(_max_dd(equal_weight)),
            "final_value": round(equal_weight[-1], 2)},
        "trades": len(trades),
        "win_rate": r(len(wins) / len(trades)) if trades else None,
        "profit_factor": r(gross_win / gross_loss, 2) if gross_loss else None,
        "turnover_per_year": r(traded_value / avg_equity / years, 2) if years and avg_equity else None,
    }


def regime_table(dates: list[date], equity: list[float], bench: list[float]) -> list[dict]:
    """Full calendar years only, classified by the benchmark's return."""
    by_year: dict[int, list[int]] = {}
    for i, d in enumerate(dates):
        by_year.setdefault(d.year, []).append(i)
    first, last = dates[0], dates[-1]
    out = []
    for y, idx in sorted(by_year.items()):
        if (y == first.year and first > date(y, 1, 7)) or (y == last.year and last < date(y, 12, 24)):
            continue  # partial year
        i0 = idx[0] - 1 if idx[0] > 0 else idx[0]  # measure from the previous close
        i1 = idx[-1]
        b = bench[i1] / bench[i0] - 1
        s = equity[i1] / equity[i0] - 1
        regime = "bull" if b > 0.15 else "bear" if b < 0 else "sideways"
        out.append({"year": y, "regime": regime, "strategy": round(s, 4), "benchmark": round(b, 4)})
    return out


# --------------------------------------------------------------------------- criteria


CRITERIA_FILE = Path(__file__).resolve().parent.parent.parent / "backtest_criteria.json"


def load_criteria() -> tuple[dict, str]:
    raw = CRITERIA_FILE.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def evaluate_criteria(criteria: dict, oos: dict, regimes: list[dict]) -> list[dict]:
    c = criteria["criteria"]
    checks = []
    ex = oos["excess_cagr"]
    checks.append({"criterion": "Beats benchmark CAGR on out-of-sample by at least "
                                f"{c['min_excess_cagr_oos']:.0%}",
                   "value": ex, "threshold": c["min_excess_cagr_oos"], "unit": "fraction",
                   "passed": ex is not None and ex >= c["min_excess_cagr_oos"]})
    s_dd, b_dd = oos["strategy"]["max_drawdown"], oos["benchmark"]["max_drawdown"]
    checks.append({"criterion": "Out-of-sample max drawdown no worse than benchmark's",
                   "value": s_dd, "threshold": b_dd, "unit": "fraction", "passed": s_dd >= b_dd})
    checks.append({"criterion": f"At least {c['min_trades_oos']} closed trades out-of-sample",
                   "value": oos["trades"], "threshold": c["min_trades_oos"], "unit": "count",
                   "passed": oos["trades"] >= c["min_trades_oos"]})
    per: dict[str, list[dict]] = {}
    for row in regimes:
        per.setdefault(row["regime"], []).append(row)
    held = {}
    for regime, rows in per.items():
        n = len(rows)
        s = math.prod(1 + r["strategy"] for r in rows) ** (1 / n) - 1
        b = math.prod(1 + r["benchmark"] for r in rows) ** (1 / n) - 1
        held[regime] = {"years": [r["year"] for r in rows], "strategy": round(s, 4), "benchmark": round(b, 4),
                        "held": s >= b}
    ok = len(held) >= c["min_regimes_held"] and all(h["held"] for h in held.values())
    checks.append({"criterion": f"Holds (strategy ≥ benchmark) in at least {c['min_regimes_held']} "
                                "distinct regime types (bull / sideways / bear years)",
                   "value": {k: v["held"] for k, v in held.items()}, "threshold": c["min_regimes_held"],
                   "unit": "regimes",
                   "passed": ok, "detail": held})
    return checks


HOLDOUT_FILE = CRITERIA_FILE.with_name("holdout_criteria.json")


def load_holdout() -> tuple[dict, str]:
    raw = HOLDOUT_FILE.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def evaluate_holdout(holdout: dict, period: dict) -> list[dict]:
    c = holdout["criteria"]
    ex = period["excess_cagr"]
    s_dd, b_dd = period["strategy"]["max_drawdown"], period["benchmark"]["max_drawdown"]
    return [
        {"criterion": f"Beats benchmark CAGR on the sealed holdout by at least {c['min_excess_cagr']:.0%}",
         "value": ex, "threshold": c["min_excess_cagr"], "unit": "fraction",
         "passed": ex is not None and ex >= c["min_excess_cagr"]},
        {"criterion": "Holdout max drawdown no worse than benchmark's",
         "value": s_dd, "threshold": b_dd, "unit": "fraction", "passed": s_dd >= b_dd},
        {"criterion": f"At least {c['min_trades']} closed trades in the holdout",
         "value": period["trades"], "threshold": c["min_trades"], "unit": "count",
         "passed": period["trades"] >= c["min_trades"]},
    ]


# --------------------------------------------------------------------------- simulation


@dataclass
class SimResult:
    dates: list[date]
    equity: list[float]
    bench: list[float]
    trades: list[ClosedTrade]
    fills: list[tuple[date, float]]
    taxes_paid: float
    final_cash: float


def _simulate(series: dict, bench_candles, dates: list[date], p: BacktestParams, rules: RuleSet) -> SimResult:
    bench_close = {c.date: c.close for c in bench_candles}
    bench_open = {c.date: c.open for c in bench_candles}
    # Benchmark buy-and-hold: buy at the first day's open with the same entry cost.
    b_units = (p.initial_capital - p.costs.buy_cost(p.initial_capital)) / bench_open[dates[0]]

    cash: float = p.initial_capital
    positions: dict[str, Position] = {}
    trades: list[ClosedTrade] = []
    fills: list[tuple[date, float]] = []  # (date, traded value) for turnover
    tax = TaxBook()
    equity: list[float] = []
    bench: list[float] = []
    pending_buys: list[str] = []
    pending_sells: list[str] = []
    last_close: dict[str, float] = {}
    current_fy = _fy(dates[0])

    def score_of(row) -> float | None:
        if any(getattr(row, f) is None for f in REQUIRED):
            return None  # gate 2: required data missing
        return weighted_score(component_scores(row, p.horizon, rules), p.horizon, rules)

    def close(pos: Position, value: float, d: date, price: float) -> float:
        """Sell a position: record the trade and the taxable gain; returns cash received."""
        cost = p.costs.sell_cost(value)
        gain = value - cost - pos.qty * pos.entry_price - pos.entry_cost  # after costs, before tax
        if d > add_months(pos.entry_date, 12):
            tax.lt += gain
        else:
            tax.st += gain
        trades.append(ClosedTrade(pos.symbol, pos.entry_date, d, pos.entry_price, price, pos.qty, gain))
        return value - cost

    for d in dates:
        if _fy(d) != current_fy:  # financial-year end: settle tax on the year's realised gains
            cash -= _settle_tax(tax, current_fy, p)
            current_fy = _fy(d)

        # 1. Fill yesterday's orders at today's open. A stock that doesn't trade today (suspended,
        # circuit) keeps its sell order for the next session it does.
        waiting = []
        for sym in pending_sells:
            row = series[sym].get(d)
            if sym not in positions:
                continue
            if row is None:
                waiting.append(sym)
                continue
            pos = positions.pop(sym)
            value = pos.qty * row.open
            cash += close(pos, value, d, row.open)
            fills.append((d, value))
        pending_sells = waiting

        equity_now = cash + sum(pos.qty * last_close.get(s, pos.entry_price) for s, pos in positions.items())
        slot_value = equity_now / p.max_positions
        for sym in pending_buys:
            if len(positions) >= p.max_positions or sym in positions:
                continue
            row = series[sym].get(d)
            if row is None:
                continue
            budget = min(slot_value, cash)
            if budget <= 0:
                break
            gross = budget / (1 + p.costs.buy_cost(1.0))
            cost = p.costs.buy_cost(gross)
            cash -= gross + cost
            fills.append((d, gross))
            positions[sym] = Position(sym, gross / row.open, d, row.open, cost)
        pending_buys = []

        # 2. Mark to market at today's close.
        for sym in positions:
            row = series[sym].get(d)
            if row is not None:
                last_close[sym] = row.close
        equity.append(cash + sum(pos.qty * last_close.get(s, pos.entry_price) for s, pos in positions.items()))
        bench.append(_liquidation_value(b_units * bench_close[d], d, dates[0], p))

        # 3. Signals at today's close → orders for tomorrow's open.
        candidates = []
        for sym, rows in series.items():
            row = rows.get(d)
            if row is None:
                continue
            s = score_of(row)
            if s is None:
                continue
            if sym in positions and s <= rules.sell_threshold:
                pending_sells.append(sym)
            elif sym not in positions and s >= rules.buy_threshold:
                candidates.append((s, sym))
        free = p.max_positions - len(positions) + len(pending_sells)
        pending_buys = [sym for _, sym in sorted(candidates, reverse=True)[:max(free, 0)]]

    # Liquidate at the last close (with costs and tax) so results are after tax.
    end = dates[-1]
    for sym, pos in list(positions.items()):
        value = pos.qty * last_close[sym]
        cash += close(pos, value, end, last_close[sym])
        fills.append((end, value))
    cash -= _settle_tax(tax, current_fy, p)
    equity[-1] = cash
    return SimResult(dates, equity, bench, trades, fills, tax.paid, cash)


def _liquidation_value(gross: float, d: date, start: date, p: BacktestParams) -> float:
    """A buy-and-hold baseline's value on day d if sold that day: after selling costs and the tax on
    the gain so far. Valuing every day this way (not only the last) means a period's return carries
    the tax on that period's gains only, and the curve has no last-day tax cliff to count as a
    drawdown. (The strategy pays its tax as each financial year closes.)"""
    after_costs = gross - p.costs.sell_cost(gross)
    rate = p.ltcg_rate if d > add_months(start, 12) else p.stcg_rate
    return after_costs - max(after_costs - p.initial_capital, 0.0) * rate


def _equal_weight_hold(series: dict, dates: list[date], p: BacktestParams) -> list[float]:
    """Buy every universe stock trading on day one in equal amounts at the open and hold to the end
    (same costs; each day valued as if sold that day, like the benchmark). Separates stock selection
    from timing."""
    first = [(s, rows[dates[0]]) for s, rows in series.items() if dates[0] in rows]
    if not first:
        return []
    slot = (p.initial_capital / len(first)) / (1 + p.costs.buy_cost(1.0))
    units = {s: slot / row.open for s, row in first}
    last = {s: row.open for s, row in first}
    values = []
    for d in dates:
        for s in units:
            row = series[s].get(d)
            if row is not None:
                last[s] = row.close
        values.append(_liquidation_value(sum(units[s] * last[s] for s in units), d, dates[0], p))
    return values


def run_backtest(params: BacktestParams, rules: RuleSet | None = None,
                 window: Literal["development", "holdout"] = "development") -> dict:
    """development: every date before the sealed holdout, judged on its out-of-sample tail.
    holdout: the sealed period only, judged on the holdout criteria."""
    rules = rules or RuleSet()
    if rules.buy_min_volume_5d is not None or rules.volume_events or rules.news_share or rules.news_events:
        # D9.4 / D10.6: the simulator doesn't apply the volume rules yet; a run would test different
        # rules from the ones its label claims.
        raise ValueError(f"{rules.version}'s volume and news rules (D9-D11) are not implemented in the backtest "
                         "engine, so it can't be backtested (and news has no history to test on).")
    p = params
    manifest = read_manifest()
    if not manifest:
        raise ValueError("No history downloaded yet")
    criteria, criteria_hash = load_criteria()
    holdout, holdout_hash = load_holdout()
    sealed = date.fromisoformat(holdout["sealed_from"])

    bench_candles = load_candles(BENCHMARK)
    calendar = [c.date for c in bench_candles]
    if len(calendar) < p.warmup_sessions + 250:
        raise ValueError("Not enough benchmark history")
    series: dict[str, dict[date, object]] = {}
    for m in manifest["members"]:
        rows = indicator_rows(load_candles(m["symbol"]))
        if rows:
            series[m["symbol"]] = {r.date: r for r in rows}

    if window == "development":
        dates = [d for d in calendar[p.warmup_sessions:] if d < sealed]
    else:
        # Indicators use earlier history (data up to each day only); trading starts on the sealed date.
        dates = [d for i, d in enumerate(calendar) if d >= sealed and i >= p.warmup_sessions]
    if len(dates) < 250:
        raise ValueError(f"Not enough history in the {window} window ({len(dates)} sessions)")

    sim = _simulate(series, bench_candles, dates, p, rules)
    ew = _equal_weight_hold(series, dates, p)
    end = dates[-1]

    def traded(a: date, b: date) -> float:
        return sum(v for d, v in sim.fills if a <= d <= b)

    if window == "development":
        split = int(len(dates) * (1 - p.oos_fraction))
        s0 = dates[split]
        in_trades = [t for t in sim.trades if t.exit_date < s0]
        oos_trades = [t for t in sim.trades if t.exit_date >= s0]
        periods = {
            "full": period_metrics(dates, sim.equity, sim.bench, sim.trades, traded(dates[0], end), ew),
            "in_sample": period_metrics(dates[:split + 1], sim.equity[:split + 1], sim.bench[:split + 1],
                                        in_trades, traded(dates[0], dates[split - 1]), ew[:split + 1]),
            "out_of_sample": period_metrics(dates[split:], sim.equity[split:], sim.bench[split:], oos_trades,
                                            traded(s0, end), ew[split:]),
        }
        regimes = regime_table(dates, sim.equity, sim.bench)
        checks = evaluate_criteria(criteria, periods["out_of_sample"], regimes)
        judged_from, used_criteria, used_hash = s0, criteria, criteria_hash
    else:
        periods = {"holdout": period_metrics(dates, sim.equity, sim.bench, sim.trades, traded(dates[0], end), ew)}
        regimes = []
        checks = evaluate_holdout(holdout, periods["holdout"])
        judged_from, used_criteria, used_hash = dates[0], holdout, holdout_hash

    step = max(1, len(dates) // 400)
    curve = [{"date": dates[i].isoformat(), "strategy": round(sim.equity[i], 2), "benchmark": round(sim.bench[i], 2),
              "equal_weight": round(ew[i], 2) if ew else None} for i in range(0, len(dates), step)]
    if curve[-1]["date"] != end.isoformat():
        curve.append({"date": end.isoformat(), "strategy": round(sim.equity[-1], 2),
                      "benchmark": round(sim.bench[-1], 2), "equal_weight": round(ew[-1], 2) if ew else None})
    pnl = sum(t.pnl for t in sim.trades)
    return {
        "engine_version": ENGINE_VERSION,
        "window": window,
        "sealed_from": sealed.isoformat(),
        "rules_version": rules.version,
        "rules_hash": hashlib.sha256(json.dumps(rules.model_dump(mode="json"), sort_keys=True).encode()).hexdigest(),
        "horizon": p.horizon.value,
        "params": p.model_dump(mode="json"),
        "criteria": used_criteria["criteria"],
        "criteria_hash": used_hash,
        "development_criteria_hash": criteria_hash,
        "holdout_criteria_hash": holdout_hash,
        "criteria_locked_at": used_criteria["locked_at"],
        "universe": {"name": manifest["universe"], "size": len(series), "missing": manifest.get("missing", [])},
        "periods": periods,
        "oos_start": judged_from.isoformat(),
        "regimes": regimes,
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
        "taxes_paid": round(sim.taxes_paid, 2),
        # Everything was liquidated at the end, so cash must reconcile exactly with the trade ledger.
        "accounting": {"initial_capital": p.initial_capital, "sum_trade_pnl_after_costs": round(pnl, 2),
                       "taxes_paid": round(sim.taxes_paid, 2), "final_cash": round(sim.final_cash, 2),
                       "difference": round(p.initial_capital + pnl - sim.taxes_paid - sim.final_cash, 6)},
        "equity_curve": curve,
        "recent_trades": [t.__dict__ | {"entry_date": t.entry_date.isoformat(), "exit_date": t.exit_date.isoformat()}
                          for t in sim.trades[-50:]],
        "limitations": [
            f"Survivorship bias: universe is today's {manifest['universe']} constituents; stocks that were "
            "dropped or delisted during the period are missing, which flatters results.",
            "Corporate actions: Kite candles are adjusted for splits and bonuses, not dividends; dividends are "
            "excluded for the strategy and both baselines.",
            "The results/events gate (gate 5) and thesis invalidation (gate 4) cannot be replayed historically.",
            "Fractional shares, fills at the next open plus fixed slippage; no liquidity or circuit-limit checks.",
            "Tax uses today's rates for every year and ignores the annual LTCG exemption (conservative). Losses "
            "are carried forward without the 8-year limit. Nifty 50 and equal-weight values are after the cost "
            "and tax of selling on that day.",
            "Idle cash earns nothing.",
            "A backtest shows how the rules behaved on past data; passing means meeting the predefined "
            "criteria, not a guarantee of future performance.",
        ],
    }

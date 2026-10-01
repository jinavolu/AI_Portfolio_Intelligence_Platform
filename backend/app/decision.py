"""Decision Engine: hard gates in order, then a horizon-weighted score.

Gates (first one that fires sets the state and ends evaluation, except gate 6):
  1 stale data            -> NO_ACTION
  2 missing data          -> NO_ACTION
  3 thesis/horizon missing-> NO_ACTION
  4 thesis invalidated    -> REVIEW
  5 event window          -> REVIEW
  6 concentration         -> blocks adding only (DON'T_ADD instead of BUY)

Score bands (after all gates pass):
  score >= buy_threshold               -> BUY
  hold_threshold <= score < buy        -> HOLD
  sell_threshold < score < hold        -> NO_ACTION (neutral band)
  score <= sell_threshold              -> SELL
BUY/SELL are *_SIGNAL until the rule version is marked validated by backtesting,
and *_CANDIDATE after.
"""

from __future__ import annotations

import json
import operator
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.broker.models import MarketEvent
from app.engines.portfolio import HoldingReport
from app.engines.risk import HoldingRisk
from app.engines.technical import TechnicalReport
from app.clock import IST
from app.fundamentals import freshness_problem

MARKET_CLOSE_IST = time(15, 30)
from app.thesis import GATE4_CATEGORIES, METRIC_SOURCES, Horizon, InvalidationCondition, Thesis

# Evidence families with no engine yet (plan Phases 9 and 11). Reported, never scored as zero.
ENGINES_NOT_BUILT = ("fundamental", "news")

State = Literal["NO_ACTION", "REVIEW", "DONT_ADD", "HOLD", "BUY_SIGNAL", "SELL_SIGNAL",
                "BUY_CANDIDATE", "SELL_CANDIDATE"]


class RuleSet(BaseModel):
    version: str = "rules-1.0.0"
    max_holdings_age_hours: float = 18
    max_candle_age_days: int = 4
    results_window_days: int = 7
    required_technicals: list[str] = ["ema20", "ema50", "sma200", "rsi14", "macd_hist", "trend"]
    weights: dict[Horizon, dict[str, float]] = {
        Horizon.SHORT_TERM: {"trend": 0.25, "long_trend": 0.10, "momentum": 0.40, "breakout": 0.25},
        Horizon.MEDIUM_TERM: {"trend": 0.35, "long_trend": 0.25, "momentum": 0.25, "breakout": 0.15},
        Horizon.LONG_TERM: {"trend": 0.20, "long_trend": 0.60, "momentum": 0.10, "breakout": 0.10},
    }
    buy_threshold: float = 0.6
    hold_threshold: float = 0.0
    sell_threshold: float = -0.6
    overbought_rsi: float = 70.0
    # rules-1.2.0 (D9): BUY needs the 5-completed-session volume to be at least this multiple of the
    # 20-session average; otherwise HOLD. None = no volume filter (rules-1.0.0).
    buy_min_volume_5d: float | None = None
    # rules-1.3.0 (D10): a heavy-volume breakout → BUY signal; a breakdown → SELL signal (short and
    # medium term) or REVIEW (long term), judged on the last completed session.
    volume_events: bool = False
    # rules-1.4.0 (D11): official NSE events reach gate 5, and news takes this share of the score.
    news_events: bool = False
    news_share: dict[Horizon, float] | None = None


LIVE_RULES_VERSION = "rules-1.3.0"
SHADOW_RULES_VERSION = "rules-1.4.0"
NEWS_SHARE = {Horizon.SHORT_TERM: 0.15, Horizon.MEDIUM_TERM: 0.15, Horizon.LONG_TERM: 0.10}  # D11.3


def live_rule_set(**limits) -> RuleSet:
    """The rule set the app runs: rules-1.0.0 plus volume confirmation for BUY (D9) and heavy-volume
    breakout/breakdown signals (D10)."""
    return RuleSet(version=LIVE_RULES_VERSION, buy_min_volume_5d=1.0, volume_events=True, **limits)


def shadow_rule_set(**limits) -> RuleSet:
    """rules-1.4.0 (D11), computed alongside the live rules in shadow mode: 1.3.0 plus official NSE
    events in gate 5 and a news share of the score. The 1.3.0 weights are kept; weighted_score blends
    (1 − share) × the 1.3.0 score + share × news, so a missing news component is exactly 1.3.0."""
    return RuleSet(version=SHADOW_RULES_VERSION, buy_min_volume_5d=1.0, volume_events=True, news_events=True,
                   news_share=dict(NEWS_SHARE), **limits)


class GateResult(BaseModel):
    gate: int
    name: str
    status: Literal["PASS", "FIRED", "SKIPPED"]
    detail: str = ""


class ConditionResult(BaseModel):
    """One ACTIVE condition evaluated against one snapshot (decisions.md D7.4)."""
    id: str
    category: str
    description: str
    metric: str
    op: str
    value: float | str
    result: Literal["MET", "NOT_MET", "CANNOT_CHECK"]
    actual: float | str | None = None
    reason: str = ""  # why it could not be checked
    source: str
    source_as_of: str | None = None
    # True for a portfolio-wide default warning: "default warning, not your thesis".
    default: bool = False
    # Business metrics: the compared periods and both raw quarters, so the percentage is never the
    # whole story (D7.7), plus when the provider was read.
    period: str | None = None
    inputs: dict | None = None
    fetched_at: str | None = None


class Decision(BaseModel):
    state: State
    reason: str
    dont_add: bool = False
    score: float | None = None
    rule_version: str
    rule_set_validated: bool
    tax_impact: dict | None = None
    # "draft" = horizon/reason assigned in bulk or by profile, not confirmed by the owner.
    thesis_status: Literal["missing", "draft", "confirmed"] | None = None
    # Evidence families the score could not use (engines not built yet). Never read as "deteriorating".
    evidence_unavailable: list[str] = []
    # Plain-language version of `reason` for the owner. Written by summarize(); `reason` stays the
    # technical, auditable statement.
    summary: str = ""
    # The summary explained: evidence (numbers), basis, action (by horizon), note. Wording only.
    guidance: dict[str, str] = {}
    # Volume confirmation (D9): ratio of 5-completed-session to 20-session volume, the threshold, and
    # whether today's open session was left out. `passed` is set only when the buy band was reached.
    volume_check: dict | None = None
    # The last completed session's heavy-volume breakout/breakdown, if any (D10), and what set the
    # state: "score" (bands) or "volume_event" (rules-1.3.0).
    volume_event: dict | None = None
    signal_source: Literal["score", "volume_event"] = "score"
    # rules-1.4.0 (D11.4): score with/without news, the news contribution and its impact class.
    news_diagnostics: dict | None = None


@dataclass
class DecisionInputs:
    symbol: str
    now: datetime
    today: date
    holding: HoldingReport | None
    holding_as_of: datetime | None
    technical: TechnicalReport | None
    thesis: Thesis | None
    risk: HoldingRisk | None
    events: list[MarketEvent] = field(default_factory=list)
    tax_impact: dict | None = None
    # Enabled portfolio-wide default warnings for this holding (TECHNICAL only).
    warnings: list[InvalidationCondition] = field(default_factory=list)
    # fundamentals.business_view() of the latest stored fetch, or a NOT_FOUND/ERROR stub; None = never fetched.
    fundamental: dict | None = None
    # news_service view (status, deterministic score, clusters); None = never fetched (D11).
    news: dict | None = None
    # Scanner (D14): a stock the owner doesn't hold. Gate 2 then doesn't require a holding; every
    # other gate and the scoring are unchanged.
    scan: bool = False


@dataclass
class DecisionOutput:
    gates: list[GateResult]
    component_scores: dict[str, float | None]
    decision: Decision
    conditions: list[ConditionResult] = field(default_factory=list)


def load_validated_rule_versions(path: Path) -> set[str]:
    """Written by the backtesting phase. Missing file = nothing validated."""
    if not path.exists():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")).get("validated", []))


# --------------------------------------------------------------------------- metrics


def condition_metrics(inp: DecisionInputs) -> dict[str, float | str | None]:
    h, t, r = inp.holding, inp.technical, inp.risk
    m: dict[str, float | str | None] = {}
    if h:
        m.update(last_price=h.last_price, pnl_pct=h.pnl_pct, weight=h.weight)
    if r:
        m["sector_weight"] = r.sector_weight
    if t:
        m.update(rsi14=t.rsi14, ema20=t.ema20, ema50=t.ema50, sma200=t.sma200, macd_hist=t.macd_hist,
                 volume_ratio=t.volume_ratio, pct_from_52w_high=t.pct_from_52w_high, trend=t.trend,
                 long_term_trend=t.long_term_trend,
                 close_vs_sma200=round(t.close / t.sma200 - 1, 4) if t.sma200 else None)
    f = inp.fundamental
    if f and f.get("status") == "OK":
        m.update(f["values"])
    return m


_OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
        "==": operator.eq, "!=": operator.ne}


def evaluate_condition(c: InvalidationCondition, metrics: dict) -> bool | None:
    """True = condition met (thesis invalidated), None = cannot evaluate (metric missing)."""
    actual = metrics.get(c.metric)
    if actual is None:
        return None
    expected = c.value
    if isinstance(actual, str) or isinstance(expected, str):
        return _OPS[c.op](str(actual), str(expected))
    return _OPS[c.op](float(actual), float(expected))


def stale_sources(inp: DecisionInputs, rules: RuleSet) -> dict[str, str]:
    """Source name -> why it is stale. Same limits as gate 1."""
    stale = {}
    if inp.holding_as_of and (inp.now - inp.holding_as_of).total_seconds() / 3600 > rules.max_holdings_age_hours:
        stale["holdings"] = f"holdings fetched {inp.holding_as_of.isoformat()}"
    if inp.technical:
        age = (inp.today - date.fromisoformat(inp.technical.as_of)).days
        if age > rules.max_candle_age_days:
            stale["candles"] = f"last candle {inp.technical.as_of} ({age} days old)"
    return stale


def evaluate_conditions(inp: DecisionInputs, rules: RuleSet) -> list[ConditionResult]:
    """Every ACTIVE condition of the thesis, then the default warnings, each MET, NOT_MET or
    CANNOT_CHECK. Missing or stale data is CANNOT_CHECK with a reason, never read as met or passed."""
    own = [(c, False) for c in (inp.thesis.invalidation_conditions if inp.thesis else []) if c.status == "ACTIVE"]
    if any(w.category != "TECHNICAL" for w in inp.warnings):
        raise ValueError("Default warnings must be TECHNICAL")  # they must never reach gate 4
    metrics, stale = condition_metrics(inp), stale_sources(inp, rules)
    f = inp.fundamental
    f_ok = f is not None and f.get("status") == "OK"
    if problem := freshness_problem(f, inp.today, inp.now):
        stale["fundamentals"] = problem
    as_of = {"holdings": inp.holding_as_of.isoformat() if inp.holding_as_of else None,
             "candles": inp.technical.as_of if inp.technical else None,
             "fundamentals": f.get("latest_quarter") if f_ok else None}
    out = []
    for c, is_default in own + [(w, True) for w in inp.warnings]:
        source = METRIC_SOURCES.get(c.metric, "computed")
        base = dict(id=c.id, category=c.category, description=c.description or f"{c.metric} {c.op} {c.value}",
                    metric=c.metric, op=c.op, value=c.value, source=source, source_as_of=as_of.get(source),
                    default=is_default)
        if source == "fundamentals" and f_ok:
            evidence = f["inputs"].get(c.metric)
            base |= {"fetched_at": f["fetched_at"], "inputs": evidence,
                     "period": (f"quarter ended {evidence['latest']['period_end']} vs "
                                f"{evidence['year_ago']['period_end']} ({f['basis'].lower()})") if evidence
                     else f"quarter ended {f['latest_quarter']} ({f['basis'].lower()})"}
        if source in stale:
            reason = stale[source] if source == "fundamentals" else f"data stale: {stale[source]}"
            out.append(ConditionResult(**base, result="CANNOT_CHECK", reason=reason))
            continue
        met = evaluate_condition(c, metrics)
        if met is None:
            reason = (f["reasons"].get(c.metric, f"no value for {c.metric}") if source == "fundamentals" and f_ok
                      else f"no value for {c.metric}")
            out.append(ConditionResult(**base, result="CANNOT_CHECK", reason=reason))
        else:
            out.append(ConditionResult(**base, result="MET" if met else "NOT_MET", actual=metrics[c.metric]))
    return out


# --------------------------------------------------------------------------- scoring


def component_scores(t: TechnicalReport, horizon: Horizon, rules: RuleSet,
                     news: float | None = None) -> dict[str, float | None]:
    trend = {"UP": 1.0, "SIDEWAYS": 0.0, "DOWN": -1.0}[t.trend] if t.trend else None
    long_trend = None if t.long_term_trend is None else (1.0 if t.long_term_trend == "ABOVE_SMA200" else -1.0)
    momentum = None
    if t.rsi14 is not None and t.macd_hist is not None:
        rsi_part = max(-1.0, min(1.0, (t.rsi14 - 50) / 25))
        macd_part = 1.0 if t.macd_hist > 0 else -1.0 if t.macd_hist < 0 else 0.0
        momentum = 0.5 * rsi_part + 0.5 * macd_part
        # Overextension only matters for short horizons; LONG_TERM + RSI 72 is not a sell reason.
        if horizon == Horizon.SHORT_TERM and t.rsi14 > rules.overbought_rsi:
            momentum -= 0.5
        momentum = round(max(-1.0, min(1.0, momentum)), 4)
    breakout = 1.0 if t.breakout else -1.0 if t.breakdown else 0.0
    # News counts only in rule sets that give it a share (rules-1.4.0); None = missing, never zero (D11.2).
    return {"trend": trend, "long_trend": long_trend, "momentum": momentum, "breakout": breakout,
            "fundamental": None, "news": news if rules.news_share else None}


def weighted_score(components: dict[str, float | None], horizon: Horizon, rules: RuleSet) -> float | None:
    weights = rules.weights[horizon]
    usable = {k: w for k, w in weights.items() if components.get(k) is not None}
    total = sum(usable.values())
    if not total:
        return None
    base = sum(components[k] * w for k, w in usable.items()) / total
    news = components.get("news")
    if rules.news_share and news is not None:
        # D11.3: news takes its share; the other components keep their relative weights. Without a
        # news score this branch is skipped, so the result is exactly the rules-1.3.0 score.
        share = rules.news_share[horizon]
        return round((1 - share) * base + share * news, 4)
    return round(base, 4)


# --------------------------------------------------------------------------- engine


def decide(inp: DecisionInputs, rules: RuleSet, validated_versions: set[str]) -> DecisionOutput:
    conditions = evaluate_conditions(inp, rules)
    out = _decide(inp, rules, validated_versions, conditions)
    out.conditions = conditions
    out.decision.summary = summarize(out, inp)
    return out


def gate4_conditions(conditions: list[ConditionResult]) -> list[ConditionResult]:
    """The owner's active THESIS and BUSINESS conditions; TECHNICAL ones never reach gate 4 (D7.2)."""
    return [c for c in conditions if c.category in GATE4_CATEGORIES]


# --------------------------------------------------------------------------- wording


_HORIZON_WORDS = {Horizon.SHORT_TERM: "short-term", Horizon.MEDIUM_TERM: "medium-term",
                  Horizon.LONG_TERM: "long-term"}


def _join(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1] if parts else ""


def _drivers(c: dict[str, float | None], horizon: Horizon | None) -> str:
    """The technical picture as one clause, most relevant factor for the horizon first:
    'the price is below its 200-day average, the trend is down and momentum is weak'."""
    trend = {1.0: "the trend is up", 0.0: "the trend is sideways", -1.0: "the trend is down"}.get(c.get("trend"))
    long_trend = {1.0: "the price is above its 200-day average",
                  -1.0: "the price is below its 200-day average"}.get(c.get("long_trend"))
    m = c.get("momentum")
    momentum = None if m is None else "momentum is positive" if m >= 0.25 else "momentum is weak" if m <= -0.25 \
        else "momentum is neutral"
    breakout = {1.0: "it has just broken out on high volume",
                -1.0: "it has just broken down on high volume"}.get(c.get("breakout"))
    order = [long_trend, trend, momentum] if horizon == Horizon.LONG_TERM else [trend, long_trend, momentum]
    return _join(order + [breakout])


_PCT_METRICS = {"pnl_pct", "weight", "sector_weight", "pct_from_52w_high", "close_vs_sma200",
                "revenue_yoy", "profit_yoy", "pbt_yoy", "promoter_holding"}
_PRICE_METRICS = {"last_price", "ema20", "ema50", "sma200"}
_VALUE_WORDS = {("trend", "UP"): "the trend is now up", ("trend", "DOWN"): "the trend is now down",
                ("trend", "SIDEWAYS"): "the trend is now sideways",
                ("long_term_trend", "BELOW_SMA200"): "the price is now below its 200-day average",
                ("long_term_trend", "ABOVE_SMA200"): "the price is now above its 200-day average"}
_METRIC_WORDS = {"pnl_pct": "your P&L", "weight": "the position's weight", "sector_weight": "the sector's weight",
                 "pct_from_52w_high": "distance from the 52-week high", "close_vs_sma200": "distance from the 200-day average",
                 "rsi14": "RSI", "volume_ratio": "volume vs its 20-day average", "macd_hist": "the MACD histogram",
                 "last_price": "the price", "ema20": "the 20-day average", "ema50": "the 50-day average",
                 "sma200": "the 200-day average",
                 "revenue_yoy": "revenue growth on the year-ago quarter", "profit_yoy": "profit growth on the year-ago quarter",
                 "pbt_yoy": "pre-tax profit growth on the year-ago quarter",
                 "net_profit_q": "the latest quarter's net profit (₹ crore)",
                 "loss_quarters_4": "loss-making quarters in the last four", "pb_ratio": "price to book",
                 "debt_to_equity": "debt to equity", "promoter_holding": "promoter holding"}


def condition_in_words(c: ConditionResult) -> str:
    actual = c.actual
    if isinstance(actual, str):
        now = _VALUE_WORDS.get((c.metric, actual), f"{c.metric} is now {actual}")
    elif c.metric in _PCT_METRICS:
        now = f"{_METRIC_WORDS[c.metric]} is {actual:+.2%}"
    elif c.metric in _PRICE_METRICS:
        now = f"{_METRIC_WORDS[c.metric]} is ₹{actual:,.2f}"
    else:
        now = f"{_METRIC_WORDS.get(c.metric, c.metric)} is {actual}"
    return f"“{c.description.rstrip('.')}” ({now})"


def _price_evidence(t: TechnicalReport | None, c: dict[str, float | None], state: str = "",
                    vc: dict | None = None, ev: dict | None = None) -> str:
    """What the price chart shows, in numbers and plain words, for the owner."""
    if t is None:
        return ""
    blocked = bool(vc and vc.get("passed") is False)  # a buy held back by the volume filter
    out = []
    if t.sma200:
        pct = t.close / t.sma200 - 1
        out.append(f"The price (₹{t.close:,.2f}) is {abs(pct):.1%} {'below' if pct < 0 else 'above'} its 200-day "
                   f"average (₹{t.sma200:,.2f}), the usual gauge of the long-run trend.")
    if t.trend == "DOWN" and t.ema50:
        out.append(f"It has been falling: it trades below its 50-day average (₹{t.ema50:,.2f}), which is itself "
                   "below a falling 200-day average.")
    elif t.trend == "UP" and t.ema50:
        out.append(f"It has been rising: it trades above its 50-day average (₹{t.ema50:,.2f}), which is itself "
                   "above a rising 200-day average.")
    elif t.trend == "SIDEWAYS":
        out.append("Over recent months it has had no clear direction.")
    m = c.get("momentum")
    if m is not None and t.rsi14 is not None and t.macd_hist is not None:
        # The score averages RSI and MACD, so they can point different ways; say so instead of
        # contradicting the net reading.
        word = "positive" if m >= 0.25 else "weak" if m <= -0.25 else "neutral"
        rsi_dir = 1 if t.rsi14 > 55 else -1 if t.rsi14 < 45 else 0
        side = {1: "more buying than selling", -1: "more selling than buying",
                0: "buying and selling roughly balanced"}[rsi_dir]
        macd_dir = 1 if t.macd_hist > 0 else -1
        macd = "gaining strength" if macd_dir > 0 else "losing strength"
        rsi_text = f"RSI is {t.rsi14:.0f} out of 100 ({side} over the last 14 sessions)"
        macd_text = f"the MACD shows the short-term move {macd}"
        if rsi_dir in (0, macd_dir):
            out.append(f"Recent momentum is {word}: {rsi_text}, and {macd_text}.")
        else:
            out.append(f"Short-term signals are mixed: {rsi_text}, but {macd_text}; on balance the app reads "
                       f"momentum as {word}.")
    if ev:  # the last completed session's heavy-volume event (D10), not today's open candle
        side, word = ("above", "high") if ev["type"] == "BREAKOUT" else ("below", "low")
        out.append(f"On {ev['date']} it closed at ₹{ev['close']:,.2f}, {side} its 20-session {word} of "
                   f"₹{ev['level']:,.2f}, on {ev['volume_ratio']:.1f}× its usual volume.")
    elif (state.startswith(("BUY", "SELL")) or blocked) and vc and vc.get("ratio") is not None:
        # The same measure the rules-1.2.0 buy filter uses (D9): 5 completed sessions vs 20.
        move = "fall" if state.startswith("SELL") else "rise"
        v = vc["ratio"]
        today = " (today's session is still open, so it isn't counted)" if vc.get("excluded_open_session") else ""
        out.append(f"Over the last 5 completed sessions{today}, volume averaged {v:.2f}× its 20-session average: " + (
            f"well above normal, so the {move} has broad participation." if v >= 1.5 else
            f"about normal, so the {move} isn't backed by unusually heavy trading." if v >= 1.0 else
            f"below normal, so fewer traders than usual are behind the {move}."))
    return " ".join(out)


def _business_fact(inp: DecisionInputs) -> str:
    """One reported figure for context, stated as a fact, never as a judgement (D1.5)."""
    f = inp.fundamental
    if not f or f.get("status") != "OK" or freshness_problem(f, inp.today, inp.now):
        return ""
    for metric, label in (("profit_yoy", "net profit"), ("revenue_yoy", "total income")):
        v = f["values"].get(metric)
        if v is not None:
            return (f" (latest results, quarter ended {f['latest_quarter']}: {label} {v:+.1%} on the year-ago "
                    f"quarter, {f['basis'].lower()})")
    return ""


def _signal_action(state: str, horizon: Horizon | None, inp: DecisionInputs) -> str:
    h = _HORIZON_WORDS.get(horizon, "")
    if state.startswith("SELL"):
        if horizon == Horizon.LONG_TERM:
            return ("For a long-term holding, price alone is never a reason to sell. Check whether anything has "
                    f"changed in the business or in your reason for holding{_business_fact(inp)}. If nothing has, "
                    "no action is needed.")
        if horizon is None:
            return "Record a horizon on this page so the app can say how much this matters for you."
        return (f"For a {h} holding this can matter: compare it with your plan, for example the loss you decided "
                "you would accept, or when you meant to exit. The app never sells.")
    if state.startswith("BUY"):
        return ("If you were thinking of adding, first check your position limit and whether your reason for "
                "buying still holds. The app never buys.")
    if state == "DONT_ADD":
        return "Don't add more. Holding what you have is fine."
    return "No action needed."


def summarize(out: DecisionOutput, inp: DecisionInputs) -> str:
    """Owner-facing wording for the decision: one line for lists. The full explanation, with numbers
    and what to do, goes to decision.guidance (see _guidance)."""
    d = out.decision
    fired = {g.gate: g.detail for g in out.gates if g.status == "FIRED"}
    horizon = inp.thesis.horizon if inp.thesis else None
    h = _HORIZON_WORDS.get(horizon, "")
    drivers = _drivers(out.component_scores, horizon)
    d.guidance = _guidance(out, inp, fired)

    if 1 in fired:
        return "No signal: the data is out of date. Log in to Kite so the app can refresh it."
    if 2 in fired:
        if "candles" in fired[2]:
            return "No signal: there is no price history for this stock."
        return ("No signal yet: there isn't enough price history. The 200-day average needs about "
                "200 trading days.")
    if 3 in fired:
        if d.thesis_status == "missing":
            return "No signal yet: record a thesis (why you hold it and for how long) to get one."
        return "No signal yet: add a horizon to your thesis."
    if 4 in fired and inp.thesis:
        met = [condition_in_words(c) for c in gate4_conditions(out.conditions) if c.result == "MET"]
        return f"Review your {h} thesis: your condition {' and '.join(met)} has been met."
    if 5 in fired:
        return f"Review before acting: {fired[5]}."

    ev = d.volume_event
    if d.signal_source == "volume_event" and ev:
        vr = f"{ev['volume_ratio']:.1f}× normal volume"
        if ev["type"] == "BREAKDOWN":
            if d.state == "REVIEW":
                return f"Review: heavy selling on a long-term holding. It closed below its 20-session low on {vr}."
            return f"Sell signal: heavy selling. It closed below its 20-session low on {vr}."
        if d.state == "DONT_ADD":
            return (f"Don't add more: heavy buying took it above its 20-session high on {vr}, but you're above a "
                    "concentration limit.")
        return f"Buy signal: heavy buying. It closed above its 20-session high on {vr}."
    if d.state == "HOLD":
        if d.volume_check and d.volume_check.get("passed") is False:
            v = d.volume_check.get("ratio")
            vol = f" ({v:.2f}× normal over 5 sessions)" if v is not None else " (no volume data)"
            return f"Hold, not a buy: the price picture is positive, but trading volume doesn't confirm it{vol}."
        return f"Hold: {drivers}."
    if d.state.startswith("BUY"):
        return f"{'Buy candidate' if d.state == 'BUY_CANDIDATE' else 'Buy signal from the price rules'}: {drivers}."
    if d.state.startswith("SELL"):
        if horizon == Horizon.LONG_TERM:
            return f"Price weakness, not a reason to sell on its own: {drivers}."
        name = "Sell candidate" if d.state == "SELL_CANDIDATE" else "Sell signal from the price rules"
        return f"{name}: {drivers}. Check it against your plan."
    if d.state == "DONT_ADD":
        return f"Don't add more: {drivers}, but you're above a concentration limit."
    if d.score is None:
        return "No signal: the score could not be computed."
    return f"No action: the price picture is mixed ({drivers})."


def _guidance(out: DecisionOutput, inp: DecisionInputs, fired: dict[int, str]) -> dict[str, str]:
    """The decision explained for the owner: what the app sees (with numbers), what it is based on,
    what to do for this horizon, and a note when the horizon wasn't set by the owner."""
    d = out.decision
    horizon = inp.thesis.horizon if inp.thesis else None
    h = _HORIZON_WORDS.get(horizon, "")
    note = (f"The {h} horizon was set by the app from the stock's profile, not by you. Confirm or change it "
            "on this page." if d.thesis_status == "draft" else "")
    if 1 in fired or 2 in fired or 3 in fired:
        return {}  # the one-line summary already says what is missing and what to do
    if 4 in fired:
        why = inp.thesis.why_bought if inp.thesis else ""
        return {"evidence": " ".join(condition_in_words(c) + "." for c in gate4_conditions(out.conditions)
                                     if c.result == "MET"),
                "basis": "You set this condition as a sign that your reason for holding may no longer apply"
                         + (f" (your reason: “{why}”)." if why else "."),
                "action": "Re-read your thesis below, then confirm it still holds or update it. The app never sells.",
                "note": note}
    if 5 in fired:
        return {"evidence": f"{fired[5][:1].upper() + fired[5][1:]}.",
                "basis": "Results and major announcements can move the price sharply in either direction.",
                "action": "Re-read your thesis before the event so you know what result would change your mind.",
                "note": note}
    limit = ""
    if d.dont_add and inp.risk:
        r = inp.risk
        limit = (f" The position is {r.position_weight:.1%} of your portfolio, above your {r.position_limit:.0%} limit."
                 if r.position_breach else
                 f" {r.sector} is {r.sector_weight:.1%} of your portfolio, above your {r.sector_limit:.0%} sector limit.")
    basis = "This comes only from the price chart; it doesn't look at the business."
    if d.state.startswith(("BUY", "SELL")) and not d.rule_set_validated:
        basis += " The app's price rules haven't passed back-testing, so treat it as a prompt to look, not advice."
    blocked = bool(d.volume_check and d.volume_check.get("passed") is False)
    ev = d.volume_event
    evidence = (_price_evidence(inp.technical, out.component_scores, d.state, d.volume_check, ev) + limit
                + _news_sentence(inp.news, d.news_diagnostics)).strip()
    if d.signal_source == "volume_event" and ev:
        heavy = "buying" if ev["type"] == "BREAKOUT" else "selling"
        basis = (f"This comes from price and trading volume: heavy {heavy} took the price to a new 20-session "
                 f"{'high' if ev['type'] == 'BREAKOUT' else 'low'}. It doesn't look at the business, such moves "
                 "often reverse, and the rule hasn't passed back-testing, so it isn't advice.")
        if ev["type"] == "BREAKOUT":
            action = ("If you want to add, check your position limit and whether your reason for buying still "
                      "holds. The app never buys.") if d.state != "DONT_ADD" else "Don't add more: you're above a concentration limit."
        elif horizon == Horizon.LONG_TERM:
            action = ("Review, not sell: price and volume alone never sell a long-term holding. Check whether the "
                      f"business or your reason for holding has changed{_business_fact(inp)}. If not, holding is fine.")
        else:
            action = (f"For a {h} holding the app's rule says sell. Decide against your plan, for example the loss "
                      "you decided you would accept. The app never places orders.")
        return {"evidence": evidence, "basis": basis, "action": action, "note": note}
    if blocked:
        basis += (" The price chart scores this in the buy range, but a buy signal also needs volume over the "
                  f"last 5 sessions to be at least {d.volume_check['threshold']:.1f}× normal, and it isn't.")
    action = ("No action needed. If volume picks up while the price holds, this can turn into a buy signal."
              if blocked else _signal_action(d.state, horizon, inp))
    return {"evidence": evidence, "basis": basis, "action": action, "note": note}


def _decide(inp: DecisionInputs, rules: RuleSet, validated_versions: set[str],
            conditions: list[ConditionResult]) -> DecisionOutput:
    validated = rules.version in validated_versions
    thesis_status = "missing" if inp.thesis is None else "draft" if inp.thesis.draft else "confirmed"
    gates: list[GateResult] = []
    names = ["stale data", "missing data", "thesis missing", "thesis invalidation", "event window",
             "concentration"]

    def stop(state: State, reason: str) -> DecisionOutput:
        for i in range(len(gates) + 1, 7):
            gates.append(GateResult(gate=i, name=names[i - 1], status="SKIPPED"))
        return DecisionOutput(gates, {}, Decision(state=state, reason=reason, rule_version=rules.version,
                                                  rule_set_validated=validated, thesis_status=thesis_status,
                                                  evidence_unavailable=list(ENGINES_NOT_BUILT)))

    # Gate 1: freshness
    stale = list(stale_sources(inp, rules).values())
    if stale:
        gates.append(GateResult(gate=1, name=names[0], status="FIRED", detail="; ".join(stale)))
        return stop("NO_ACTION", "Stale data: " + "; ".join(stale))
    gates.append(GateResult(gate=1, name=names[0], status="PASS"))

    # Gate 2: required data
    missing = []
    if inp.holding is None and not inp.scan:
        missing.append("holding")
    if inp.technical is None:
        missing.append("candles")
    else:
        missing += [f for f in rules.required_technicals if getattr(inp.technical, f) is None]
    if missing:
        detail = "missing: " + ", ".join(missing)
        gates.append(GateResult(gate=2, name=names[1], status="FIRED", detail=detail))
        return stop("NO_ACTION", "Required data " + detail)
    gates.append(GateResult(gate=2, name=names[1], status="PASS"))

    # Gate 3: thesis + horizon
    if inp.thesis is None or inp.thesis.horizon is None:
        what = "thesis" if inp.thesis is None else "horizon"
        gates.append(GateResult(gate=3, name=names[2], status="FIRED", detail=f"no {what}"))
        return stop("NO_ACTION", "Thesis missing" if what == "thesis" else "Thesis has no horizon")
    gates.append(GateResult(gate=3, name=names[2], status="PASS",
                            detail=f"thesis v{inp.thesis.version}" + (" (draft, applied in bulk)" if inp.thesis.draft else "")))
    horizon = inp.thesis.horizon

    # Gate 4: the owner's active THESIS and BUSINESS invalidation conditions (D7.2)
    met, unevaluable = [], []
    for c in gate4_conditions(conditions):
        if c.result == "CANNOT_CHECK":
            unevaluable.append(c.description)
        elif c.result == "MET":
            met.append(f"{c.description} (actual {c.metric} = {c.actual})")
    if met:
        gates.append(GateResult(gate=4, name=names[3], status="FIRED", detail="; ".join(met)))
        return stop("REVIEW", "Thesis invalidation condition met: " + "; ".join(met))
    gates.append(GateResult(gate=4, name=names[3], status="PASS",
                            detail=("cannot evaluate: " + "; ".join(unevaluable)) if unevaluable else ""))

    # Gate 5: events
    hits = []
    for e in inp.events:
        days = (e.date - inp.today).days
        if e.source == "news":
            # Official NSE events (D11.1): only rule sets with news_events, each with its own window.
            if rules.news_events and e.before_days is not None and -(e.after_days or 0) <= days <= e.before_days:
                when = "in {0} days".format(days) if days > 0 else "today" if days == 0 else f"{-days} days ago"
                hits.append(f"{e.description} ({when}, NSE)")
            continue
        if e.type == "RESULTS" and 0 <= days <= rules.results_window_days:
            hits.append(f"results on {e.date.isoformat()} ({days} days)")
        elif e.major and -rules.results_window_days <= days <= rules.results_window_days:
            hits.append(f"{e.description} on {e.date.isoformat()}")
    if hits:
        gates.append(GateResult(gate=5, name=names[4], status="FIRED", detail="; ".join(hits)))
        return stop("REVIEW", "Event window: " + "; ".join(hits))
    gates.append(GateResult(gate=5, name=names[4], status="PASS"))

    # Gate 6: concentration (does not stop evaluation)
    dont_add = bool(inp.risk and (inp.risk.position_breach or inp.risk.sector_breach))
    if dont_add:
        why = []
        if inp.risk.position_breach:
            why.append(f"position {inp.risk.position_weight:.1%} > {inp.risk.position_limit:.0%}")
        if inp.risk.sector_breach:
            why.append(f"sector {inp.risk.sector} {inp.risk.sector_weight:.1%} > {inp.risk.sector_limit:.0%}")
        gates.append(GateResult(gate=6, name=names[5], status="FIRED", detail="; ".join(why)))
    else:
        gates.append(GateResult(gate=6, name=names[5], status="PASS"))

    n = inp.news
    news_value = n.get("score") if n and n.get("status") in ("SUCCESS", "PARTIAL") else None
    components = component_scores(inp.technical, horizon, rules, news_value)
    score = weighted_score(components, horizon, rules)
    news_diag = None
    if rules.news_share:
        without = weighted_score({**components, "news": None}, horizon, rules)
        news_diag = {"score_with_news": score, "score_without_news": without,
                     "news_score": components.get("news"),
                     "news_contribution": None if score is None or without is None else round(score - without, 4),
                     "impact": _news_impact(score, without, components.get("news"), rules)}
    # Backtests validate a rule version per horizon ("rules-1.0.0:LONG_TERM"); a bare version covers all.
    validated = validated or f"{rules.version}:{horizon.value}" in validated_versions
    suffix = "CANDIDATE" if validated else "SIGNAL"
    tax = None
    # Volume over completed sessions only (D9.2): before 15:30 IST today's candle is still forming.
    t = inp.technical
    session_open = t.as_of == inp.today.isoformat() and inp.now.astimezone(IST).time() < MARKET_CLOSE_IST
    vol = t.volume_5d_ratio_excl_last if session_open else t.volume_5d_ratio
    volume_check = {"ratio": vol, "threshold": rules.buy_min_volume_5d, "excluded_open_session": session_open}
    event = t.volume_event_prev if session_open else t.volume_event
    event = event if event and event.get("type") else None
    source = "score"
    if event and rules.volume_events:
        source = "volume_event"
        what = (f"{event['type'].lower()} on {event['date']}: close {event['close']} vs 20-session "
                f"{'high' if event['type'] == 'BREAKOUT' else 'low'} {event['level']} on "
                f"{event['volume_ratio']}x volume")
        if event["type"] == "BREAKDOWN":
            if horizon == Horizon.LONG_TERM:
                state, reason = "REVIEW", f"Heavy-volume {what}; long-term holding, so REVIEW not SELL (D1.2)"
            else:
                state, reason = f"SELL_{suffix}", f"Heavy-volume {what}"
                tax = inp.tax_impact
        elif dont_add:
            state, reason = "DONT_ADD", f"Heavy-volume {what}, but concentration limit is reached"
        else:
            state, reason = f"BUY_{suffix}", f"Heavy-volume {what}"
    elif score is None:
        state, reason = "NO_ACTION", "No score components available"
    elif score >= rules.buy_threshold:
        if dont_add:
            state, reason = "DONT_ADD", f"Score {score} is in the buy band but concentration limit is reached"
        elif rules.buy_min_volume_5d is not None and (vol is None or vol < rules.buy_min_volume_5d):
            volume_check["passed"] = False
            state = "HOLD"
            reason = (f"Score {score} >= buy threshold {rules.buy_threshold}, but volume is not confirmed: "
                      f"5-session volume {'unavailable' if vol is None else f'{vol}x'} vs required "
                      f"{rules.buy_min_volume_5d}x of the 20-session average")
        else:
            if rules.buy_min_volume_5d is not None:
                volume_check["passed"] = True
            state, reason = f"BUY_{suffix}", f"Score {score} >= buy threshold {rules.buy_threshold}"
    elif score <= rules.sell_threshold:
        state, reason = f"SELL_{suffix}", f"Score {score} <= sell threshold {rules.sell_threshold}"
        tax = inp.tax_impact
    elif score >= rules.hold_threshold:
        state, reason = "HOLD", f"Score {score} in hold band, thesis intact"
    else:
        state, reason = "NO_ACTION", f"Score {score} in neutral band"

    return DecisionOutput(
        gates,
        components,
        Decision(state=state, reason=reason, dont_add=dont_add, score=score, rule_version=rules.version,
                 rule_set_validated=validated, tax_impact=tax, thesis_status=thesis_status,
                 volume_check=volume_check, volume_event=event, signal_source=source, news_diagnostics=news_diag,
                 evidence_unavailable=[k for k in ENGINES_NOT_BUILT if components.get(k) is None]),
    )


def _news_sentence(news: dict | None, diag: dict | None) -> str:
    """One sentence on recent news, source-backed. Under the live rules it is context only."""
    if not news or not news.get("clusters"):
        return ""
    cl = news["clusters"]
    top = max(cl, key=lambda c: c["effective_weight"])
    src = next((s for s in top["sources"] if s["official"]), top["sources"][0])
    pos, neg = sum(1 for c in cl if c["sentiment"] > 0), sum(1 for c in cl if c["sentiment"] < 0)
    dup = news.get("counts", {}).get("duplicates_merged", 0)
    head = (f" News contributed {diag['news_contribution']:+.2f} to the score." if diag and diag.get("news_contribution")
            is not None else " Recent news (context only; it isn't part of the live signal yet):")
    events = f"{len(cl)} event" + ("s" if len(cl) != 1 else "")
    merged = f", {dup} duplicate report" + ("s" if dup != 1 else "") + " merged" if dup else ""
    return (f"{head} {events} in 7 days ({pos} positive, {neg} negative){merged}; strongest: "
            f"{top['summary'].rstrip('.')} ({src['publisher']}, {top['published']}).")


def _band(score: float | None, rules: RuleSet) -> str | None:
    if score is None:
        return None
    return ("BUY" if score >= rules.buy_threshold else "SELL" if score <= rules.sell_threshold
            else "HOLD" if score >= rules.hold_threshold else "NEUTRAL")


def _news_impact(with_news: float | None, without: float | None, news: float | None, rules: RuleSet) -> str:
    """D11.4: what news did. REVERSED = band and direction flipped; MODIFIED = band changed, or a
    material move (≥ 0.05) against the existing direction; REINFORCED = news agrees with it."""
    if news is None or with_news is None or without is None or abs(with_news - without) < 0.01:
        return "NO_IMPACT"
    band_changed = _band(with_news, rules) != _band(without, rules)
    if band_changed and (with_news > 0) != (without > 0):
        return "REVERSED"
    agrees = (news > 0) == (without > 0)
    if band_changed or (not agrees and abs(with_news - without) >= 0.05):
        return "MODIFIED"
    return "REINFORCED" if agrees else "NO_IMPACT"

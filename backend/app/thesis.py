"""Horizon-aware investment thesis."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Metrics an invalidation condition may reference. They are resolved deterministically
# from the snapshot; unknown metrics are rejected at save time.
CONDITION_METRICS = {
    "last_price": "Last traded price (₹)",
    "pnl_pct": "Unrealised P&L as a fraction of invested (0.10 = +10%)",
    "weight": "Position weight in portfolio (fraction)",
    "sector_weight": "Sector weight in portfolio (fraction)",
    "rsi14": "RSI(14)",
    "ema20": "EMA(20)",
    "ema50": "EMA(50)",
    "sma200": "SMA(200)",
    "macd_hist": "MACD histogram",
    "volume_ratio": "Today's volume / 20-session average",
    "pct_from_52w_high": "Distance from 52-week high (fraction, negative below)",
    "close_vs_sma200": "Close / SMA(200) - 1 (fraction)",
    "trend": "UP | DOWN | SIDEWAYS",
    "long_term_trend": "ABOVE_SMA200 | BELOW_SMA200",
}

# Business metrics (D8): from the fundamentals provider, standalone figures, growth computed by us
# (latest quarter vs the same quarter a year earlier). P/E is deliberately absent.
BUSINESS_METRICS = {
    "revenue_yoy": "Total income, latest quarter vs year-ago quarter (fraction; standalone)",
    "profit_yoy": "Net profit, latest quarter vs year-ago quarter (fraction; standalone)",
    "pbt_yoy": "Profit before tax, latest quarter vs year-ago quarter (fraction; standalone)",
    "net_profit_q": "Net profit, latest quarter (₹ crore; standalone)",
    "loss_quarters_4": "Loss-making quarters among the last four (count)",
    "pb_ratio": "Price / book value",
    "debt_to_equity": "Debt / equity (not for banks, NBFCs or insurers)",
    "promoter_holding": "Promoter holding (fraction)",
}
CONDITION_METRICS |= BUSINESS_METRICS

# Condition categories (decisions.md D7). Only ACTIVE THESIS and BUSINESS conditions reach gate 4;
# TECHNICAL ones only raise warnings.
Category = Literal["THESIS", "BUSINESS", "TECHNICAL"]
GATE4_CATEGORIES = ("THESIS", "BUSINESS")

# Which categories may use each metric. Price/technical metrics may appear in an owner's THESIS
# condition, never in BUSINESS. Business metrics arrive with the fundamentals provider.
METRIC_CATEGORIES: dict[str, tuple[str, ...]] = {
    m: ("BUSINESS", "THESIS") if m in BUSINESS_METRICS else ("TECHNICAL", "THESIS") for m in CONDITION_METRICS}

# Where each metric's value comes from, for freshness and alert evidence.
_HOLDINGS_METRICS = ("last_price", "pnl_pct", "weight", "sector_weight")
METRIC_SOURCES = {m: "holdings" if m in _HOLDINGS_METRICS else "fundamentals" if m in BUSINESS_METRICS
                  else "candles" for m in CONDITION_METRICS}

ENUM_VALUES = {"trend": ["UP", "DOWN", "SIDEWAYS"], "long_term_trend": ["ABOVE_SMA200", "BELOW_SMA200"]}
FRACTION_METRICS = {"pnl_pct", "weight", "sector_weight", "pct_from_52w_high", "close_vs_sma200",
                    "revenue_yoy", "profit_yoy", "pbt_yoy", "promoter_holding"}


def metric_catalogue() -> dict[str, dict]:
    return {m: {"description": d, "categories": list(METRIC_CATEGORIES[m]), "source": METRIC_SOURCES[m],
                "kind": "enum" if m in ENUM_VALUES else "fraction" if m in FRACTION_METRICS else "number",
                "values": ENUM_VALUES.get(m)}
            for m, d in CONDITION_METRICS.items()}


class Horizon(str, Enum):
    SHORT_TERM = "SHORT_TERM"
    MEDIUM_TERM = "MEDIUM_TERM"
    LONG_TERM = "LONG_TERM"


class InvalidationCondition(BaseModel):
    # Defaults reproduce conditions saved before D7: an owner's active THESIS condition.
    id: str = ""  # stable across thesis versions; assigned on load/save when empty
    category: Category = "THESIS"
    metric: str
    op: Literal["<", "<=", ">", ">=", "==", "!="]
    value: float | str
    description: str = ""
    origin: Literal["OWNER", "SUGGESTED"] = "OWNER"
    # Only ACTIVE is evaluated. PROPOSED = a suggestion awaiting the owner; DISMISSED = rejected.
    status: Literal["ACTIVE", "PROPOSED", "DISMISSED"] = "ACTIVE"
    confirmed_at: datetime | None = None

    @field_validator("metric")
    @classmethod
    def _known_metric(cls, v: str) -> str:
        if v not in CONDITION_METRICS:
            raise ValueError(f"Unknown metric {v!r}; allowed: {sorted(CONDITION_METRICS)}")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> InvalidationCondition:
        if self.category not in METRIC_CATEGORIES[self.metric]:
            raise ValueError(f"Metric {self.metric!r} is not allowed in a {self.category} condition; "
                             f"allowed categories: {list(METRIC_CATEGORIES[self.metric])}")
        if self.status != "ACTIVE" and self.origin != "SUGGESTED":
            raise ValueError("Only suggested conditions can be PROPOSED or DISMISSED; delete your own instead")
        # Compared as given, "-0.2" would be compared as text ("-0.15" < "-0.2" is True).
        if self.metric in ENUM_VALUES:
            if self.value not in ENUM_VALUES[self.metric] or self.op not in ("==", "!="):
                raise ValueError(f"{self.metric} takes == or != and one of {ENUM_VALUES[self.metric]}")
        elif isinstance(self.value, str):
            try:
                self.value = float(self.value)
            except ValueError:
                raise ValueError(f"{self.metric} needs a number, got {self.value!r}") from None
        return self


class ThesisIn(BaseModel):
    why_bought: str = Field(min_length=1)
    horizon: Horizon | None = None
    assumptions: list[str] = []
    metrics_to_monitor: list[str] = []
    invalidation_conditions: list[InvalidationCondition] = []
    notes: str = ""
    # True for theses applied in bulk: they satisfy gate 3 but are flagged until edited individually.
    draft: bool = False

    @model_validator(mode="after")
    def _condition_ids(self) -> ThesisIn:
        """Give every condition a unique id (c1, c2, ...) so alerts can follow it across versions."""
        used = {c.id for c in self.invalidation_conditions if c.id}
        if len(used) != sum(1 for c in self.invalidation_conditions if c.id):
            raise ValueError("Condition ids must be unique")
        if any(i.startswith("w_") for i in used):
            raise ValueError("Condition ids starting with 'w_' are reserved for default warnings")
        n = 0
        for c in self.invalidation_conditions:
            while not c.id:
                n += 1
                if f"c{n}" not in used:
                    c.id = f"c{n}"
                    used.add(c.id)
        return self

    def confirm_conditions(self, now: datetime) -> ThesisIn:
        """Stamp the owner's confirmation on ACTIVE conditions that don't have one yet."""
        for c in self.invalidation_conditions:
            if c.status == "ACTIVE" and c.confirmed_at is None:
                c.confirmed_at = now
        return self


class BulkThesisIn(BaseModel):
    symbols: list[str] = Field(min_length=1)
    thesis: ThesisIn
    skip_existing: bool = True  # never overwrite a thesis the user already wrote


class Thesis(ThesisIn):
    symbol: str
    version: int
    updated_at: datetime

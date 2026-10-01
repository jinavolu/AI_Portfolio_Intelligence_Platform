"""Snapshots: every analysis is frozen with its inputs, outputs, versions and hashes,
so any decision can be replayed and "what changed?" is a diff, not a guess."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.broker.models import MarketEvent
from app.decision import ConditionResult, Decision, GateResult
from app.engines.portfolio import HoldingReport
from app.engines.risk import HoldingRisk, RiskReport
from app.engines.technical import TechnicalReport
from app.thesis import Thesis


def canonical_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class HoldingSnapshot(BaseModel):
    symbol: str
    data_as_of: dict[str, str | None]
    portfolio: HoldingReport
    market: dict[str, float | None]
    technical: TechnicalReport | None
    fundamental: dict | None = None  # Phase 11
    news: dict | None = None  # Phase 9: news_service view + price since each event (D11)
    # rules-1.4.0 decision computed in shadow mode (D11.5): state, score, diagnostics. Never the signal.
    shadow: dict | None = None
    thesis: Thesis | None
    risk: HoldingRisk | None
    events: list[MarketEvent]
    gates: list[GateResult]
    component_scores: dict[str, float | None]
    decision: Decision
    # Every ACTIVE thesis condition's result. None in snapshots taken before D7.
    conditions: list[ConditionResult] | None = None
    content_hash: str = ""

    def compute_content_hash(self) -> str:
        """Hash of everything that affects meaning. Fetch timestamps are excluded so an
        unchanged holding re-fetched later keeps its hash (and its AI cache entry)."""
        body = self.model_dump(mode="json", exclude={"content_hash", "data_as_of"})
        body["candles_as_of"] = self.data_as_of.get("candles")
        return canonical_hash(body)


class Snapshot(BaseModel):
    id: str | None = None
    created_at: datetime
    data_as_of: dict[str, str | None]
    versions: dict[str, str]
    rule_set_validated: bool
    portfolio_summary: dict[str, Any]
    risk: RiskReport
    holdings: dict[str, HoldingSnapshot]
    input_hash: str
    ai: dict | None = None


# --------------------------------------------------------------------------- diff


def _band(rsi: float | None) -> str | None:
    if rsi is None:
        return None
    return "OVERBOUGHT" if rsi > 70 else "OVERSOLD" if rsi < 30 else "NEUTRAL"


def diff_snapshots(old: Snapshot, new: Snapshot) -> dict[str, Any]:
    """Structured diff. Version changes are reported separately so "the rules changed"
    is never confused with "the data changed"."""
    version_changes = {k: {"from": old.versions.get(k), "to": v}
                       for k, v in new.versions.items() if old.versions.get(k) != v}
    added = sorted(set(new.holdings) - set(old.holdings))
    removed = sorted(set(old.holdings) - set(new.holdings))
    changes: dict[str, list[dict]] = {}

    for sym in sorted(set(new.holdings) & set(old.holdings)):
        a, b = old.holdings[sym], new.holdings[sym]
        items: list[dict] = []

        def add(field: str, before, after, **extra):
            if before != after:
                items.append({"field": field, "from": before, "to": after, **extra})

        add("decision", a.decision.state, b.decision.state)
        add("dont_add", a.decision.dont_add, b.decision.dont_add)
        add("quantity", a.portfolio.quantity, b.portfolio.quantity)
        add("thesis_version", a.thesis.version if a.thesis else None, b.thesis.version if b.thesis else None)
        ta, tb = a.technical, b.technical
        add("trend", ta.trend if ta else None, tb.trend if tb else None)
        add("long_term_trend", ta.long_term_trend if ta else None, tb.long_term_trend if tb else None)
        add("rsi_band", _band(ta.rsi14 if ta else None), _band(tb.rsi14 if tb else None))
        add("breakout", ta.breakout if ta else None, tb.breakout if tb else None)
        fired_a = [g.gate for g in a.gates if g.status == "FIRED"]
        fired_b = [g.gate for g in b.gates if g.status == "FIRED"]
        add("gates_fired", fired_a, fired_b)
        pa, pb = a.portfolio.last_price, b.portfolio.last_price
        if pa and pa != pb:
            items.append({"field": "price_change_pct", "from": pa, "to": pb,
                          "change": round((pb - pa) / pa, 6)})
        dw = round(b.portfolio.weight - a.portfolio.weight, 6)
        if abs(dw) >= 0.005:
            items.append({"field": "weight", "from": a.portfolio.weight, "to": b.portfolio.weight,
                          "change": dw})
        if items:
            changes[sym] = items

    so, sn = old.portfolio_summary, new.portfolio_summary
    return {
        "from_snapshot": old.id,
        "to_snapshot": new.id,
        "from_created_at": old.created_at.isoformat(),
        "to_created_at": new.created_at.isoformat(),
        "version_changes": version_changes,
        "rules_changed": "rules" in version_changes,
        "portfolio": {
            "value_from": so.get("total_current_value"),
            "value_to": sn.get("total_current_value"),
            "value_change": (round(sn["total_current_value"] - so["total_current_value"], 2)
                             if sn.get("total_current_value") is not None
                             and so.get("total_current_value") is not None else None),
        },
        "added_holdings": added,
        "removed_holdings": removed,
        "holdings": changes,
    }

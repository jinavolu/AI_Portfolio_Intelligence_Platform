"""Alerts (plan Phase 13): "This needs your attention." Never trades.

Alerts are transitions between two consecutive snapshots, so each fires once when the
condition starts, not on every refresh while it holds. Everything is deterministic.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.decision import GateResult, condition_in_words
from app.snapshot import HoldingSnapshot, Snapshot
from app.thesis import Horizon

Severity = Literal["high", "medium", "info"]


class AlertIn(BaseModel):
    symbol: str
    type: str
    severity: Severity
    message: str
    details: dict = {}

    @property
    def key(self) -> str:
        """What tells this alert apart from another open one of the same symbol and type: another sector,
        disclosure, signal or condition is news. Empty for price levels, so a steady fall doesn't
        re-alert at every lower support."""
        d = self.details
        if self.type == "SIGNAL_CHANGED":
            return f"{d.get('from')}>{d.get('to')}"
        if "conditions" in d:
            return ",".join(sorted(c["id"] for c in d["conditions"]))[:80]
        return str(d.get("sector") or d.get("cluster_id") or "")[:80]


def _fired(hs: HoldingSnapshot, gate: int) -> GateResult | None:
    g = next((g for g in hs.gates if g.gate == gate), None)
    return g if g and g.status == "FIRED" else None


def technical_severity(horizon: Horizon | None) -> Severity:
    """Technical warnings stay quiet for long-term holdings (D7.6)."""
    return "info" if horizon == Horizon.LONG_TERM else "medium"


# A support/resistance alert needs the price at least this far past the level, so a price wobbling
# around a level between 15-minute refreshes doesn't raise an alert for a 10-paise move.
LEVEL_CROSS_MARGIN = 0.01

_HORIZON = {Horizon.SHORT_TERM: "short-term", Horizon.MEDIUM_TERM: "medium-term", Horizon.LONG_TERM: "long-term"}


def _rs(v: float) -> str:
    return f"₹{v:,.2f}"


def _explain(what: str, meaning: str, action: str, headline: str = "", facts: list | None = None) -> dict:
    """Every alert answers: what happened, what it means, what to do (monitoring-design.md section 6).
    `headline` and `facts` ([label, value] pairs) are the scannable view; `what` is the full sentence."""
    return {"what": what, "meaning": meaning, "action": action, "headline": headline or what, "facts": facts or []}


def _price_action(hs: HoldingSnapshot, shorter: str) -> str:
    """What a price-only signal asks of the owner, by horizon (D1.2: price alone never sells a long-term
    holding). The horizon and whether it's a draft are shown as tags, so they aren't repeated here."""
    h = hs.thesis.horizon if hs.thesis else None
    if h == Horizon.LONG_TERM:
        return "No action needed: for a long-term holding, price moves alone never lead to a sale."
    if h is None:
        return "Record a horizon on the stock page so the app can tell how much this matters for you."
    return shorter[:1].upper() + shorter[1:]


def _pct(v: float) -> str:
    return f"{v:+.1%}"


def _fmt_metric(metric: str, v) -> str:
    from app.thesis import FRACTION_METRICS
    if v is None:
        return "—"
    if isinstance(v, str):
        return v.replace("_", " ").lower()
    return f"{v:+.2%}" if metric in FRACTION_METRICS else f"{v:,.2f}"


def _evidence(c) -> str:
    """Both raw quarters behind a growth figure, e.g. ': net profit ₹120 cr (2026-06-30) vs ₹190 cr
    (2025-06-30), standalone'. A one-off quarter is visible here, not hidden in the percentage."""
    i = c.inputs
    if not i:
        return f": {c.period}" if c.period else ""
    basis = (c.period or "").rsplit("(", 1)[-1].rstrip(")") if c.period else ""
    return (f": {i['label']} ₹{i['latest']['value']:,.0f} cr ({i['latest']['period_end']}) vs "
            f"₹{i['year_ago']['value']:,.0f} cr ({i['year_ago']['period_end']}){', ' + basis if basis else ''}")


_CONDITION_ALERTS = {"THESIS": ("THESIS_CONDITION", "Thesis invalidation condition met"),
                     "BUSINESS": ("BUSINESS_CONDITION", "Business condition met"),
                     "TECHNICAL": ("TECHNICAL_WARNING", "Technical warning")}


def _condition_alerts(a: HoldingSnapshot, b: HoldingSnapshot) -> list[AlertIn]:
    """One alert per category for conditions that became MET since the previous snapshot, with the
    evidence needed to read it later (monitoring-design.md section 5). Followed by condition id, so
    editing the thesis doesn't re-alert a condition that was already met."""
    if a.conditions is None or not b.conditions:
        return []  # the older snapshot predates condition results: no baseline to compare with
    before = {c.id: c for c in a.conditions}
    horizon = b.thesis.horizon if b.thesis else None

    def started(c) -> bool:
        prev = before.get(c.id)
        if c.result != "MET" or (prev and prev.result == "MET"):
            return False
        # Alerts report the market changing, not the owner changing the rule: a condition whose
        # threshold was just edited doesn't alert on that snapshot (like SIGNAL_CHANGED below).
        if prev and (prev.op, prev.value, prev.metric) != (c.op, c.value, c.metric):
            return False
        # A default warning alerts only on a real change, not when it is first evaluated (newly
        # enabled, or the first snapshot with defaults), so switching one on can't flood 66 alerts.
        return not c.default or prev is not None

    out = []
    for category, (typ, title) in _CONDITION_ALERTS.items():
        new = [c for c in b.conditions if c.category == category and started(c)]
        if not new:
            continue
        severity = "high" if category != "TECHNICAL" else technical_severity(horizon)
        # Stored message keeps the exact metric and value for audit; "what" is the owner's wording,
        # e.g. "“Down more than 20% on cost” (your P&L is -20.14%)".
        message = f"{title}: " + "; ".join(
            f"{c.description}{' (default warning, not your thesis)' if c.default else ''} "
            f"(actual {c.metric} = {c.actual}){_evidence(c)}" for c in new)
        what = f"{title}: " + "; ".join(condition_in_words(c) + _evidence(c) for c in new) + "."
        facts = []
        for c in new:
            facts.append([c.description, _fmt_metric(c.metric, c.actual)])
            if c.inputs:
                i = c.inputs
                facts.append(["Quarters (₹ cr, standalone)",
                              f"{i['latest']['value']:,.0f} ({i['latest']['period_end']}) vs "
                              f"{i['year_ago']['value']:,.0f} ({i['year_ago']['period_end']})"])
        if category == "TECHNICAL":
            headline = "Default warning triggered" if all(c.default for c in new) else "Your technical warning triggered"
            meaning = ("A generic threshold applied to every holding, not your reason for owning it."
                       if all(c.default for c in new) else "A technical warning you set for this holding.")
            action = _price_action(b, "check whether this changes your plan for it.")
        else:
            headline = "Your thesis condition was met" if category == "THESIS" else "Your business condition was met"
            why = b.thesis.why_bought if b.thesis else ""
            meaning = ("You set this as a sign that your reason for holding may no longer apply"
                       + (f" (your reason: “{why}”)." if why else ".")
                       + (" Check both quarters for one-offs such as a demerger or a large gain."
                          if category == "BUSINESS" else ""))
            action = "Marked Review: re-read your thesis on the stock page and confirm or update it."
        out.append(AlertIn(
            symbol=b.symbol, type=typ, severity=severity, message=message,
            details={"category": category, "thesis_version": b.thesis.version if b.thesis else None,
                     "horizon": horizon.value if horizon else None,
                     "conditions": [c.model_dump(mode="json") for c in new],
                     "gate": 4 if category != "TECHNICAL" else None,
                     "explain": _explain(what, meaning, action, headline, facts)}))
    return out


def _holding_alerts(a: HoldingSnapshot, b: HoldingSnapshot, rules_changed: bool) -> list[AlertIn]:
    out: list[AlertIn] = []
    sym = b.symbol
    price_a, price_b = a.portfolio.last_price, b.portfolio.last_price
    ta, tb = a.technical, b.technical

    # Thesis, business and technical conditions that started being met.
    out += _condition_alerts(a, b)

    # Event window opened (gate 5), e.g. results within N days.
    g5 = _fired(b, 5)
    if g5 and not _fired(a, 5):
        what = f"Event approaching: {g5.detail}."
        out.append(AlertIn(symbol=sym, type="EVENT_WINDOW", severity="medium", message=what, details={
            "gate": 5, "explain": _explain(
                what, "Results and major announcements can move the price sharply in either direction.",
                "Marked Review until it has passed: re-read your thesis beforehand.",
                "Results or a major event are coming up", [["Event", g5.detail]])}))

    # Built-in technical detectors (category TECHNICAL, D7). "Below the 200-day average" is now the
    # default warning w_sma200, raised by _condition_alerts, so it can be switched off per holding.
    horizon = b.thesis.horizon if b.thesis else None
    tech = technical_severity(horizon)
    if ta and tb:
        lookback = tb.params.breakout_lookback
        # Levels come from the OLDER snapshot: the level that existed before the move. The price must
        # get LEVEL_CROSS_MARGIN past it (and must not have been that far past it already).
        s = ta.support
        if s is not None and price_a > s * (1 - LEVEL_CROSS_MARGIN) >= price_b:
            what = (f"Price fell below support at {_rs(s)}: now {_rs(price_b)}, {(price_b - s) / s:+.1%} "
                    f"(was {_rs(price_a)}).")
            out.append(AlertIn(symbol=sym, type="SUPPORT_BROKEN", severity=tech, message=what, details={
                "category": "TECHNICAL", "support": s, "from": price_a, "to": price_b,
                "explain": _explain(what, f"Support is the nearest earlier low (last {ta.params.sr_lookback} "
                                          "sessions) where falls had stopped before; it didn't hold this time.",
                                    _price_action(b, "check it against your plan, e.g. the loss you'd accept."),
                                    "Fell below support",
                                    [["Support", _rs(s)], ["Now", f"{_rs(price_b)} ({_pct((price_b - s) / s)})"],
                                     ["Before", _rs(price_a)]])}))
        r = ta.resistance
        if r is not None and price_a < r * (1 + LEVEL_CROSS_MARGIN) <= price_b:
            what = (f"Price rose above resistance at {_rs(r)}: now {_rs(price_b)}, {(price_b - r) / r:+.1%} "
                    f"(was {_rs(price_a)}).")
            out.append(AlertIn(symbol=sym, type="RESISTANCE_CROSSED", severity="info", message=what, details={
                "category": "TECHNICAL", "resistance": r, "from": price_a, "to": price_b,
                "explain": _explain(what, f"Resistance is the nearest earlier peak (last {ta.params.sr_lookback} "
                                          "sessions) where rises had stopped before. Such moves often reverse.",
                                    "No action needed.", "Rose above resistance",
                                    [["Resistance", _rs(r)], ["Now", f"{_rs(price_b)} ({_pct((price_b - r) / r)})"],
                                     ["Before", _rs(price_a)]])}))
    # Heavy-volume breakout/breakdown of the last COMPLETED session (D10), as recorded by the decision,
    # so the alert and the signal always describe the same session.
    ev, ev_a = b.decision.volume_event, a.decision.volume_event
    lookback = tb.params.breakout_lookback if tb else 20
    if ev and (ev_a is None or (ev_a["date"], ev_a["type"]) != (ev["date"], ev["type"])):
        up = ev["type"] == "BREAKOUT"
        drove = b.decision.signal_source == "volume_event"
        state = b.decision.state
        if up:
            severity = "medium" if drove and state.startswith("BUY") else "info"
        else:
            severity = "high" if drove and state.startswith("SELL") else "medium" if drove else tech
        word, side = ("high", "above") if up else ("low", "below")
        what = (f"On {ev['date']} it closed at {_rs(ev['close'])}, {side} its {lookback}-session {word} of "
                f"{_rs(ev['level'])}, on {ev['volume_ratio']:.1f}× the usual volume.")
        g = b.decision.guidance or {}
        effect = {"BUY": "buy signal", "SELL": "sell signal", "REVIEW": "review", "DONT_ADD": "don't add"}
        led_to = next((v for k, v in effect.items() if state.startswith(k)), None) if drove else None
        facts = [["Close", _rs(ev["close"])], [f"Previous {lookback}-day {word}", _rs(ev["level"])],
                 ["Volume", f"{ev['volume_ratio']:.1f}× normal"]]
        if led_to:
            facts.append(["Result", led_to])
        out.append(AlertIn(symbol=sym, type="BREAKOUT" if up else "BREAKDOWN", severity=severity, message=what,
                           details={"category": "TECHNICAL", "close": ev["close"], "volume_ratio": ev["volume_ratio"],
                                    "level": ev["level"], "session": ev["date"], "signal": led_to,
                                    "explain": _explain(
                                        what, g.get("basis") if drove else
                                        f"Strong {'buying' if up else 'selling'}: a momentum signal, not a forecast.",
                                        g.get("action") if drove else
                                        ("No action needed. If you plan to add, check your position limit first."
                                         if up else _price_action(b, "check whether this breaks your plan for it.")),
                                        f"Heavy {'buying' if up else 'selling'}: new {lookback}-day {word}", facts)}))

    # News (D11): a new HIGH-materiality OFFICIAL event cluster. Keyed by cluster id, so a refresh or a
    # republication in the press never repeats it. Information about news itself, raised even in
    # shadow mode; it doesn't change the live signal.
    seen_clusters = {c["cluster_id"] for c in ((a.news or {}).get("clusters") or [])}
    for c in (b.news or {}).get("clusters") or []:
        if not (c["official"] and c["materiality"] == "HIGH") or c["cluster_id"] in seen_clusters:
            continue
        src = next((s for s in c["sources"] if s["official"]), c["sources"][0])
        tone = {2: "clearly positive", 1: "positive", 0: "neutral", -1: "negative", -2: "clearly negative"}[c["sentiment"]]
        what = f"{c['summary'].rstrip('.')} (NSE disclosure, {c['published']}): “{c['evidence'][:200]}”"
        facts = [["Source", src["publisher"]], ["Date", c["published"]], ["Type", c["event_type"].replace("_", " ").lower()],
                 ["Reading", tone]]
        if c.get("duplicates"):
            facts.append(["Also reported by", f"{c['duplicates']} other source(s)"])
        out.append(AlertIn(symbol=sym, type="NEWS_MATERIAL", severity="high" if c["sentiment"] <= -1 else "medium",
                           message=what[:500], details={
            "category": "NEWS", "cluster_id": c["cluster_id"], "url": src.get("url"), "revision": 1,
            "explain": _explain(
                what,
                f"The company disclosed this officially. The app's AI reads it as {tone} and highly material; that "
                "reading is an interpretation, the quoted text is the fact.",
                "Read the disclosure and check it against your reason for holding. Under the news rules being "
                "tested (rules-1.4.0, shadow) it opens a 7-day review window.",
                "Material company disclosure", facts)}))

    # Concentration is a risk limit, not a price move: it stays medium whatever the horizon.
    ra, rb = a.risk, b.risk
    if rb and rb.position_breach and not (ra and ra.position_breach):
        what = (f"This position is now {rb.position_weight:.1%} of your portfolio, above your "
                f"{rb.position_limit:.0%} limit.")
        out.append(AlertIn(symbol=sym, type="CONCENTRATION", severity="medium", message=what, details={
            "category": "TECHNICAL", "weight": rb.position_weight, "limit": rb.position_limit,
            "explain": _explain(what, "One stock now has an outsized effect on the whole portfolio.",
                                "Don't add to it; decide whether this size is intended.",
                                "Position above your size limit",
                                [["Weight", f"{rb.position_weight:.1%}"], ["Limit", f"{rb.position_limit:.0%}"]])}))
    if rb and rb.sector_breach and not (ra and ra.sector_breach):
        what = f"{rb.sector} is now {rb.sector_weight:.1%} of your portfolio, above your {rb.sector_limit:.0%} limit."
        out.append(AlertIn(symbol=sym, type="SECTOR_CONCENTRATION", severity="medium", message=what, details={
            "category": "TECHNICAL", "sector": rb.sector, "weight": rb.sector_weight, "limit": rb.sector_limit,
            "explain": _explain(what, "Holdings in one sector tend to move together, so one bad event can hit "
                                      "several positions at once.",
                                f"Don't add to {rb.sector}; decide whether this exposure is intended.",
                                f"{rb.sector} above your sector limit",
                                [["Weight", f"{rb.sector_weight:.1%}"], ["Limit", f"{rb.sector_limit:.0%}"]])}))

    # Signal change, unless the user caused it by editing the thesis (e.g. bulk drafts).
    # Also skip it when a gate alert above already explains the move (e.g. thesis condition → REVIEW).
    thesis_edited = (a.thesis.version if a.thesis else None) != (b.thesis.version if b.thesis else None)
    explained = (b.decision.state == "REVIEW" and any(
        x.type in ("THESIS_CONDITION", "BUSINESS_CONDITION", "EVENT_WINDOW") for x in out)) or any(
        x.type in ("BREAKOUT", "BREAKDOWN") and x.details.get("signal") for x in out)
    if a.decision.state != b.decision.state and not thesis_edited and not explained:
        note = " (rule version changed too)" if rules_changed else ""
        message = f"Signal {a.decision.state} → {b.decision.state}: {b.decision.summary or b.decision.reason}{note}"
        words = lambda s: s.replace("_", " ").capitalize()  # noqa: E731
        g = b.decision.guidance or {}
        facts = []
        t, vc = b.technical, b.decision.volume_check
        if t and t.sma200:
            facts.append(["vs 200-day average", _pct(t.close / t.sma200 - 1)])
        if t and t.trend:
            facts.append(["Trend", t.trend.lower()])
        if t and t.rsi14 is not None:
            facts.append(["RSI (0–100)", f"{t.rsi14:.0f}"])
        if vc and vc.get("ratio") is not None:
            need = f" (needs {vc['threshold']:.1f}×)" if vc.get("passed") is False else ""
            facts.append(["5-day volume", f"{vc['ratio']:.2f}× normal{need}"])
        meaning = (f"The app's price-based signal changed{note}. " + g.get("basis", "")).strip()
        out.append(AlertIn(symbol=sym, type="SIGNAL_CHANGED", severity="medium", message=message, details={
            "from": a.decision.state, "to": b.decision.state,
            "explain": _explain(b.decision.summary or message, meaning,
                                g.get("action") or "Open the stock page to see what drove the change.",
                                f"{words(a.decision.state)} → {words(b.decision.state)}", facts)}))
    # Horizon as tags on every alert, instead of repeating it in each sentence.
    for x in out:
        x.details.setdefault("horizon", horizon.value if horizon else None)
        x.details["horizon_draft"] = bool(b.thesis and b.thesis.draft)
    return out


def detect_alerts(old: Snapshot, new: Snapshot) -> list[AlertIn]:
    rules_changed = old.versions.get("rules") != new.versions.get("rules")
    alerts: list[AlertIn] = []
    for sym in sorted(set(old.holdings) & set(new.holdings)):
        alerts += _holding_alerts(old.holdings[sym], new.holdings[sym], rules_changed)
    # Sector breaches are per sector, not per holding: keep one alert per sector.
    seen: set[str] = set()
    deduped = []
    for a in alerts:
        if a.type == "SECTOR_CONCENTRATION":
            if a.details["sector"] in seen:
                continue
            seen.add(a.details["sector"])
            a = a.model_copy(update={"symbol": "*"})
        deduped.append(a)
    return deduped

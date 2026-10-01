"""Application layer: REAL DATA → NORMALIZE → ENGINES → SNAPSHOT → GATES → SIGNAL."""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import timedelta
from pathlib import Path

from app.alerts import detect_alerts
from app.broker.base import BrokerAdapter, BrokerError
from app.broker.models import Candle, MarketEvent
from app.clock import Clock
from app.config import Settings
from app.db import Repository
from app.decision import DecisionInputs, RuleSet, decide, live_rule_set, load_validated_rule_versions, shadow_rule_set
from app.news_score import price_since
from app.news_service import NewsService
from app.engines import portfolio as portfolio_engine
from app.engines import risk as risk_engine
from app.engines import technical as technical_engine
from app.snapshot import HoldingSnapshot, Snapshot, canonical_hash
from app.fundamentals_service import FundamentalsService
from app.tech_warnings import WarningSettings, effective_warnings
from app.thesis import Horizon, ThesisIn

WARNINGS_KEY = "technical_warnings"

# Chart-based draft horizons (decisions.md D12): annualised volatility of daily returns over the last
# year. The reasons are the D4 wording, so the suggester knows the app wrote them.
CHART_DRAFT_MIN_SESSIONS = 60
CHART_DRAFT_BANDS = (  # (volatility at or above, horizon, reason)
    (0.45, Horizon.SHORT_TERM, "High-volatility or turnaround position held for a shorter move."),
    (0.30, Horizon.MEDIUM_TERM, "Growth or cyclical theme (capex, manufacturing, consumption, recent listing) "
                                "held for a multi-quarter run."),
    (0.0, Horizon.LONG_TERM, "Core long-term holding: established, quality business held for compounding."),
)


def chart_draft_thesis(candles: list[Candle]) -> ThesisIn | None:
    """A draft thesis for a holding that has none, so its chart-based signal can be shown. The horizon
    comes from how much the price moves; the owner confirms or changes it on the Stock page."""
    closes = [c.close for c in candles[-253:] if c.close > 0]
    if len(closes) < CHART_DRAFT_MIN_SESSIONS:
        return None
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    mean = sum(returns) / len(returns)
    vol = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1) * 252)
    horizon, why = next((h, w) for floor, h, w in CHART_DRAFT_BANDS if vol >= floor)
    return ThesisIn(why_bought=why, horizon=horizon, draft=True,
                    notes=f"Draft set by the app from the price chart: the price moves about {vol:.0%} a year "
                          f"(under 30% long term, 30-45% medium term, 45% or more short term), over "
                          f"{len(closes)} sessions. It is not your reason. Confirm or change it here.")


CANDLE_FAILURE_COOLDOWN = 600  # seconds before a failed symbol's candles are retried


def load_sector_map(path: Path) -> dict[str, str]:
    """Symbol → sector. Keys starting with '_' are comments. Kite series suffixes (e.g. HFCL-BE)
    resolve to the base symbol via SectorMap."""
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return SectorMap({k.upper(): v for k, v in raw.items() if not k.startswith("_")})


class SectorMap(dict):
    def get(self, symbol, default=None):
        if symbol in self:
            return self[symbol]
        base = symbol.rsplit("-", 1)[0] if "-" in symbol else symbol
        return super().get(base, default)


def load_events(path: Path | None) -> list[MarketEvent]:
    if not path or not path.exists():
        return []
    return [MarketEvent.model_validate(e) for e in json.loads(path.read_text(encoding="utf-8"))]


class AnalysisService:
    def __init__(self, broker: BrokerAdapter, repo: Repository, settings: Settings, clock: Clock,
                 rules: RuleSet | None = None, events: list[MarketEvent] | None = None,
                 fundamentals: FundamentalsService | None = None, news: NewsService | None = None):
        self.broker = broker
        self.repo = repo
        self.fundamentals = fundamentals
        self.news = news
        self.settings = settings
        self.clock = clock
        self.rules = rules or live_rule_set(
            max_holdings_age_hours=settings.max_holdings_age_hours,
            max_candle_age_days=settings.max_candle_age_days,
            results_window_days=settings.results_window_days,
        )
        # rules-1.4.0 runs alongside in shadow mode (D11.5): recorded, never shown as the signal.
        self.shadow_rules = shadow_rule_set(
            max_holdings_age_hours=settings.max_holdings_age_hours,
            max_candle_age_days=settings.max_candle_age_days,
            results_window_days=settings.results_window_days,
        ) if news is not None and news.enabled else None
        self.sector_map = load_sector_map(settings.resolved_sector_map_file())
        self.events = events if events is not None else load_events(settings.events_file)
        # Daily candles per (symbol, day). Replaced by the Parquet/DuckDB EOD store later.
        self._candle_cache: dict[tuple[str, str], list[Candle]] = {}
        self.candle_errors: dict[str, str] = {}
        self.candles_blocked: str | None = None
        self._candle_failed_at: dict[str, float] = {}
        # Page loads fire several requests at once; build one snapshot at a time so candle fetches
        # aren't duplicated (and don't trip the broker's rate limit). Re-entrant: a build syncs the
        # account under it, and a login's sync_account waits for a running build instead of switching
        # accounts under it (D12).
        self._build_lock = threading.RLock()
        self._account_synced = False

    def sync_account(self, symbols: set[str] | None = None) -> str:
        """Point the repository at the logged-in broker user: theses, snapshots, alerts and settings are
        kept per account. Runs after each login and before the first snapshot of the process."""
        with self._build_lock:
            self._account_synced = False  # a failed lookup is retried by the next snapshot
            account = self.broker.account_id()
            if account:
                self.repo.set_account(account, self.clock.now())
                if symbols is None:
                    symbols = {h.symbol for h in self.broker.get_holdings()}
                self.repo.claim_legacy(symbols)
            self._account_synced = True
            return self.repo.account

    def candles(self, symbol: str) -> list[Candle]:
        today = self.clock.today_ist()
        key = (symbol, today.isoformat())
        if key not in self._candle_cache:
            failed_at = self._candle_failed_at.get(symbol)
            if failed_at and time.monotonic() - failed_at < CANDLE_FAILURE_COOLDOWN:
                return []  # failed recently; error already recorded in candle_errors
            if self.candles_blocked:
                # Circuit breaker: after one historical failure, stop calling the broker (a failing call can
                # end the Kite MCP session). Missing stays missing: gate 2 reports "candles".
                self.candle_errors[symbol] = f"skipped: {self.candles_blocked}"
                return []
            start = today - timedelta(days=self.settings.candle_lookback_days)
            self._candle_cache = {k: v for k, v in self._candle_cache.items() if k[1] == key[1]}
            try:
                self._candle_cache[key] = self.broker.get_historical(symbol, start, today)
            except BrokerError as e:
                self.candle_errors[symbol] = str(e)
                self._candle_failed_at[symbol] = time.monotonic()
                # Keep going for other symbols only if the broker session is still healthy.
                try:
                    self.broker.get_margins()
                except BrokerError as health:
                    self.candles_blocked = f"broker session unhealthy after {symbol} failed: {health}"
                return []
            self.candle_errors.pop(symbol, None)
        return self._candle_cache[key]

    def retry_candles(self) -> None:
        self.candles_blocked = None
        self.candle_errors.clear()
        self._candle_failed_at.clear()

    def warning_settings(self) -> WarningSettings:
        return WarningSettings.model_validate(self.repo.get_setting(WARNINGS_KEY) or {})

    def save_warning_settings(self, settings: WarningSettings) -> None:
        self.repo.put_setting(WARNINGS_KEY, settings.model_dump(mode="json", exclude_none=True), self.clock.now())

    def build_snapshot(self) -> Snapshot:
        now = self.clock.now()
        today = self.clock.today_ist()
        holdings = self.broker.get_holdings()
        if not self._account_synced:
            self.sync_account({h.symbol for h in holdings})
        trades = self.repo.get_trades()
        theses = self.repo.latest_theses()
        warning_settings = self.warning_settings()
        # Stored fetches only: a snapshot never calls the fundamentals provider.
        fundamentals = self.fundamentals.views() if self.fundamentals else {}
        news_views = self.news.views(today, now) if self.news and self.news.enabled else {}
        validated = load_validated_rule_versions(self.settings.resolved_validated_rules_file())

        report = portfolio_engine.compute_portfolio(holdings, trades, self.sector_map, today)
        risk = risk_engine.compute_risk(report, self.settings.max_position_weight, self.settings.max_sector_weight)
        by_symbol = {h.symbol: h for h in holdings}

        candle_digest: dict[str, str | None] = {}
        snaps: dict[str, HoldingSnapshot] = {}
        for hr in report.holdings:
            h = by_symbol[hr.symbol]
            candles = self.candles(hr.symbol)
            candle_digest[hr.symbol] = canonical_hash([c.model_dump(mode="json") for c in candles]) if candles else None
            tech = technical_engine.compute_technicals(candles)
            thesis = theses.get(hr.symbol)
            if thesis is None and (draft := chart_draft_thesis(candles)):
                thesis = theses[hr.symbol] = self.repo.save_thesis(hr.symbol, draft, now)
            nv = news_views.get(hr.symbol)
            if nv:  # display only: how the price moved since each news event (D11, never "caused")
                nv = {**nv, "clusters": [{**c, "price_since": price_since(candles, c["published"])}
                                         for c in nv["clusters"]]}
            events = [e for e in self.events if e.symbol == hr.symbol] +                 [MarketEvent.model_validate(e) for e in (nv or {}).get("events", [])]
            inp = (
                DecisionInputs(
                    symbol=hr.symbol, now=now, today=today, holding=hr, holding_as_of=h.as_of,
                    technical=tech, thesis=thesis, risk=risk.holdings.get(hr.symbol), events=events,
                    tax_impact=portfolio_engine.estimated_tax_if_sold(
                        hr, self.settings.stcg_rate, self.settings.ltcg_rate),
                    warnings=effective_warnings(warning_settings, hr.symbol),
                    fundamental=fundamentals.get(hr.symbol),
                    news=nv,
                )
            )
            out = decide(inp, self.rules, validated)
            shadow = None
            if self.shadow_rules:
                sd = decide(inp, self.shadow_rules, validated).decision
                diag = sd.news_diagnostics or {"impact": "BLOCKED" if nv and nv.get("score") is not None
                                               else "NO_IMPACT"}
                shadow = {"rule_version": sd.rule_version, "state": sd.state, "score": sd.score,
                          "summary": sd.summary, "reason": sd.reason, "diagnostics": diag}
            hs = HoldingSnapshot(
                symbol=hr.symbol,
                data_as_of={"holdings": h.as_of.isoformat(), "candles": tech.as_of if tech else None,
                            "thesis": thesis.updated_at.isoformat() if thesis else None},
                portfolio=hr,
                market={"last_price": h.last_price, "close_price": h.close_price},
                technical=tech,
                fundamental=fundamentals.get(hr.symbol),
                news=nv,
                shadow=shadow,
                thesis=thesis,
                risk=risk.holdings.get(hr.symbol),
                events=events,
                gates=out.gates,
                component_scores=out.component_scores,
                decision=out.decision,
                conditions=out.conditions,
            )
            hs.content_hash = hs.compute_content_hash()
            snaps[hr.symbol] = hs

        versions = {
            "portfolio_engine": portfolio_engine.ENGINE_VERSION,
            "technical_engine": technical_engine.ENGINE_VERSION,
            "risk_engine": risk_engine.ENGINE_VERSION,
            "rules": self.rules.version,
        }
        input_hash = canonical_hash({
            "holdings": [h.model_dump(mode="json", exclude={"as_of"}) for h in holdings],
            "trades": sorted((t.model_dump(mode="json") for t in trades), key=lambda t: (t["trade_date"], t["trade_id"])),
            "candles": candle_digest,
            "theses": {s: t.version for s, t in theses.items()},
            "warning_settings": warning_settings.model_dump(mode="json"),
            "fundamentals": {s: (v["status"], v["fetched_at"]) for s, v in fundamentals.items()},
            "news": {s: (v["status"], v["fetched_at"], v.get("score"), len(v.get("clusters", [])),
                         len(v.get("events", []))) for s, v in news_views.items()},
            "events": [e.model_dump(mode="json") for e in self.events],
            "sector_map": self.sector_map,
            "rules": self.rules.model_dump(mode="json"),
            "limits": [self.settings.max_position_weight, self.settings.max_sector_weight],
            "versions": versions,
            "today": today.isoformat(),
        })
        return Snapshot(
            created_at=now,
            data_as_of={"holdings": max((h.as_of for h in holdings), default=now).isoformat(),
                        "candles": max((s.technical.as_of for s in snaps.values() if s.technical), default=None)},
            versions=versions,
            rule_set_validated=self.rules.version in validated,
            portfolio_summary=report.model_dump(mode="json", exclude={"holdings"}),
            risk=risk,
            holdings=snaps,
            input_hash=input_hash,
        )

    def _save_with_alerts(self, fresh: Snapshot, previous: Snapshot | None) -> Snapshot:
        saved = self.repo.save_snapshot(fresh)
        if previous is not None:
            alerts = detect_alerts(previous, saved)
            if alerts:
                self.repo.save_alerts(alerts, saved.id, previous.id, self.clock.now())
        return saved

    def take_snapshot(self) -> Snapshot:
        with self._build_lock:
            latest = self.repo.latest_snapshots(1)
            return self._save_with_alerts(self.build_snapshot(), latest[0] if latest else None)

    def current(self) -> Snapshot:
        """Latest stored snapshot if its inputs are unchanged, otherwise a fresh stored one.
        Every newly stored snapshot is compared with the previous one to raise alerts."""
        with self._build_lock:
            fresh = self.build_snapshot()
            latest = self.repo.latest_snapshots(1)
            if latest and latest[0].input_hash == fresh.input_hash:
                return latest[0]
            return self._save_with_alerts(fresh, latest[0] if latest else None)


ATTENTION_STATES = {"REVIEW", "DONT_ADD", "SELL_SIGNAL", "SELL_CANDIDATE", "BUY_SIGNAL", "BUY_CANDIDATE"}


def attention_list(snap: Snapshot) -> list[dict]:
    """Holdings needing attention: REVIEW/BUY/SELL states, missing theses, stale data."""
    out = []
    for s in snap.holdings.values():
        d = s.decision
        if d.state in ATTENTION_STATES or d.reason.startswith(("Thesis missing", "Stale", "Required data")):
            out.append({"symbol": s.symbol, "state": d.state, "reason": d.reason, "summary": d.summary or d.reason,
                        "horizon": s.thesis.horizon.value if s.thesis and s.thesis.horizon else None,
                        "thesis_status": d.thesis_status, "weight": s.portfolio.weight})
    order = {"REVIEW": 0, "SELL_CANDIDATE": 1, "SELL_SIGNAL": 1, "DONT_ADD": 2}
    return sorted(out, key=lambda x: (order.get(x["state"], 3), x["symbol"]))


def short_why(hs) -> str:
    """A few words for one holding, for lists: the reason without caveats or boilerplate."""

    d = hs.decision
    if d.reason.startswith("Stale"):
        return "data out of date"
    if d.reason.startswith("Required data"):
        return "no price history" if "candles" in d.reason else "not enough price history yet"
    if d.reason.startswith(("Thesis missing", "Thesis has no horizon")):
        return "no thesis recorded"
    ev = d.volume_event
    if d.signal_source == "volume_event" and ev:
        up = ev["type"] == "BREAKOUT"
        return (f"heavy {'buying' if up else 'selling'}: new 20-day {'high' if up else 'low'} on "
                f"{ev['volume_ratio']:.1f}× volume")
    if d.state == "REVIEW":
        return d.summary.split(": ", 1)[-1].rstrip(".") if d.summary else d.reason
    if d.volume_check and d.volume_check.get("passed") is False and d.volume_check.get("ratio") is not None:
        return f"positive price picture, but volume only {d.volume_check['ratio']:.2f}× normal"
    return _drivers_short(hs.component_scores)


def _drivers_short(c: dict) -> str:
    """Compact price picture for lists: 'below 200-day avg, downtrend, weak momentum'."""
    long_trend = {1.0: "above 200-day avg", -1.0: "below 200-day avg"}.get(c.get("long_trend"))
    trend = {1.0: "uptrend", 0.0: "no clear trend", -1.0: "downtrend"}.get(c.get("trend"))
    m = c.get("momentum")
    momentum = None if m is None else "positive momentum" if m >= 0.25 else "weak momentum" if m <= -0.25 \
        else "neutral momentum"
    return ", ".join(x for x in (long_trend, trend, momentum) if x)


def _long_term(i: dict) -> bool:
    return i["horizon"] == "LONG_TERM"


_SELL = ("SELL_SIGNAL", "SELL_CANDIDATE")
# (key, title, member test). Sell signals are split by horizon: for a long-term holding a price-based
# sell is "price weakness, not a reason to sell on its own" (D1.2), as the Stock page says.
ATTENTION_GROUPS = [
    ("review", "Review your thesis", lambda i: i["state"] == "REVIEW"),
    ("sell_signals", "Sell signals (short and medium term)", lambda i: i["state"] in _SELL and not _long_term(i)),
    ("price_weakness", "Price weakness on long-term holdings (not a reason to sell on its own)",
     lambda i: i["state"] in _SELL and _long_term(i)),
    ("dont_add", "Over a concentration limit", lambda i: i["state"] == "DONT_ADD"),
    ("buy_signals", "Buy signals", lambda i: i["state"] in ("BUY_SIGNAL", "BUY_CANDIDATE")),
    ("no_signal", "No signal yet", lambda i: i["state"] == "NO_ACTION"),
]


def attention_digest(snap: Snapshot, top: int = 5) -> dict:
    """Attention items grouped and ranked by portfolio weight, with shared caveats stated once.
    Built for summaries: the largest positions first, the rest as counts."""
    items = attention_list(snap)
    by_symbol = snap.holdings
    groups = []
    for key, title, member in ATTENTION_GROUPS:
        members = [i for i in items if member(i)]
        members.sort(key=lambda i: -i["weight"])
        if not members:
            continue
        groups.append({
            "group": key, "title": title, "count": len(members),
            "total_weight": round(sum(i["weight"] for i in members), 4),
            "largest": [{"symbol": i["symbol"], "weight": i["weight"], "why": short_why(by_symbol[i["symbol"]]),
                         "thesis": i["thesis_status"]} for i in members[:top]],
            "others": [i["symbol"] for i in members[top:]],
            "others_count": len(members[top:]),
        })
    also_over_limit = sorted((i for i in items if i["state"] != "DONT_ADD"
                              and by_symbol[i["symbol"]].decision.dont_add), key=lambda i: -i["weight"])
    drafts = sum(1 for s in by_symbol.values() if s.decision.thesis_status == "draft")
    with_fundamentals = sum(1 for s in by_symbol.values() if s.fundamental and s.fundamental.get("status") == "OK")
    return {
        # Totals are given so the model never has to add them up (it must not compute numbers).
        "total": {"holdings_needing_attention": sum(g["count"] for g in groups),
                  "weight": round(sum(g["total_weight"] for g in groups), 4)},
        "groups": groups,
        "also_over_limit": [{"symbol": i["symbol"], "weight": i["weight"]} for i in also_over_limit],
        "shared_caveats": {
            "rules_validated": snap.rule_set_validated,
            "rule_version": snap.versions.get("rules"),
            # Draft horizons were assigned by the app from each stock's profile (D4), not by the owner.
            "draft_horizons_set_by_app": drafts,
            "holdings": len(by_symbol),
            "holdings_with_reported_fundamentals": with_fundamentals,
            "evidence_unavailable": [] if any(s.news and s.news.get("status") in ("SUCCESS", "PARTIAL")
                                              for s in by_symbol.values()) else ["news"],
            "news_in_live_signal": False,  # D11.5: rules-1.4.0 runs in shadow mode
        },
    }

"""Scanner (decisions.md D14): the live rules run over an index universe, for stocks the owner may not hold.

Same code path as a holding: compute_technicals -> decide() with the live rule set, every gate except
"holding required". The horizon is the chart-based draft of D12 (volatility bands). Prices come from the
local EOD store (backtest history, Nifty 500); "refresh prices" tops it up from Kite with one request
per stock. Results are prompts to look, not advice: the rule set hasn't passed back-testing.
"""

from __future__ import annotations

import threading
from datetime import timedelta

from app.backtest import data as store
from app.broker.base import BrokerError
from app.clock import IST
from app.decision import MARKET_CLOSE_IST, DecisionInputs, decide, load_validated_rule_versions
from app.engines import technical as technical_engine
from app.thesis import Thesis

SCAN_SESSIONS = 400  # enough for SMA200, the 52-week range and the one-year volatility band
TOP_UP_DAYS = 10     # a refresh asks Kite for the last 10 days only, then merges


REBASE_TOLERANCE = 0.01  # stored vs fetched close on the same day; more than 1% apart = re-adjusted history


def _rebased(have: list, new: list) -> bool:
    """True when the fetched candles disagree with the stored ones on days both have (before the
    newest stored day, which may have been a part-day candle)."""
    stored = {c.date: c.close for c in have[:-1]}
    return any(abs(c.close / stored[c.date] - 1) > REBASE_TOLERANCE
               for c in new if c.date in stored and stored[c.date])


class Scanner:
    def __init__(self, service, repo, clock):
        self.service, self.repo, self.clock = service, repo, clock
        self.state: dict = {"running": False, "phase": None, "done": 0, "total": 0, "error": None, "failed": {}}
        self.result: dict | None = None
        self._lock = threading.Lock()

    # --- jobs ---
    def start(self, refresh_prices: bool = False) -> dict:
        members = (store.read_manifest() or {}).get("members") or []
        if not members:
            raise ValueError("No stock list yet: download history on the Backtest page first (Nifty 500).")
        if refresh_prices:
            try:
                self.service.broker.get_margins()  # one cheap call: fail now, not 500 times
            except BrokerError as e:
                raise ValueError(f"Log in to Kite first to refresh prices ({e})") from e
        with self._lock:
            if self.state["running"]:
                return self.status()
            self.state = {"running": True, "phase": "prices" if refresh_prices else "scan", "done": 0,
                          "total": len(members), "error": None, "failed": {}}
        threading.Thread(target=self._run, args=(members, refresh_prices), daemon=True, name="scanner").start()
        return self.status()

    def _run(self, members: list[dict], refresh_prices: bool) -> None:
        try:
            if refresh_prices:
                self._top_up([m["symbol"] for m in members])
                self.state.update(phase="scan", done=0)
            self.result = self._scan(members)
        except Exception as e:  # noqa: BLE001 - shown on the page
            self.state["error"] = str(e)[:300]
        finally:
            self.state["running"] = False

    def _top_up(self, symbols: list[str]) -> None:
        today = self.clock.today_ist()
        # Today's candle is stored only once the session has closed: a mid-morning candle saved as
        # "today" would later be read as the finished session (D9.2, D10.2).
        session_closed = self.clock.now().astimezone(IST).time() >= MARKET_CLOSE_IST
        for sym in symbols:
            try:
                have = [c for c in store.load_candles(sym) if c.date < today or session_closed]
                last = have[-1].date if have else None
                start = (last - timedelta(days=TOP_UP_DAYS)) if last else today - timedelta(days=SCAN_SESSIONS * 2)
                new = [c for c in self.service.broker.get_historical(sym, start, today)
                       if c.date < today or session_closed]
                if new and have and _rebased(have, new):
                    # Kite adjusts all history for a split or bonus when it is fetched: the stored older
                    # candles are now on another scale, so mixing them would fake a crash (a false SELL).
                    new = [c for c in self.service.broker.get_historical(sym, have[0].date, today)
                           if c.date < today or session_closed]
                    have = []
                if new:
                    merged = {c.date: c for c in have} | {c.date: c for c in new}
                    store.save_candles(sym, [merged[d] for d in sorted(merged)])
            except BrokerError as e:
                self.state["failed"][sym] = str(e)[:160]
                succeeded = self.state["done"] - len(self.state["failed"]) + 1
                if len(self.state["failed"]) >= 5 and succeeded <= 0:  # the first five all failed
                    raise RuntimeError("Kite stopped answering; log in again and retry") from e
            finally:
                self.state["done"] += 1

    def _scan(self, members: list[dict]) -> dict:
        from app.services import chart_draft_thesis

        s = self.service
        now, today = self.clock.now(), self.clock.today_ist()
        validated = load_validated_rule_versions(s.settings.resolved_validated_rules_file())
        rows, newest = [], None
        for m in members:
            self.state["done"] += 1
            sym = m["symbol"]
            candles = store.load_candles(sym)[-SCAN_SESSIONS:]
            tech = technical_engine.compute_technicals(candles)
            draft = chart_draft_thesis(candles)
            thesis = Thesis(symbol=sym, version=0, updated_at=now, **draft.model_dump()) if draft else None
            out = decide(DecisionInputs(symbol=sym, now=now, today=today, holding=None, holding_as_of=None,
                                        technical=tech, thesis=thesis, risk=None,
                                        events=[e for e in s.events if e.symbol == sym], scan=True),
                         s.rules, validated)
            d = out.decision
            fired = {g.gate for g in out.gates if g.status == "FIRED"}
            summary = d.summary
            if 3 in fired or (2 in fired and tech is not None):
                summary = "No signal: too little price history (listed recently)."
            prev = candles[-2].close if len(candles) >= 2 else None
            newest = max(newest, tech.as_of) if newest and tech else (tech.as_of if tech else newest)
            rows.append({
                "symbol": sym, "company": m.get("company") or sym, "industry": m.get("industry") or "",
                "state": d.state, "score": d.score, "summary": summary,
                "evidence": (d.guidance or {}).get("evidence", ""), "horizon": thesis.horizon if thesis else None,
                "signal_source": d.signal_source, "volume_event": d.volume_event,
                "volume_passed": (d.volume_check or {}).get("passed"),
                "close": tech.close if tech else None,
                "day_change_pct": round(tech.close / prev - 1, 4) if tech and prev else None,
                "trend": tech.trend if tech else None, "rsi14": tech.rsi14 if tech else None,
                "pct_from_52w_high": tech.pct_from_52w_high if tech else None,
                "as_of": tech.as_of if tech else None,
            })
        return {"scanned_at": now.isoformat(), "prices_as_of": newest, "rules": s.rules.version,
                "rule_set_validated": s.rules.version in validated,
                "universe": (store.read_manifest() or {}).get("universe"), "rows": rows}

    # --- read ---
    def status(self) -> dict:
        st = {k: v for k, v in self.state.items() if k != "failed"}
        return {**st, "failed": len(self.state["failed"])}

    def view(self, held: set[str], telegram: dict | None = None) -> dict:
        """Scan rows for the page, each with what the owner's Telegram channels said about the stock.
        Telegram is shown beside the chart signal; it never changes the state or the score (D13)."""
        if self.result is None and not self.state["running"] and (store.read_manifest() or {}).get("members"):
            self.start()  # first visit: scan the stored prices
        res = self.result or {"rows": []}
        base = {h.split("-")[0] for h in held}
        said = (telegram or {}).get("by_symbol", {})
        rows, scanned = [], set()
        for r in res["rows"]:
            scanned.add(r["symbol"])
            is_held = r["symbol"] in held or r["symbol"] in base
            summary = r["summary"]
            if not is_held and r["state"].startswith("SELL"):
                # Not owned: the sell rules mean "weak chart, not a buy", there is nothing to sell.
                why = summary.split(": ", 1)[-1].replace(" Check it against your plan.", "").rstrip(".")
                summary = f"Weak chart, not a buy now: {why}."
            tg = _telegram_summary(said.get(r["symbol"], []))
            rows.append({**r, "held": is_held, "summary": summary, "telegram": tg,
                         "agreement": _agreement(r["state"], tg)})
        outside = [{"symbol": s, "name": items[0]["name"], "held": s in held or s.split("-")[0] in base,
                    "telegram": _telegram_summary(items)}
                   for s, items in said.items() if s not in scanned]
        outside.sort(key=lambda x: x["telegram"]["latest"]["date"] or "", reverse=True)
        return {**res, "rows": rows, "job": self.status(), "telegram_outside": outside,
                "telegram": {k: v for k, v in (telegram or {}).items() if k != "by_symbol"}}


def _telegram_summary(items: list[dict]) -> dict | None:
    if not items:
        return None
    count = lambda v: sum(1 for m in items if m["view"] == v)  # noqa: E731
    return {"mentions": len(items), "buy": count("BUY"), "sell": count("SELL"), "news": count(None),
            "latest": items[0], "items": items[:5]}


def _agreement(state: str, tg: dict | None) -> str | None:
    """AGREE: the chart and at least one post point the same way (and none the other way);
    CONFLICT: they point opposite ways. News-only mentions (grey) neither agree nor conflict."""
    if not tg or not (tg["buy"] or tg["sell"]):
        return None
    chart = "BUY" if state.startswith("BUY") else "SELL" if state.startswith("SELL") else None
    posts = "BUY" if tg["buy"] and not tg["sell"] else "SELL" if tg["sell"] and not tg["buy"] else "MIXED"
    if chart is None or posts == "MIXED":
        return None
    return "AGREE" if chart == posts else "CONFLICT"

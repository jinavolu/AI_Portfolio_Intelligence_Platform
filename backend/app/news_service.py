"""Fetching, verifying and rating news off the snapshot path (decisions.md D11).

A snapshot never calls a provider: it reads stored items, ratings and statuses, and the score is
computed deterministically at snapshot time (app/news_score.py).
"""

from __future__ import annotations

import csv
import logging
import threading
import time as _time
from datetime import date, datetime, time, timedelta

from app.ai.llm import LLMUnavailable
from app.ai.news_rater import PROMPT_VERSION, NewsRater
from app.clock import IST, Clock
from app.config import BACKEND_DIR, Settings
from app.db import Repository
from app.fundamentals import ProviderError, base_symbol, load_aliases
from app.news import NewsItem, NewsRating, NseClient, next_results_date, official_events
from app.news_score import score_news

log = logging.getLogger("pi.news")


def build_nse_client(settings: Settings, clock: Clock):
    name = settings.news_provider or ("none" if settings.broker == "fixture" else "nse")
    if name == "none":
        return None
    try:
        return NseClient(load_aliases(BACKEND_DIR / "fundamentals_aliases.json"), clock=clock.now)
    except ImportError:
        log.warning("news disabled: install the optional extra with pip install -e '.[fundamentals]'")
        return None


def _instrument_names() -> dict[str, str]:
    path = BACKEND_DIR / "data" / "instruments_NSE.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {r["tradingsymbol"].upper(): r["name"] for r in csv.DictReader(f) if r.get("name")}


class NewsService:
    def __init__(self, nse: NseClient | None, rater: NewsRater, repo: Repository, settings: Settings, clock: Clock):
        self.nse, self.rater, self.repo, self.settings, self.clock = nse, rater, repo, settings, clock
        self._lock = threading.Lock()
        self.running = False
        self.progress: dict = {}
        self._names: dict[str, str] | None = None
        # Circuit breaker: once every Gemini model is over quota (or busy), stop calling it until it can
        # work again, instead of failing 4 models x every remaining holding.
        self.gemini_paused_until: datetime | None = None
        self.gemini_pause_reason = ""

    def _gemini_ok(self) -> bool:
        return self.gemini_paused_until is None or self.clock.now() >= self.gemini_paused_until

    def _pause_gemini(self, err: Exception) -> None:
        now = self.clock.now()
        text = str(err)
        if "RESOURCE_EXHAUSTED" in text or "429" in text:
            # Gemini's daily quota resets at midnight US Pacific (12:30-13:30 IST); 13:30 IST is safe.
            ist = now.astimezone(IST)
            reset = datetime.combine(ist.date(), time(13, 30), tzinfo=IST)
            self.gemini_paused_until = reset if ist < reset else reset + timedelta(days=1)
            self.gemini_pause_reason = "Gemini's daily quota is used up"
        else:
            self.gemini_paused_until = now + timedelta(minutes=15)
            self.gemini_pause_reason = "Gemini is busy"
        log.warning("Gemini paused until %s: %s", self.gemini_paused_until.astimezone(IST).strftime("%d %b %H:%M IST"),
                    self.gemini_pause_reason)

    def _paused_note(self, what: str) -> str:
        until = self.gemini_paused_until.astimezone(IST).strftime("%d %b %H:%M IST")
        return f"{what} skipped: {self.gemini_pause_reason}; retrying after {until}"

    @property
    def enabled(self) -> bool:
        return self.nse is not None

    def names(self, symbol: str, items: list[NewsItem]) -> list[str]:
        """Company names to recognise in articles: NSE's own name, the instrument name, the symbol."""
        if self._names is None:
            self._names = _instrument_names()
        out = []
        base = base_symbol(symbol)
        inst = self._names.get(base)
        for n in (inst, base):
            if n:
                out.append(n)
                short = n.replace(" LIMITED", "").replace(" LTD", "").strip()
                if short and short != n:
                    out.append(short)
        return list(dict.fromkeys(out))

    # ------------------------------------------------------------------ refresh
    def refresh_one(self, symbol: str, exchange: str = "NSE", press: bool = True, press_note: str = "") -> str:
        now = self.clock.now()
        today = now.astimezone(IST).date()
        try:
            official = self._with_retry(lambda: self.nse.fetch(symbol))
        except ProviderError as e:
            self.repo.save_news_status(symbol, "FAILED", e.detail, now)
            return "FAILED"
        if not official and exchange == "BSE":
            self.repo.save_news_status(symbol, "NOT_COVERED", "held on BSE; no NSE disclosures found", now)
            return "NOT_COVERED"
        names = self.names(symbol, official)
        press_items, notes = [], [press_note] if press_note else []
        if press and self.settings.news_press and self.rater.enabled:
            if not self._gemini_ok():
                notes.append(self._paused_note("press search"))
            else:
                try:
                    found = self.rater.discover(symbol, names, today)
                    known = {d["item_id"] for d in self.repo.news_items(symbol)}
                    for it in found:
                        v = self.rater.verify(it, names)
                        if v.item_id not in known:
                            press_items.append(v)
                except LLMUnavailable as e:
                    self._pause_gemini(e)
                    notes.append(self._paused_note("press search"))
        self.repo.upsert_news_items([i.model_dump(mode="json") for i in official + press_items], now)
        rated = self.repo.news_ratings(PROMPT_VERSION)
        window_start = today - timedelta(days=8)
        unrated = [NewsItem.model_validate(d) for d in self.repo.news_items(symbol)
                   if d["item_id"] not in rated and (d.get("published_at") or "")[:10] >= window_start.isoformat()]
        status = "SUCCESS"
        if unrated and not self._gemini_ok():
            status = "PARTIAL"
            notes.append(self._paused_note("rating") + " (items shown unrated)")
        elif unrated:
            try:
                ratings, dropped = self.rater.rate(symbol, names[0] if names else symbol, unrated)
                self.repo.save_news_ratings([r.model_dump(mode="json") for r in ratings])
                if dropped:
                    notes.append(f"{len(dropped)} rating(s) rejected")
                if not self.rater.enabled:
                    status, notes = "PARTIAL", notes + ["no Gemini key: official items shown unrated"]
            except LLMUnavailable as e:
                self._pause_gemini(e)
                status = "PARTIAL"
                notes.append(self._paused_note("rating") + " (items shown unrated)")
        if notes and status == "SUCCESS" and any("skipped" in n for n in notes):
            status = "PARTIAL"
        self.repo.save_news_status(symbol, status, "; ".join(notes), now)
        return status

    def _with_retry(self, fn, attempts: int = 3):
        for i in range(attempts):
            try:
                return fn()
            except ProviderError:
                if i == attempts - 1:
                    raise
                _time.sleep(2 ** i)

    def refresh(self, holdings: list[tuple[str, str]], press: bool = True) -> dict[str, int]:
        """holdings: [(symbol, exchange)], largest position first. One at a time; a second call while
        running is refused. Press search only for the first `news_press_top_n` (Gemini quota)."""
        if not self._lock.acquire(blocking=False):
            return {"already_running": 1}
        self.running, counts = True, {}
        self.progress = {"total": len(holdings), "done": 0, "started_at": self.clock.now().isoformat(),
                         "press": press}
        try:
            top_n = self.settings.news_press_top_n
            for i, (sym, exch) in enumerate(holdings):
                searched = press and i < top_n
                note = (f"press search limited to your {top_n} largest holdings (official NSE news only)"
                        if press and not searched else "")
                try:
                    st = self.refresh_one(sym, exch, searched, note)
                except Exception as e:  # noqa: BLE001 - one bad symbol must not stop the rest
                    log.exception("news refresh failed for %s", sym)
                    self.repo.save_news_status(sym, "FAILED", str(e), self.clock.now())
                    st = "FAILED"
                counts[st] = counts.get(st, 0) + 1
                self.progress["done"] += 1
            self.progress |= {"finished_at": self.clock.now().isoformat(), "counts": counts}
            return counts
        finally:
            self.running = False
            self._lock.release()

    def refresh_in_background(self, holdings: list[tuple[str, str]], press: bool = True) -> bool:
        if self.running:
            return False
        threading.Thread(target=self.refresh, args=(holdings, press), daemon=True, name="news").start()
        return True

    def last_fetch(self) -> datetime | None:
        return max((r["fetched_at"] for r in self.repo.news_statuses().values()), default=None)

    # ------------------------------------------------------------------ snapshot inputs
    def views(self, today: date, now: datetime) -> dict[str, dict]:
        """Per symbol: status (with STALE derived), items, ratings, events, the deterministic score and the
        next results date. Pure reads from storage."""
        statuses = self.repo.news_statuses()
        ratings = {k: NewsRating.model_validate(v) for k, v in self.repo.news_ratings(PROMPT_VERSION).items()}
        by_symbol: dict[str, list[NewsItem]] = {}
        for d in self.repo.news_items():
            by_symbol.setdefault(d["symbol"], []).append(NewsItem.model_validate(d))
        out = {}
        for sym, st in statuses.items():
            items = by_symbol.get(sym, [])
            age_h = (now - st["fetched_at"]).total_seconds() / 3600
            status = "STALE" if st["status"] in ("SUCCESS", "PARTIAL") and age_h > self.settings.news_max_age_hours \
                else st["status"]
            sym_ratings = {i.item_id: ratings[i.item_id] for i in items if i.item_id in ratings}
            # Clusters always come from what is stored, so a failed or stale fetch doesn't make them vanish
            # (the next good fetch would then re-alert every one of them). Only a good fetch gives a score.
            scored = score_news(items, sym_ratings, today)
            if status not in ("SUCCESS", "PARTIAL"):
                scored |= {"score": None, "confidence": None}
            nr = next_results_date(items, today)
            out[sym] = {
                "status": status, "detail": st["detail"], "fetched_at": st["fetched_at"].isoformat(),
                "prompt_version": PROMPT_VERSION, **scored,
                "events": [e.model_dump(mode="json") for e in official_events(items, sym_ratings)]
                if status not in ("FAILED", "NOT_COVERED", "DISABLED") else [],
                "next_results": nr.isoformat() if nr else None,
                "recent": [{"item_id": i.item_id, "title": i.title, "publisher": i.publisher, "url": i.source_url,
                            "published": i.published_at.astimezone(IST).date().isoformat() if i.published_at else None,
                            "official": i.verification == "OFFICIAL", "verification": i.verification,
                            "rating": sym_ratings[i.item_id].model_dump(mode="json") if i.item_id in sym_ratings else None}
                           for i in sorted(items, key=lambda i: i.published_at or datetime.min.replace(tzinfo=IST),
                                           reverse=True)
                           if i.source_type in ("NSE_ANNOUNCEMENT", "PRESS")][:20],
            }
        return out

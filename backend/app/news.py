"""News records and the official NSE provider (decisions.md D11).

Two pathways share these records: the RISK pathway turns official disclosures into gate-5 events
(never from press, never from sentiment); the SCORING pathway (app/news_score.py) turns validated,
rated items into the news component. News content is untrusted data (D11.6).
"""

from __future__ import annotations

import hashlib
import time as _time
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from app.broker.models import MarketEvent
from app.clock import IST
from app.fundamentals import ProviderError, base_symbol

SourceType = Literal["NSE_ANNOUNCEMENT", "NSE_BOARD_MEETING", "NSE_CORP_ACTION", "PRESS"]
Verification = Literal["OFFICIAL", "VERIFIED", "UNVERIFIED_CONTENT"]
Materiality = Literal["HIGH", "MEDIUM", "LOW"]
Relevance = Literal["DIRECT_COMPANY", "SUBSIDIARY", "PROMOTER", "CUSTOMER", "COMPETITOR", "SECTOR", "UNRELATED"]
Confirmation = Literal["CONFIRMED", "REPORTED", "SPECULATIVE", "DENIED"]
EventType = Literal["RESULTS", "RATING_CHANGE", "KMP_OR_AUDITOR_EXIT", "PLEDGE_OR_DEFAULT",
                    "FRAUD_OR_INVESTIGATION", "REGULATORY_ACTION", "MERGER_ACQUISITION", "LARGE_ORDER",
                    "CAPITAL_RAISE", "DIVIDEND_OR_CORPORATE_ACTION", "MANAGEMENT_COMMENTARY",
                    "OTHER_MATERIAL", "PRICE_MOVE", "ROUTINE"]
FetchStatus = Literal["SUCCESS", "PARTIAL", "FAILED", "DISABLED", "STALE", "NOT_COVERED"]

# D11.1: event types an official HIGH-materiality item turns into a MAJOR_EVENT.
MATERIAL_EVENT_TYPES = {"RATING_CHANGE", "KMP_OR_AUDITOR_EXIT", "PLEDGE_OR_DEFAULT", "FRAUD_OR_INVESTIGATION",
                        "REGULATORY_ACTION", "MERGER_ACQUISITION", "LARGE_ORDER", "OTHER_MATERIAL"}

# D11.1: review windows per event, in days before / after the event date.
EVENT_WINDOWS = {
    "RESULTS": {"before": 7, "after": 0},            # board meeting to consider results
    "RESULTS_ANNOUNCED": {"before": 0, "after": 2},
    "MAJOR_EVENT": {"before": 0, "after": 7},
}


class NewsItem(BaseModel):
    """One source record. `item_id` is stable across refreshes, so nothing is processed twice."""
    item_id: str
    symbol: str
    source_type: SourceType
    source_url: str | None = None
    publisher: str
    title: str
    text: str = ""  # official summary, or the verified page excerpt for press
    published_at: datetime | None = None  # when it was disclosed / published
    event_date: date | None = None  # when the event takes place (board meeting, ex-date)
    effective_date: date | None = None
    fetched_at: datetime
    verification: Verification
    category: str | None = None  # NSE's own category, e.g. "Outcome of Board Meeting"


class NewsRating(BaseModel):
    """Gemini's reading of one item, validated in Python (app/ai/news_rater.py)."""
    item_id: str
    prompt_version: str
    model: str
    rated_at: datetime
    sentiment: int = Field(ge=-2, le=2)
    materiality: Materiality
    relevance: Relevance
    event_type: EventType
    confirmation: Confirmation
    evidence: str  # a verbatim span of the item text
    summary: str
    cluster_key: str  # short label shared by items about the same underlying event


def _sha(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _ist(s: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(s, fmt).replace(tzinfo=IST)
    except (TypeError, ValueError):
        return None


def _day(s: str) -> date | None:
    d = _ist(s, "%d-%b-%Y")
    return d.date() if d else None


class NseClient:
    """Official NSE disclosures (announcements, board meetings, corporate actions). Undocumented
    website endpoints; one request at a time, politely spaced, like MarketLensProvider."""

    BASE = "https://www.nseindia.com/api"

    def __init__(self, aliases: dict[str, str] | None = None, pause_seconds: float = 1.0, clock=None):
        import httpx  # optional extra: pip install -e ".[fundamentals]"

        self.aliases = aliases or {}
        self.pause = pause_seconds
        self.now = clock or (lambda: datetime.now(IST))
        self._last = 0.0
        self.http = httpx.Client(timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
            "Accept": "application/json", "Referer": "https://www.nseindia.com/"})

    def _get(self, path: str, params: dict) -> list:
        wait = self.pause - (_time.monotonic() - self._last)
        if wait > 0:
            _time.sleep(wait)
        self._last = _time.monotonic()
        try:
            r = self.http.get(f"{self.BASE}/{path}", params=params)
        except Exception as e:  # network: report, never guess
            raise ProviderError(params.get("symbol", "?"), "ERROR", f"{path}: {e}") from e
        if r.status_code != 200:
            raise ProviderError(params.get("symbol", "?"), "ERROR", f"{path}: HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise ProviderError(params.get("symbol", "?"), "ERROR", f"{path}: not JSON") from e
        return body if isinstance(body, list) else body.get("data", []) if isinstance(body, dict) else []

    def fetch(self, symbol: str, days: int = 30) -> list[NewsItem]:
        """Announcements of the last `days`, upcoming/recent board meetings and corporate actions."""
        sym = self.aliases.get(symbol.upper(), base_symbol(symbol.upper()))
        now = self.now()
        today = now.astimezone(IST).date()
        fr, to = (today - timedelta(days=days)).strftime("%d-%m-%Y"), today.strftime("%d-%m-%Y")
        items: list[NewsItem] = []
        for a in self._get("corporate-announcements", {"index": "equities", "symbol": sym, "from_date": fr,
                                                       "to_date": to}):
            ident = a.get("seq_id") or _sha(a.get("attchmntFile") or "", a.get("an_dt") or "")
            published = _ist(a.get("an_dt") or "", "%d-%b-%Y %H:%M:%S")
            items.append(NewsItem(
                item_id=f"nse:{symbol}:{ident}", symbol=symbol, source_type="NSE_ANNOUNCEMENT",
                source_url=a.get("attchmntFile"), publisher="NSE (company disclosure)",
                title=a.get("desc") or "Announcement", text=(a.get("attchmntText") or "").strip(),
                published_at=published, event_date=published.date() if published else None, fetched_at=now,
                verification="OFFICIAL", category=a.get("desc")))
        for m in self._get("corporate-board-meetings", {"index": "equities", "symbol": sym}):
            meeting = _day(m.get("bm_date") or "")
            if meeting is None or meeting < today - timedelta(days=7):
                continue
            desc = (m.get("bm_desc") or "").strip()
            items.append(NewsItem(
                item_id=f"nse-bm:{symbol}:{meeting.isoformat()}:{_sha(desc)}", symbol=symbol,
                source_type="NSE_BOARD_MEETING", source_url=m.get("attachment") or None,
                publisher="NSE (board meeting)", title=m.get("bm_purpose") or "Board meeting", text=desc,
                published_at=_ist(m.get("bm_timestamp") or "", "%d-%b-%Y %H:%M:%S"), event_date=meeting,
                fetched_at=now, verification="OFFICIAL", category=m.get("bm_purpose")))
        for c in self._get("corporates-corporateActions", {"index": "equities", "symbol": sym}):
            ex = _day(c.get("exDate") or "")
            if ex is None or ex < today - timedelta(days=days):
                continue
            subject = (c.get("subject") or "").strip()
            items.append(NewsItem(
                item_id=f"nse-ca:{symbol}:{ex.isoformat()}:{_sha(subject)}", symbol=symbol,
                source_type="NSE_CORP_ACTION", source_url=None, publisher="NSE (corporate action)",
                title=subject or "Corporate action", text=subject, published_at=None, event_date=ex,
                effective_date=_day(c.get("recDate") or ""), fetched_at=now, verification="OFFICIAL",
                category="Corporate action"))
        return items


def _is_results(text: str) -> bool:
    t = text.lower()
    return "result" in t and ("financial" in t or "quarter" in t or "half year" in t or "year ended" in t)


def official_events(items: list[NewsItem], ratings: dict[str, NewsRating]) -> list[MarketEvent]:
    """RISK pathway (D11.1): deterministic events from official items only. Sentiment is never read."""
    out: list[MarketEvent] = []
    for it in items:
        if it.verification != "OFFICIAL":
            continue  # press never creates an event
        if it.source_type == "NSE_BOARD_MEETING" and it.event_date and _is_results(f"{it.title} {it.text}"):
            w = EVENT_WINDOWS["RESULTS"]
            out.append(MarketEvent(symbol=it.symbol, type="RESULTS", date=it.event_date,
                                   description=f"board meeting to consider results on {it.event_date.isoformat()}",
                                   source="news", source_id=it.item_id,
                                   before_days=w["before"], after_days=w["after"]))
        elif it.source_type == "NSE_CORP_ACTION" and it.event_date:
            out.append(MarketEvent(symbol=it.symbol, type="CORPORATE_ACTION", date=it.event_date,
                                   description=it.title, source="news", source_id=it.item_id))
        elif it.source_type == "NSE_ANNOUNCEMENT" and it.event_date:
            if _is_results(f"{it.category or ''} {it.text}") and "board meeting" not in (it.category or "").lower():
                w = EVENT_WINDOWS["RESULTS_ANNOUNCED"]
                out.append(MarketEvent(symbol=it.symbol, type="RESULTS_ANNOUNCED", date=it.event_date,
                                       description=f"results announced on {it.event_date.isoformat()}",
                                       source="news", source_id=it.item_id,
                                       before_days=w["before"], after_days=w["after"]))
            r = ratings.get(it.item_id)
            if r and r.materiality == "HIGH" and r.event_type in MATERIAL_EVENT_TYPES \
                    and r.relevance in ("DIRECT_COMPANY", "SUBSIDIARY") and r.confirmation == "CONFIRMED":
                w = EVENT_WINDOWS["MAJOR_EVENT"]
                out.append(MarketEvent(symbol=it.symbol, type="MAJOR_EVENT", date=it.event_date, major=True,
                                       description=f"{r.event_type.replace('_', ' ').lower()}: {r.summary}",
                                       source="news", source_id=it.item_id,
                                       before_days=w["before"], after_days=w["after"]))
    # NSE often lists the same meeting twice (intimation, then a revision): one event per type and date.
    seen, unique = set(), []
    for e in out:
        key = (e.type, e.date, e.description if e.type == "MAJOR_EVENT" else "")
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def next_results_date(items: list[NewsItem], today: date) -> date | None:
    days = [it.event_date for it in items if it.source_type == "NSE_BOARD_MEETING" and it.event_date
            and it.event_date >= today and _is_results(f"{it.title} {it.text}")]
    return min(days) if days else None

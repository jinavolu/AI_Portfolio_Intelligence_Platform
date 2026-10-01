"""SCORING pathway (decisions.md D11.2): validated, rated news → one number in [−1, +1], or None.

Fully deterministic: the same items and ratings always give the same score. Gemini only supplied the
per-item ratings; every weight, filter and cap below is Python.
"""

from __future__ import annotations

from datetime import date, datetime

from app.clock import IST
from app.news import NewsItem, NewsRating

WINDOW_DAYS = 7
CLUSTER_DAYS = 3
MATERIALITY_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.2}
SCORED_RELEVANCE = {"DIRECT_COMPANY", "SUBSIDIARY"}
SCORED_CONFIRMATION = {"CONFIRMED", "REPORTED"}
PRESS_CAP = 0.5  # press-only clusters' share of Σ m·r when an official cluster exists
# Not news about the business: routine filings, and reports about the share price itself (the price
# rules already measure that; scoring it again would count the same move twice).
UNSCORED_EVENT_TYPES = {"ROUTINE": "routine filing or page, not news", "PRICE_MOVE": "about the share price, already in the price rules"}


def recency_weight(age_days: int) -> float:
    return 1.0 if age_days <= 1 else 0.75 if age_days <= 3 else 0.5 if age_days <= 5 else 0.25


def _pub_day(it: NewsItem) -> date | None:
    return it.published_at.astimezone(IST).date() if it.published_at else None


def score_news(items: list[NewsItem], ratings: dict[str, NewsRating], today: date) -> dict:
    """Returns {score, confidence, clusters, excluded, counts}. score is None when nothing qualifies
    or confidence is LOW (D11.2): missing, never zero."""
    excluded, eligible = [], []
    for it in items:
        day = _pub_day(it)
        why = None
        if it.source_type in ("NSE_BOARD_MEETING", "NSE_CORP_ACTION"):
            why = "scheduled event, not news"
        elif day is None:
            why = "no publication date"
        elif not 0 <= (today - day).days <= WINDOW_DAYS:
            why = f"older than {WINDOW_DAYS} days"
        elif it.verification == "UNVERIFIED_CONTENT":
            why = "article content could not be verified"
        elif it.item_id not in ratings:
            why = "not rated"
        else:
            r = ratings[it.item_id]
            if r.relevance not in SCORED_RELEVANCE:
                why = f"about a {r.relevance.lower().replace('_', ' ')}, not the company itself"
            elif r.confirmation not in SCORED_CONFIRMATION:
                why = f"{r.confirmation.lower()}, not confirmed"
            elif r.event_type in UNSCORED_EVENT_TYPES:
                why = UNSCORED_EVENT_TYPES[r.event_type]
            elif r.sentiment == 0:
                # Neutral carries no direction: counting it would only drag the score toward zero.
                why = "neutral, no direction to score"
        if why:
            if it.source_type not in ("NSE_BOARD_MEETING", "NSE_CORP_ACTION"):
                excluded.append({"item_id": it.item_id, "title": it.title, "why": why})
            continue
        eligible.append((it, ratings[it.item_id], day))

    # Event clusters: same event type, same cluster key, within CLUSTER_DAYS. One contribution each.
    clusters: list[dict] = []
    for it, r, day in sorted(eligible, key=lambda x: (x[2], x[0].item_id)):
        key = r.cluster_key.strip().lower()
        home = next((c for c in clusters if c["event_type"] == r.event_type and c["key"] == key
                     and abs((day - c["last_day"]).days) <= CLUSTER_DAYS), None)
        if home is None:
            home = {"key": key, "event_type": r.event_type, "members": [], "last_day": day}
            clusters.append(home)
        home["members"].append((it, r, day))
        home["last_day"] = max(home["last_day"], day)

    out_clusters = []
    for c in clusters:
        # Representative: official first, then highest materiality, then most recent.
        rep_it, rep_r, rep_day = max(c["members"], key=lambda m: (
            m[0].verification == "OFFICIAL", MATERIALITY_WEIGHT[m[1].materiality], m[2]))
        official = any(m[0].verification == "OFFICIAL" for m in c["members"])
        age = (today - rep_day).days
        weight = MATERIALITY_WEIGHT[rep_r.materiality] * recency_weight(age)
        cid = f"{rep_it.symbol}:{c['event_type']}:{c['key']}:{min(m[2] for m in c['members']).isoformat()}"
        out_clusters.append({
            "cluster_id": cid, "event_type": c["event_type"], "official": official,
            "sentiment": rep_r.sentiment, "materiality": rep_r.materiality, "confirmation": rep_r.confirmation,
            "published": rep_day.isoformat(), "age_days": age, "weight": round(weight, 4),
            "summary": rep_r.summary, "evidence": rep_r.evidence,
            "sources": [{"item_id": m[0].item_id, "publisher": m[0].publisher, "url": m[0].source_url,
                         "title": m[0].title, "published": m[2].isoformat(), "official": m[0].verification == "OFFICIAL"}
                        for m in c["members"]],
            "duplicates": len(c["members"]) - 1})

    w_off = sum(c["weight"] for c in out_clusters if c["official"])
    w_press = sum(c["weight"] for c in out_clusters if not c["official"])
    press_scale = 1.0
    if w_off > 0 and w_press > w_off * PRESS_CAP / (1 - PRESS_CAP):  # press ≤ 50% of the total
        press_scale = (w_off * PRESS_CAP / (1 - PRESS_CAP)) / w_press
    for c in out_clusters:
        c["effective_weight"] = round(c["weight"] * (1.0 if c["official"] else press_scale), 4)

    publishers = {s["publisher"] for c in out_clusters if not c["official"] for s in c["sources"] if not s["official"]}
    confidence = "HIGH" if any(c["official"] for c in out_clusters) else "MEDIUM" if len(publishers) >= 2 else "LOW"
    total = sum(c["effective_weight"] for c in out_clusters)
    score = None
    if out_clusters and confidence != "LOW" and total > 0:
        score = round(sum(c["sentiment"] / 2 * c["effective_weight"] for c in out_clusters) / total, 4)
    out_clusters.sort(key=lambda c: (c["published"], c["effective_weight"]), reverse=True)
    return {"score": score, "confidence": confidence if out_clusters else None, "clusters": out_clusters,
            "excluded": excluded, "counts": {"items": len(items), "scored_clusters": len(out_clusters),
                                             "duplicates_merged": sum(c["duplicates"] for c in out_clusters)},
            "press_scaled": press_scale < 1.0}


def price_since(candles, published: str) -> float | None:
    """Display only: close change from the last close before publication to the latest close.
    It says what coincided with the news, never what the news caused."""
    day = date.fromisoformat(published)
    before = [c for c in candles if c.date < day]
    if not before or not candles:
        return None
    base = before[-1].close
    return round(candles[-1].close / base - 1, 4) if base else None


def now_day() -> date:
    return datetime.now(IST).date()

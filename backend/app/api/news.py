"""News (D11): status, refreshes, the live vs shadow rules; and fundamentals: status and refreshes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.ai.llm import LLMUnavailable
from app.api.context import ApiContext


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter()
    news, fundamentals, repo, settings = ctx.news, ctx.fundamentals, ctx.repo, ctx.settings

    def _news_on():
        if not news.enabled:
            raise HTTPException(400, "News is off (fixture mode or PI_NEWS_PROVIDER=none)")

    def _holdings_for_news() -> list[tuple[str, str]]:
        held = repo.current_holdings(by_weight=True)  # largest positions first: they get the Gemini quota first
        if not held:
            return [(s, "NSE") for s in sorted(repo.latest_theses())]
        return [(s, hs.portfolio.exchange) for s, hs in held.items()]

    @r.get("/api/news/status")
    def news_status():
        last = news.last_fetch()
        counts: dict[str, int] = {}
        for row in repo.news_statuses().values():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {"enabled": news.enabled, "press": settings.news_press and news.rater.enabled,
                "running": news.running, "progress": news.progress, "counts": counts,
                "last_fetch": last.isoformat() if last else None,
                "daily_refresh_ist": [settings.news_refresh_time, settings.news_evening_time],
                "gemini_paused_until": None if news._gemini_ok() else news.gemini_paused_until.isoformat(),
                "gemini_pause_reason": "" if news._gemini_ok() else news.gemini_pause_reason}

    @r.post("/api/news/refresh")
    def news_refresh_all(press: bool = True):
        """Refresh every holding in the background (official disclosures; press too unless press=false)."""
        _news_on()
        holdings = _holdings_for_news()
        return {"started": news.refresh_in_background(holdings, press), "symbols": len(holdings)}

    @r.post("/api/news/refresh/{symbol}")
    def news_refresh_one(symbol: str, press: bool = True):
        _news_on()
        sym = symbol.upper()
        exch = dict(_holdings_for_news()).get(sym, "NSE")
        try:
            return {"symbol": sym, "status": news.refresh_one(sym, exch, press)}
        except LLMUnavailable as e:
            raise HTTPException(503, str(e)) from e

    @r.get("/api/news/shadow")
    def news_shadow():
        """rules-1.3.0 (live) vs rules-1.4.0 (shadow) for every holding (D11.5)."""
        snap = ctx.service.current()
        rows = []
        for s, hs in sorted(snap.holdings.items(), key=lambda x: -x[1].portfolio.weight):
            sh = hs.shadow or {}
            n = hs.news or {}
            diag = sh.get("diagnostics") or {}
            rows.append({"symbol": s, "weight": hs.portfolio.weight,
                         "live": {"rule_version": hs.decision.rule_version, "state": hs.decision.state,
                                  "score": hs.decision.score},
                         "shadow": {k: sh.get(k) for k in ("rule_version", "state", "score")},
                         "impact": diag.get("impact"), "news_contribution": diag.get("news_contribution"),
                         "news_status": n.get("status"), "news_score": n.get("score"),
                         "confidence": n.get("confidence"), "events": len(n.get("clusters", []))})
        changed = sum(1 for row in rows if row["shadow"]["state"] and row["live"]["state"] != row["shadow"]["state"])
        return {"live_rules": ctx.service.rules.version,
                "shadow_rules": ctx.service.shadow_rules.version if ctx.service.shadow_rules else None,
                "changed_states": changed, "rows": rows}

    # --- fundamentals

    def _fundamentals_on():
        if not fundamentals.enabled:
            raise HTTPException(400, "Fundamentals are off (fixture mode, PI_FUNDAMENTALS_PROVIDER=none, "
                                     "or the optional 'fundamentals' extra is not installed)")

    @r.get("/api/fundamentals/status")
    def fundamentals_status():
        last = fundamentals.last_fetch()
        counts: dict[str, int] = {}
        for row in repo.all_fundamentals().values():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {"enabled": fundamentals.enabled, "provider": getattr(fundamentals.provider, "name", None),
                "running": fundamentals.running, "progress": fundamentals.progress,
                "last_fetch": last.isoformat() if last else None, "counts": counts,
                "daily_refresh_ist": settings.fundamentals_refresh_time}

    @r.post("/api/fundamentals/refresh")
    def fundamentals_refresh_all():
        """Refresh every current holding in the background (about 2 s per holding)."""
        _fundamentals_on()
        symbols = sorted(repo.current_holdings()) or sorted(repo.latest_theses())
        started = fundamentals.refresh_in_background(symbols)
        return {"started": started, "symbols": len(symbols), "running": fundamentals.running}

    @r.post("/api/fundamentals/refresh/{symbol}")
    def fundamentals_refresh_one(symbol: str):
        _fundamentals_on()
        return {"symbol": symbol.upper(), "status": fundamentals.refresh_one(symbol.upper())}

    return r

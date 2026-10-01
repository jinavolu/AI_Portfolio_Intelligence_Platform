"""Portfolio, holdings, the AI explanation, snapshots and their diffs, the scheduler's status."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException

from app.ai.explainer import QuotaExceeded
from app.ai.llm import LLMUnavailable
from app.api.context import ApiContext, Days, Limit
from app.services import attention_list
from app.snapshot import diff_snapshots


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter()
    service, repo, clock = ctx.service, ctx.repo, ctx.clock

    @r.get("/api/portfolio")
    def portfolio():
        snap = service.current()
        return {"snapshot_id": snap.id, "created_at": snap.created_at, "data_as_of": snap.data_as_of,
                "versions": snap.versions, "rule_set_validated": snap.rule_set_validated,
                "summary": snap.portfolio_summary, "risk": snap.risk.model_dump(exclude={"holdings"}),
                "attention": attention_list(snap)}

    @r.get("/api/holdings")
    def holdings():
        snap = service.current()
        return [{
            "symbol": s.symbol, "sector": s.portfolio.sector, "quantity": s.portfolio.quantity,
            "average_price": s.portfolio.average_price, "last_price": s.portfolio.last_price,
            "pnl": s.portfolio.pnl, "pnl_pct": s.portfolio.pnl_pct, "day_change_pct": s.portfolio.day_change_pct,
            "weight": s.portfolio.weight, "trend": s.technical.trend if s.technical else None,
            "signal": s.decision.state, "dont_add": s.decision.dont_add, "reason": s.decision.reason,
            "summary": s.decision.summary or s.decision.reason,
            "as_of": s.data_as_of,
            "thesis": None if s.thesis is None else {
                "version": s.thesis.version, "horizon": s.thesis.horizon, "draft": s.thesis.draft},
        } for s in snap.holdings.values()]

    @r.get("/api/holdings/{symbol}")
    def holding(symbol: str, chart_days: Days = 365):
        snap = service.current()
        hs = snap.holdings.get(symbol.upper())
        if hs is None:
            raise HTTPException(404, f"{symbol} is not a current holding")
        candles = service.candles(hs.symbol)
        cutoff = clock.today_ist() - timedelta(days=chart_days)
        return {"snapshot_id": snap.id, "holding": hs,
                "candles": [c for c in candles if c.date >= cutoff],
                "thesis_history": repo.thesis_history(hs.symbol)}

    @r.post("/api/holdings/{symbol}/explain")
    def explain(symbol: str):
        hs = service.current().holdings.get(symbol.upper())
        if hs is None:
            raise HTTPException(404, f"{symbol} is not a current holding")
        try:
            return ctx.explainer.explain(hs)
        except QuotaExceeded as e:
            raise HTTPException(429, str(e)) from e
        except LLMUnavailable as e:
            raise HTTPException(503, str(e)) from e

    # --- snapshots: /diff and /daily before /{snapshot_id}

    @r.post("/api/snapshots")
    def take_snapshot():
        snap = service.take_snapshot()
        return {"id": snap.id, "created_at": snap.created_at, "input_hash": snap.input_hash}

    @r.get("/api/snapshots")
    def list_snapshots(limit: Limit = 50):
        return repo.list_snapshots(limit)

    @r.get("/api/snapshots/daily")
    def list_daily_snapshots(limit: Limit = 60):
        return repo.list_daily(limit)

    @r.get("/api/snapshots/diff")
    def snapshot_diff(from_id: str | None = None, to_id: str | None = None, days: Days | None = None):
        """Default: the two most recent snapshots. With `days`: the current snapshot against the
        daily snapshot from `days` ago (or the oldest daily one, if history is shorter)."""
        if from_id and to_id:
            old, new = repo.get_snapshot(from_id), repo.get_snapshot(to_id)
        elif days:
            new = service.current()
            base = repo.daily_on_or_before(clock.today_ist() - timedelta(days=days)) or repo.earliest_daily()
            if base is None or base.snapshot_id == new.id:
                raise HTTPException(409, f"No daily snapshot from {days} day(s) ago yet. Daily snapshots are "
                                         f"taken after {ctx.settings.daily_snapshot_time} IST on weekdays.")
            old = repo.get_snapshot(base.snapshot_id)
        else:
            service.current()
            latest = repo.latest_snapshots(2)
            if len(latest) < 2:
                raise HTTPException(409, "Need at least two snapshots to diff")
            new, old = latest
        if old is None or new is None:
            raise HTTPException(404, "Snapshot not found")
        return diff_snapshots(old, new)

    @r.get("/api/snapshots/{snapshot_id}")
    def get_snapshot(snapshot_id: str):
        snap = repo.get_snapshot(snapshot_id)
        if snap is None:
            raise HTTPException(404, "Snapshot not found")
        return snap

    @r.get("/api/scheduler")
    def scheduler_status():
        return ctx.scheduler.status()

    return r

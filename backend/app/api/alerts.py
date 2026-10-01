"""Alerts and their Telegram notifications."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.context import ApiContext, Limit


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter()
    repo, clock, notifier = ctx.repo, ctx.clock, ctx.notifier

    @r.get("/api/alerts")
    def list_alerts(open_only: bool = False, limit: Limit = 200):
        return repo.list_alerts(open_only=open_only, limit=limit)

    @r.get("/api/alerts/count")
    def alert_count():
        return repo.open_alert_counts()

    @r.post("/api/alerts/ack-all")
    def acknowledge_all_alerts():
        return {"acknowledged": repo.acknowledge_alerts(clock.now())}

    @r.post("/api/alerts/{alert_id}/ack")
    def acknowledge_alert(alert_id: int):
        n = repo.acknowledge_alerts(clock.now(), alert_id)
        if not n:
            raise HTTPException(404, "Alert not found or already acknowledged")
        return {"acknowledged": n}

    # --- Telegram

    @r.get("/api/notify/status")
    def notify_status():
        return notifier.status()

    @r.post("/api/notify/link")
    def notify_link():
        """After you press Start in your bot's chat: find that chat, remember it, send a confirmation."""
        try:
            return {"linked": notifier.link()}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(502, str(e)) from e

    @r.post("/api/notify/test")
    def notify_test():
        try:
            notifier.test()
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(502, str(e)) from e
        return {"sent": True}

    return r

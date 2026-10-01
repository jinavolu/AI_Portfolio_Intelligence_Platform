"""Health and the broker session: status, the Kite MCP login, candle errors."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.context import ApiContext
from app.broker.base import BrokerError


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter()
    broker, service, scheduler = ctx.broker, ctx.service, ctx.scheduler

    @r.get("/health")
    def health():
        return {"status": "ok", "broker": broker.name, "read_only": True}

    def _bridge():
        bridge = getattr(broker, "login_bridge", None)
        if bridge is None:
            raise HTTPException(400, f"Broker {broker.name!r} has no interactive login")
        return bridge

    @r.get("/api/broker/status")
    def broker_status():
        bridge = getattr(broker, "login_bridge", None)
        base = {"broker": broker.name, "login_supported": bridge is not None, "account": ctx.repo.account}
        if bridge is not None and bridge.awaiting_login:
            return {**base, "connected": False, "awaiting_login": True,
                    "detail": "Waiting for the Kite login to finish"}
        try:
            broker.get_margins()
            return {**base, "connected": True, "awaiting_login": False}
        except BrokerError as e:
            return {**base, "connected": False, "awaiting_login": False, "detail": str(e)}

    @r.post("/api/broker/login")
    def broker_login():
        return {"login_url": _bridge().login_url()}

    @r.post("/api/broker/confirm-login")
    def broker_confirm_login():
        _bridge().confirm_login()
        status = broker_status()
        if status["connected"]:
            try:  # possibly a different Kite user: switch theses, snapshots and alerts to theirs
                status["account"] = service.sync_account()
            except BrokerError:
                pass  # the next snapshot retries it
            # Don't wait up to 15 min for the next refresh / daily retry: run on the next scheduler poll.
            scheduler.last_intraday = None
            scheduler.last_attempt = None
        return status

    @r.get("/api/broker/candle-errors")
    def candle_errors():
        return {"blocked": service.candles_blocked, "errors": service.candle_errors}

    @r.post("/api/broker/retry-candles")
    def retry_candles():
        service.retry_candles()
        return {"blocked": None}

    return r

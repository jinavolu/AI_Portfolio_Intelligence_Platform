"""HTTP routes, one router per area. Each module's `router(ctx)` builds its APIRouter over the shared
services in `ApiContext` (built once in main.create_app)."""

from __future__ import annotations

from fastapi import FastAPI

from app.api import alerts, backtest, broker, feed, misc, news, portfolio, theses
from app.api.context import ApiContext

# Registration order matters only where paths overlap within one router (e.g. /api/snapshots/diff
# before /api/snapshots/{snapshot_id}), which each module keeps.
ROUTERS = (broker, portfolio, theses, alerts, feed, news, backtest, misc)


def include_all(app: FastAPI, ctx: ApiContext) -> None:
    for module in ROUTERS:
        app.include_router(module.router(ctx))


__all__ = ["ApiContext", "include_all"]

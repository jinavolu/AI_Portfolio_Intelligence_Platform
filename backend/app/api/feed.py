"""The public Telegram channels feed (read-only) and the scanner (D14), which shows it beside each signal."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.context import ApiContext
from app.telegram_feed import FeedError


class ChannelIn(BaseModel):
    channel: str  # name, @name or t.me link


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter()
    feed, clock, scanner = ctx.feed, ctx.clock, ctx.scanner

    @r.get("/api/scanner")
    def scanner_view():
        held = ctx.held_symbols()
        return scanner.view(held, feed.mentions(held))  # Telegram: shown beside the signal, never scored

    @r.post("/api/scanner/run")
    def scanner_run(refresh_prices: bool = False):
        try:
            return scanner.start(refresh_prices)
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    @r.get("/api/feed")
    def telegram_feed(channel: str | None = None, before: int | None = None):
        try:
            return feed.feed(ctx.held_symbols(), channel, before)  # stored holdings: no Kite login needed
        except FeedError as e:
            raise HTTPException(400, str(e)) from e

    @r.post("/api/feed/channels")
    def feed_add_channel(body: ChannelIn):
        try:
            return feed.add_channel(body.channel, clock.now())
        except FeedError as e:
            raise HTTPException(400, str(e)) from e

    @r.delete("/api/feed/channels/{name}")
    def feed_remove_channel(name: str):
        feed.remove_channel(name, clock.now())
        return {"channels": feed.channels()}

    return r

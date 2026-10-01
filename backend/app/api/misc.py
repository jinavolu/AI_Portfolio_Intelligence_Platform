"""Tradebook import, AI usage and the chat assistant."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

from app.ai.llm import LLMUnavailable, model_chain
from app.api.context import ApiContext, Days
from app.broker.tradebook import TradebookError, parse_tradebook_csv


class ChatIn(BaseModel):
    message: str


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter()
    repo, settings = ctx.repo, ctx.settings

    @r.post("/api/tradebook")
    async def upload_tradebook(file: UploadFile):
        try:
            trades = parse_tradebook_csv((await file.read()).decode("utf-8"))
        except (TradebookError, UnicodeDecodeError) as e:
            raise HTTPException(422, str(e)) from e
        return {"imported": repo.replace_trades(trades)}

    @r.get("/api/usage")
    def usage(days: Days = 1):
        return repo.usage_summary(ctx.clock.now() - timedelta(days=days))

    @r.post("/api/chat")
    def chat(body: ChatIn):
        """Sync on purpose: FastAPI runs it in a worker thread with its own event loop, so the agent's
        tools (broker calls, the snapshot build lock) never block the server's loop."""
        if not settings.gemini_api_key:
            raise HTTPException(503, "Chat needs PI_GEMINI_API_KEY and the 'ai' extra installed")
        try:
            from app.ai.agent import ChatCapReached, chat_with_fallback
        except ImportError as e:  # pragma: no cover
            raise HTTPException(503, "Install the 'ai' extra: uv pip install -e .[ai]") from e
        try:
            return asyncio.run(chat_with_fallback(ctx.service, model_chain("chat", settings), body.message,
                                                  settings.gemini_api_key.get_secret_value()))
        except ChatCapReached as e:
            raise HTTPException(429, str(e)) from e
        except LLMUnavailable as e:
            raise HTTPException(503, str(e)) from e

    return r

"""FastAPI entrypoint. One process: REST APIs + ADK orchestration. The routes live in app/api/, one module
per area; this file builds the shared services and wires them in."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import time, timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.ai.explainer import Explainer
from app.ai.llm import build_llm_client
from app.ai.news_rater import NewsRater
from app.ai.suggester import Suggester
from app.api import ApiContext, include_all
from app.backtest import BacktestJob
from app.backtest import data as backtest_data
from app.backtest.data import HistoryFetchJob
from app.broker import build_broker
from app.broker.base import BrokerAdapter, BrokerError
from app.clock import Clock
from app.config import SAMPLE_DATA_DIR, Settings, get_settings
from app.db import Repository
from app.fundamentals_service import FundamentalsService, build_provider
from app.news_service import NewsService, build_nse_client
from app.notify import Notifier
from app.scanner import Scanner
from app.scheduler import DailySnapshotScheduler
from app.services import AnalysisService, load_events
from app.telegram_feed import ImageReader, TelegramFeed
from app.thesis import ThesisIn


def _seed_fixture_data(repo: Repository, clock: Clock) -> None:
    from app.broker.fixture_adapter import load_sample_trades

    if not repo.get_trades():
        repo.replace_trades(load_sample_trades())
    if not repo.latest_theses():
        for symbol, thesis in json.loads((SAMPLE_DATA_DIR / "theses.json").read_text(encoding="utf-8")).items():
            repo.save_thesis(symbol, ThesisIn.model_validate(thesis), clock.now())


def _hhmm(value: str) -> time:
    return time(*map(int, value.split(":")))


def create_app(settings: Settings | None = None, clock: Clock | None = None,
               broker: BrokerAdapter | None = None, llm=None) -> FastAPI:
    settings = settings or get_settings()
    clock = clock or Clock()
    repo = Repository(settings.resolved_database_url(), settings.instance)
    broker = broker or build_broker(settings, clock)
    if settings.broker == "fixture":
        _seed_fixture_data(repo, clock)
    events = load_events(SAMPLE_DATA_DIR / "events.json") if settings.broker == "fixture" \
        else load_events(settings.events_file)
    fundamentals = FundamentalsService(build_provider(settings, clock), repo, clock)
    llm_client = llm or build_llm_client(settings)
    news = NewsService(build_nse_client(settings, clock), NewsRater(repo, llm_client, settings, clock), repo,
                       settings, clock)
    service = AnalysisService(broker, repo, settings, clock, events=events, fundamentals=fundamentals, news=news)
    notifier = Notifier(settings, repo, clock)
    feed = TelegramFeed(repo, settings.telegram_web_url, today=clock.today_ist,
                        images=ImageReader(repo, settings, clock))
    notifier.telegram_mentions = feed.mentions  # the daily summary's "channels named your holdings" line
    scheduler = DailySnapshotScheduler(
        service, repo, clock, _hhmm(settings.daily_snapshot_time),
        intraday_every=timedelta(minutes=settings.intraday_refresh_minutes) if settings.intraday_refresh_minutes else None,
        fundamentals=fundamentals, fundamentals_at=_hhmm(settings.fundamentals_refresh_time),
        notifier=notifier,
        news=news, news_times=[(_hhmm(settings.news_refresh_time), True), (_hhmm(settings.news_evening_time), False)])
    backtest_data.configure(broker.name, settings.backtest_data_dir)  # also the scanner's price store
    ctx = ApiContext(
        settings=settings, clock=clock, repo=repo, broker=broker, service=service, notifier=notifier,
        explainer=Explainer(repo, llm_client, settings, clock), suggester=Suggester(repo, llm_client, settings, clock),
        scanner=Scanner(service, repo, clock), feed=feed, news=news, fundamentals=fundamentals, scheduler=scheduler,
        history_job=HistoryFetchJob(broker, clock.today_ist()), backtest_job=BacktestJob(repo, clock, service.rules))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if settings.scheduler_enabled:
            scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="AI Portfolio Intelligence Platform", version="0.1.0", lifespan=lifespan)
    app.state.scheduler = scheduler
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"],
                       allow_headers=["*"])
    app.state.service, app.state.repo, app.state.explainer = service, repo, ctx.explainer

    @app.exception_handler(BrokerError)
    def broker_error(_request, exc: BrokerError):
        return JSONResponse(status_code=503, content={
            "detail": str(exc), "broker": broker.name,
            "login_required": getattr(broker, "login_bridge", None) is not None})

    include_all(app, ctx)
    return app
# Run with: uvicorn app.main:create_app --factory

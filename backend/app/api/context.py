"""The services every router uses, built once per app in main.create_app."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Query

from app.backtest import BacktestJob
from app.backtest.data import HistoryFetchJob
from app.broker.base import BrokerAdapter
from app.clock import Clock
from app.config import Settings
from app.db import Repository
from app.fundamentals_service import FundamentalsService
from app.news_service import NewsService
from app.notify import Notifier
from app.scanner import Scanner
from app.scheduler import DailySnapshotScheduler
from app.services import AnalysisService
from app.telegram_feed import TelegramFeed

# Bounded query parameters: an unbounded `days` overflows timedelta (500), a negative `limit` lists everything.
Days = Annotated[int, Query(ge=1, le=3650)]
Limit = Annotated[int, Query(ge=1, le=1000)]


@dataclass
class ApiContext:
    settings: Settings
    clock: Clock
    repo: Repository
    broker: BrokerAdapter
    service: AnalysisService
    notifier: Notifier
    explainer: Any  # app.ai.explainer.Explainer
    suggester: Any  # app.ai.suggester.Suggester
    scanner: Scanner
    feed: TelegramFeed
    news: NewsService
    fundamentals: FundamentalsService
    scheduler: DailySnapshotScheduler
    history_job: HistoryFetchJob
    backtest_job: BacktestJob

    def held_symbols(self) -> set[str]:
        return set(self.repo.current_holdings())

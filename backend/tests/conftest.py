from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.ai.llm import OfflineTemplateClient
from app.broker.fixture_adapter import FixtureBrokerAdapter, load_sample_trades
from app.clock import FixedClock
from app.config import Settings
from app.db import Repository
from app.main import create_app

# Sunday; the last synthetic candle is Friday 2026-09-25.
NOW = datetime(2026, 9, 27, 6, 30, tzinfo=timezone.utc)


@pytest.fixture
def clock():
    return FixedClock(NOW)


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
                    validated_rules_file=tmp_path / "validated_rules.json")


class CountingLLM(OfflineTemplateClient):
    def __init__(self, text: str | None = None):
        self.calls = 0
        self.text = text

    def generate(self, model, system, prompt):
        self.calls += 1
        result = super().generate(model, system, prompt)
        if self.text is not None:
            result.text = self.text
        return result


@pytest.fixture
def llm():
    return CountingLLM()


@pytest.fixture
def broker(clock):
    return FixtureBrokerAdapter(clock)


@pytest.fixture
def repo(settings):
    """A repository seeded with the sample tradebook, as the app seeds it in fixture mode."""
    r = Repository(settings.resolved_database_url())
    r.replace_trades(load_sample_trades())
    return r


@pytest.fixture
def client(settings, clock, llm):
    return TestClient(create_app(settings=settings, clock=clock, llm=llm))

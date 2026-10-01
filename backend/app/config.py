"""Runtime configuration. Secrets come from environment variables only and are never logged."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DATA_DIR = BACKEND_DIR / "sample_data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="PI_", extra="ignore")

    # --- data sources ---
    broker: Literal["fixture", "kite_connect", "kite_mcp"] = "fixture"
    # Name of this backend process. Run one per Kite user (each has its own login) against the same
    # database; each instance remembers which user it last served (README: two portfolios at once).
    instance: str = "main"
    # Default: one SQLite file per broker, so sample data never mixes with the real account.
    database_url: str | None = None
    # None → sample map in fixture mode, backend/sector_map.json for a real account.
    sector_map_file: Path | None = None
    events_file: Path | None = None  # fixture mode always uses sample_data/events.json
    # Default: validated_rules_<broker>.json, so a backtest on synthetic sample data can never
    # graduate signals for the real account.
    validated_rules_file: Path | None = None
    backtest_data_dir: Path | None = None  # default backend/data; one eod_<broker> folder per broker
    candle_lookback_days: int = 400

    # --- Kite (read-only) ---
    kite_mcp_url: str = "https://mcp.kite.trade/mcp"
    kite_api_key: SecretStr | None = None
    kite_access_token: SecretStr | None = None

    # --- freshness limits (gate 1) ---
    max_holdings_age_hours: float = 18
    max_candle_age_days: int = 4  # calendar days; covers weekends

    # --- risk limits (gate 6) ---
    max_position_weight: float = 0.15
    max_sector_weight: float = 0.35

    # --- event gate (gate 5) ---
    results_window_days: int = 7

    # --- daily snapshots (local scheduler) ---
    scheduler_enabled: bool = True
    daily_snapshot_time: str = "16:00"  # IST, after market close
    intraday_refresh_minutes: int = 15  # market hours; drives alerts. 0 disables.

    # --- fundamentals (monitoring-design.md section 4) ---
    # None → "none" in fixture mode (offline sample data), "marketlens" for a real account.
    fundamentals_provider: Literal["marketlens", "none"] | None = None
    fundamentals_refresh_time: str = "07:00"  # IST, daily, before market open

    # --- news (decisions.md D11) ---
    # None → "none" in fixture mode, "nse" for a real account.
    news_provider: Literal["nse", "none"] | None = None
    news_press: bool = True  # Gemini-grounded press discovery (needs a Gemini key)
    news_refresh_time: str = "07:30"  # IST, daily: official + press
    news_evening_time: str = "18:30"  # IST, daily: official disclosures only (no press search)
    news_max_age_hours: float = 36
    # Free-tier Gemini quota: press search in full/scheduled refreshes only for the largest N holdings
    # (by weight); the rest get official NSE disclosures only. A manual per-holding refresh always searches.
    news_press_top_n: int = 20

    # --- notifications (Telegram bot → the owner's own chat) ---
    telegram_bot_token: SecretStr | None = None  # from @BotFather
    telegram_chat_id: str | None = None  # optional; normally found by POST /api/notify/link
    notify_min_severity: Literal["high", "medium"] = "medium"
    notify_quiet_start: str = "22:00"  # IST; alerts are held until quiet hours end
    notify_quiet_end: str = "07:00"
    notify_summary_time: str = "16:10"  # IST, weekdays, after the daily snapshot
    app_url: str = "http://localhost:5173"  # links in messages
    # Public channel feed (read-only, no login). telegram.me serves the same pages as t.me, which some
    # networks block.
    telegram_web_url: str = "https://telegram.me"
    # Read the text in feed images (news screenshots) offline with RapidOCR: no API, no quota.
    feed_read_images: bool = True

    # --- tax (listed equity, India; flags only, not tax advice) ---
    stcg_rate: float = 0.20
    ltcg_rate: float = 0.125

    # --- AI ---
    gemini_api_key: SecretStr | None = None
    # Pinned versions (not *-latest aliases) so cached explanations stay tied to one model.
    # Free-tier keys get no Pro quota, so the "strong" slot defaults to Flash too; on a paid key set
    # PI_GEMINI_STRONG_MODEL to a Pro model.
    gemini_fast_model: str = "gemini-3.8-flash"
    gemini_strong_model: str = "gemini-3.8-flash"
    # Tried in order when the primary is overloaded or its (free-tier, per-model) daily quota is used up.
    gemini_fallback_models: list[str] = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
    ai_daily_cap_per_feature: int = Field(default=200, ge=0)


    def resolved_validated_rules_file(self) -> Path:
        return self.validated_rules_file or BACKEND_DIR / f"validated_rules_{self.broker}.json"

    def resolved_sector_map_file(self) -> Path:
        if self.sector_map_file:
            return self.sector_map_file
        return SAMPLE_DATA_DIR / "sector_map.json" if self.broker == "fixture" else BACKEND_DIR / "sector_map.json"

    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{(BACKEND_DIR / f'portfolio_{self.broker}.db').as_posix()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Fetching and storing fundamentals, off the snapshot path.

A snapshot never calls the provider: it reads what was stored here. A refresh of all holdings takes
a couple of minutes (one polite request per second), so it runs once a day from the scheduler, or on
demand in a background thread.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from app.clock import Clock
from app.config import BACKEND_DIR, Settings
from app.db import Repository
from app.fundamentals import FundamentalsRecord, ProviderError, business_view, load_aliases

log = logging.getLogger("pi.fundamentals")


def build_provider(settings: Settings, clock: Clock):
    """None when fundamentals are off (fixture mode by default) or the optional httpx is missing."""
    name = settings.fundamentals_provider or ("none" if settings.broker == "fixture" else "marketlens")
    if name == "none":
        return None
    try:
        from app.fundamentals import MarketLensProvider
        return MarketLensProvider(load_aliases(BACKEND_DIR / "fundamentals_aliases.json"), clock=clock.now)
    except ImportError:
        log.warning("fundamentals disabled: install the optional extra with pip install -e '.[fundamentals]'")
        return None


class FundamentalsService:
    def __init__(self, provider, repo: Repository, clock: Clock):
        self.provider, self.repo, self.clock = provider, repo, clock
        self._lock = threading.Lock()
        self.running = False
        self.progress: dict = {}
        self._views: dict[tuple, dict] = {}

    @property
    def enabled(self) -> bool:
        return self.provider is not None

    def refresh_one(self, symbol: str) -> str:
        now = self.clock.now()
        try:
            rec: FundamentalsRecord = self.provider.fetch(symbol)
        except ProviderError as e:
            self.repo.save_fundamentals(symbol, now, e.kind, e.detail)
            return e.kind
        self.repo.save_fundamentals(symbol, now, "OK", "", rec.model_dump(mode="json"))
        return "OK"

    def refresh(self, symbols: list[str]) -> dict[str, int]:
        """Fetch every symbol once, one at a time. Returns counts by status."""
        if not self._lock.acquire(blocking=False):
            return {"already_running": 1}
        self.running, counts = True, {}
        self.progress = {"total": len(symbols), "done": 0, "started_at": self.clock.now().isoformat()}
        try:
            for sym in symbols:
                try:
                    status = self.refresh_one(sym)
                except Exception as e:  # noqa: BLE001 - one bad symbol must not stop the rest
                    log.exception("fundamentals refresh failed for %s", sym)
                    self.repo.save_fundamentals(sym, self.clock.now(), "ERROR", str(e))
                    status = "ERROR"
                counts[status] = counts.get(status, 0) + 1
                self.progress["done"] += 1
            self.progress["finished_at"] = self.clock.now().isoformat()
            self.progress["counts"] = counts
            return counts
        finally:
            self.running = False
            self._lock.release()

    def refresh_in_background(self, symbols: list[str]) -> bool:
        if self.running:
            return False
        threading.Thread(target=self.refresh, args=(symbols,), daemon=True, name="fundamentals").start()
        return True

    def views(self) -> dict[str, dict]:
        """Symbol -> the snapshot's view of its latest fetch."""
        out = {}
        for sym, row in self.repo.all_fundamentals().items():
            key = (sym, row["fetched_at"], row["status"])
            if key not in self._views:  # rebuilt only when a fetch changes
                if row["status"] == "OK" and row["data"]:
                    self._views[key] = business_view(FundamentalsRecord.model_validate(row["data"]))
                else:
                    self._views[key] = {"status": row["status"], "detail": row["detail"],
                                        "fetched_at": row["fetched_at"].isoformat()}
            out[sym] = self._views[key]
        return out

    def last_fetch(self) -> datetime | None:
        rows = self.repo.all_fundamentals()
        return max((r["fetched_at"] for r in rows.values()), default=None)

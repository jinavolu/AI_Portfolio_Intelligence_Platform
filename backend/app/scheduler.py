"""Local scheduler (plan §12: "Local scheduler initially"): one snapshot per trading day.

After `at` (IST) on a weekday, if today has no daily snapshot, it takes one. A failure
(usually "Kite not logged in", tokens expire daily) is retried every `retry` until it
succeeds or the day ends; the broker adapter itself refuses calls while a login is in
progress, so retries never disturb a pending login.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta

from app.broker.base import BrokerError
from app.clock import IST, Clock
from app.db import Repository
from app.services import AnalysisService

log = logging.getLogger("pi.scheduler")
RUNS_KEY = "scheduler_runs"  # shared setting: which slots of the shared daily jobs ran today


class DailySnapshotScheduler:
    MARKET_OPEN, MARKET_CLOSE = time(9, 15), time(15, 35)

    def __init__(self, service: AnalysisService, repo: Repository, clock: Clock, at: time,
                 retry: timedelta = timedelta(minutes=10), poll_seconds: float = 60,
                 intraday_every: timedelta | None = timedelta(minutes=15),
                 fundamentals=None, fundamentals_at: time | None = None,
                 news=None, news_times: list[tuple[time, bool]] | None = None, notifier=None):
        self.service, self.repo, self.clock = service, repo, clock
        # news_times: [(time IST, with press?)], e.g. 07:30 with press, 18:30 official disclosures only.
        self.news, self.news_times = news, news_times or []
        self.notifier = notifier
        self.fundamentals, self.fundamentals_at = fundamentals, fundamentals_at
        self.last_fundamentals_attempt: datetime | None = None
        self.at, self.retry, self.poll_seconds = at, retry, poll_seconds
        self.intraday_every = intraday_every
        self.last_attempt: datetime | None = None
        self.last_error: str | None = None
        self.last_intraday: datetime | None = None
        self.last_intraday_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def intraday_tick(self) -> bool:
        """During market hours, refresh the snapshot every `intraday_every` so alerts fire
        without the page open. A logged-out broker just skips the refresh."""
        if not self.intraday_every:
            return False
        now = self.clock.now()
        ist = now.astimezone(IST)
        if ist.weekday() >= 5 or not (self.MARKET_OPEN <= ist.time() <= self.MARKET_CLOSE):
            return False
        if self.last_intraday and now - self.last_intraday < self.intraday_every:
            return False
        self.last_intraday = now
        try:
            self.service.current()
        except BrokerError as e:
            self.last_intraday_error = str(e)
            return False
        self.last_intraday_error = None
        return True

    def fundamentals_tick(self) -> bool:
        """Once a day after `fundamentals_at` (IST): refresh fundamentals for the current holdings,
        taken from the latest snapshot so no broker login is needed. Runs in the scheduler thread."""
        f = self.fundamentals
        if not (f and f.enabled and self.fundamentals_at) or f.running:
            return False
        now = self.clock.now()
        ist = now.astimezone(IST)
        if ist.time() < self.fundamentals_at:
            return False
        last = f.last_fetch()
        if last and last.astimezone(IST).date() == ist.date():
            return False
        if self.last_fundamentals_attempt and now - self.last_fundamentals_attempt < self.retry:
            return False
        self.last_fundamentals_attempt = now
        latest = self.repo.latest_snapshots(1)
        # Claimed per retry window (the n-th of the day), so the other backend doesn't start the same fetch
        # alongside this one, while a failed fetch is still retried in the next window.
        seconds = ist.hour * 3600 + ist.minute * 60 + ist.second
        slot = f"w{int(seconds // max(60, self.retry.total_seconds()))}"
        if not latest or not self._claim("fundamentals", ist.date().isoformat(), slot):
            return False
        f.refresh_in_background(sorted(latest[0].holdings))  # about 2 s a holding: off the scheduler thread
        log.info("fundamentals refresh started")
        return True

    def notify_tick(self) -> int:
        """Send pending alerts to Telegram (the notifier handles quiet hours, retries and the summary)."""
        return self.notifier.tick() if self.notifier and self.notifier.enabled else 0

    def news_tick(self) -> bool:
        """Each configured time once a day: refresh news for the current holdings (from the latest
        snapshot, so no broker login is needed). The evening run skips the press search."""
        n = self.news
        if not (n and n.enabled and self.news_times) or n.running:
            return False
        ist = self.clock.now().astimezone(IST)
        for at, press in self.news_times:
            if ist.time() >= at and self._claim("news", ist.date().isoformat(), at.strftime("%H:%M")):
                latest = self.repo.latest_snapshots(1)
                if not latest:
                    return False
                # Minutes long (press search per holding): in its own thread, so the intraday refresh
                # and Telegram keep their schedule.
                n.refresh_in_background([(s, hs.portfolio.exchange) for s, hs in  # largest positions first
                                         sorted(latest[0].holdings.items(), key=lambda x: -x[1].portfolio.weight)],
                                        press)
                log.info("news refresh started (%s, press=%s)", at.strftime("%H:%M"), press)
                return True
        return False

    def _claim(self, job: str, day: str, slot: str) -> bool:
        """Record that today's `slot` of a shared job has started; False if it already had, by this process
        before a restart or by the other account's backend (news and fundamentals are shared, so one
        run serves both). Kept in the database, not memory: a restart (or --reload) doesn't repeat it."""
        runs = self.repo.get_setting(RUNS_KEY, shared=True) or {}
        done = runs.get(job, {}).get(day, [])
        if slot in done:
            return False
        # Only today's entries are kept: the record stays small.
        runs[job] = {day: [*done, slot]}
        self.repo.put_setting(RUNS_KEY, runs, self.clock.now(), shared=True)
        return True

    def due(self) -> bool:
        now = self.clock.now().astimezone(IST)
        return now.weekday() < 5 and now.time() >= self.at and self.repo.daily_for(now.date()) is None

    def tick(self) -> str | None:
        """Take today's daily snapshot if it is due. Returns the snapshot id when one was recorded."""
        if not self.due():
            return None
        now = self.clock.now()
        if self.last_attempt and now - self.last_attempt < self.retry:
            return None
        self.last_attempt = now
        try:
            snap = self.service.current()
        except BrokerError as e:
            self.last_error = str(e)
            log.info("daily snapshot postponed: %s", e)
            return None
        day = now.astimezone(IST).date()
        self.repo.record_daily(day, snap.id, now)
        self.last_error = None
        log.info("daily snapshot recorded: %s", snap.id)
        if self._claim("prune", day.isoformat(), "daily"):  # once a day, by one backend
            try:
                log.info("database pruned: %s", self.repo.prune(now))
            except Exception:  # noqa: BLE001 - pruning must never cost the day's snapshot
                log.exception("database prune failed")
        return snap.id

    def status(self) -> dict:
        today = self.clock.now().astimezone(IST).date()
        row = self.repo.daily_for(today)
        return {
            "snapshot_time_ist": self.at.strftime("%H:%M"),
            "today": today.isoformat(),
            "taken_today": row is not None,
            "taken_at": row.created_at.isoformat() if row else None,
            "due": self.due(),
            "last_attempt": self.last_attempt.isoformat() if self.last_attempt else None,
            "last_error": self.last_error,
            "intraday_every_minutes": int(self.intraday_every.total_seconds() // 60) if self.intraday_every else 0,
            "last_intraday_refresh": self.last_intraday.isoformat() if self.last_intraday else None,
            "last_intraday_error": self.last_intraday_error,
        }

    # ------------------------------------------------------------------ thread

    def _run(self) -> None:
        while not self._stop.is_set():
            for step in (self.tick, self.intraday_tick, self.notify_tick, self.fundamentals_tick, self.news_tick):
                try:
                    step()
                except Exception:  # noqa: BLE001 - the scheduler must never die
                    log.exception("scheduler step %s failed", step.__name__)
            self._stop.wait(self.poll_seconds)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="daily-snapshot")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

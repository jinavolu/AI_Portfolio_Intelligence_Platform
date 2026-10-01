"""PostgreSQL (or SQLite for local dev/tests) persistence.

One database serves every broker account. The owner's own data (theses, trades, snapshots, daily
snapshots, alerts, settings) carries an `account` column and every query is scoped to
`Repository.account`, the logged-in broker user. Market data (fundamentals, news, AI cache,
backtests) is per symbol and shared.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, Integer, String, UniqueConstraint,
                        create_engine, func, inspect, select, text, update)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from app.broker.models import Trade
from app.snapshot import Snapshot
from app.thesis import Thesis, ThesisIn

DEFAULT_ACCOUNT = "default"  # adapters without a user (fixture), or before the first login
LEGACY_ACCOUNT = "legacy"    # rows written before accounts existed, until an account claims them
SHARED = ""                  # settings that belong to the person, not an account (the Telegram chat)
ACTIVE_ACCOUNT_KEY = "active_account"
SHARED_SETTING_KEYS = frozenset({"telegram_chat"})  # moved to SHARED when an old database is migrated


class Base(DeclarativeBase):
    pass


class ThesisRow(Base):
    __tablename__ = "theses"
    __table_args__ = (UniqueConstraint("account", "symbol", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(40), index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TradeRow(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(40), index=True)
    trade_id: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    exchange: Mapped[str] = mapped_column(String(10))
    isin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    trade_date: Mapped[date] = mapped_column(Date)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)


class SnapshotRow(Base):
    __tablename__ = "snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[dict] = mapped_column(JSON)


class DailySnapshotRow(Base):
    """One snapshot per trading day, taken by the scheduler: the baseline for "what changed this week"."""
    __tablename__ = "daily_snapshots"
    account: Mapped[str] = mapped_column(String(40), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlertRow(Base):
    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("snapshot_id", "symbol", "type", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    type: Mapped[str] = mapped_column(String(40))
    key: Mapped[str] = mapped_column(String(80), default="")  # AlertIn.key: two sectors, two disclosures…
    severity: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(String(500))
    details: Mapped[dict] = mapped_column(JSON)
    snapshot_id: Mapped[str] = mapped_column(String(36))
    previous_snapshot_id: Mapped[str] = mapped_column(String(36))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    rules_version: Mapped[str] = mapped_column(String(40))
    horizon: Mapped[str] = mapped_column(String(20))
    passed: Mapped[bool] = mapped_column(Boolean)
    criteria_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)


class AppSettingRow(Base):
    """Owner-editable settings stored as JSON documents, e.g. default technical warnings."""
    __tablename__ = "app_settings"
    account: Mapped[str] = mapped_column(String(40), primary_key=True)
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FundamentalsRow(Base):
    """Latest fundamentals fetch per holding: OK (with the record), NOT_FOUND or ERROR. Snapshots keep
    the derived view, so history lives there."""
    __tablename__ = "fundamentals"
    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12))
    detail: Mapped[str] = mapped_column(String(500), default="")
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class NewsItemRow(Base):
    """One news source record (D11): raw data, kept apart from ratings so news can be re-rated."""
    __tablename__ = "news_items"
    item_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSON)


class NewsRatingRow(Base):
    __tablename__ = "news_ratings"
    item_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    prompt_version: Mapped[str] = mapped_column(String(40), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class NewsStatusRow(Base):
    """Per-holding fetch status: SUCCESS / PARTIAL / FAILED / DISABLED / NOT_COVERED (STALE is derived)."""
    __tablename__ = "news_status"
    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    status: Mapped[str] = mapped_column(String(12))
    detail: Mapped[str] = mapped_column(String(500), default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AiCacheRow(Base):
    __tablename__ = "ai_cache"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    feature: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(60))
    prompt_version: Mapped[str] = mapped_column(String(40))
    response: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AiUsageRow(Base):
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    feature: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(60))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_hit: Mapped[bool] = mapped_column(Boolean)
    grounded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


def _utc(dt: datetime) -> datetime:
    # SQLite drops tzinfo; everything we store is UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


ACCOUNT_TABLES = (ThesisRow, TradeRow, SnapshotRow, DailySnapshotRow, AlertRow, AppSettingRow)


def _add_account_column(engine) -> None:
    """Databases from before accounts: rebuild the owner tables with an `account` column (their keys
    change, which SQLite can't ALTER) and mark existing rows LEGACY until an account claims them."""
    from sqlalchemy import MetaData, Table

    insp = inspect(engine)
    old = [m.__table__ for m in ACCOUNT_TABLES if insp.has_table(m.__tablename__)
           and "account" not in {c["name"] for c in insp.get_columns(m.__tablename__)}]
    if not old:
        return
    with engine.begin() as conn:
        for table in old:
            reflected = Table(table.name, MetaData(), autoload_with=conn)
            rows = [dict(r) for r in conn.execute(select(reflected)).mappings()]
            reflected.drop(conn)
            table.create(conn)
            if rows:
                conn.execute(table.insert(), [{**r, "account": SHARED if r.get("key") in SHARED_SETTING_KEYS
                                               and table is AppSettingRow.__table__ else LEGACY_ACCOUNT}
                                              for r in rows])
            if conn.dialect.name == "postgresql" and "id" in table.c and isinstance(table.c.id.type, Integer):
                conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                                  f"COALESCE(MAX(id), 0) + 1, false) FROM {table.name}"))


def _add_alert_key(engine) -> None:
    """Databases from before AlertRow.key: rebuild the alerts table (its unique key changes, which
    SQLite can't ALTER), keeping every row with an empty key."""
    from sqlalchemy import MetaData, Table

    insp = inspect(engine)
    if not insp.has_table("alerts") or "key" in {c["name"] for c in insp.get_columns("alerts")}:
        return
    table = AlertRow.__table__
    with engine.begin() as conn:
        reflected = Table("alerts", MetaData(), autoload_with=conn)
        rows = [dict(r) for r in conn.execute(select(reflected)).mappings()]
        reflected.drop(conn)
        table.create(conn)
        if rows:
            conn.execute(table.insert(), [{**r, "key": ""} for r in rows])
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT setval(pg_get_serial_sequence('alerts', 'id'), "
                              "COALESCE(MAX(id), 0) + 1, false) FROM alerts"))


class Repository:
    def __init__(self, database_url: str, instance: str = "main"):
        # timeout: two backend processes (one per account) may write the same SQLite file at once.
        kwargs: dict = ({"connect_args": {"check_same_thread": False, "timeout": 30}}
                        if database_url.startswith("sqlite") else {})
        if database_url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool  # one shared connection, otherwise each gets an empty DB
        self.engine = create_engine(database_url, **kwargs)
        self._session = sessionmaker(self.engine, expire_on_commit=False)
        _add_account_column(self.engine)
        _add_alert_key(self.engine)
        Base.metadata.create_all(self.engine)
        self._active_key = ACTIVE_ACCOUNT_KEY if instance == "main" else f"{ACTIVE_ACCOUNT_KEY}:{instance}"
        self.account = (self.get_setting(self._active_key, shared=True) or {}).get("id", DEFAULT_ACCOUNT)

    def session(self) -> Session:
        return self._session()

    # --- accounts ---
    def set_account(self, account: str, now: datetime) -> None:
        """Scope every owner query to this broker user; remembered so a restart starts in it."""
        if account != self.account:
            self.account = account
            self.put_setting(self._active_key, {"id": account}, now, shared=True)

    def claim_legacy(self, holdings: set[str], min_overlap: float = 0.5) -> bool:
        """Rows from before accounts existed belong to whichever account made them. They go to the
        first real account that has no theses yet and whose holdings mostly have a legacy thesis.
        Snapshots move only if their holdings match this account's (another account's snapshots
        stay LEGACY); alerts and daily rows follow their snapshot."""
        if self.account in (DEFAULT_ACCOUNT, LEGACY_ACCOUNT) or not holdings:
            return False
        with self.session() as s, s.begin():
            legacy = set(s.scalars(select(ThesisRow.symbol).where(ThesisRow.account == LEGACY_ACCOUNT).distinct()))
            if (not legacy or len(holdings & legacy) < min_overlap * len(holdings)
                    or s.scalar(select(ThesisRow.id).where(ThesisRow.account == self.account).limit(1))):
                return False
            snap_ids = [i for i, data in s.execute(select(SnapshotRow.id, SnapshotRow.data)
                                                   .where(SnapshotRow.account == LEGACY_ACCOUNT))
                        if len(holdings & set(data.get("holdings", {}))) >= min_overlap * len(holdings)]
            to = {"account": self.account}
            for model in (ThesisRow, TradeRow):
                s.execute(update(model).where(model.account == LEGACY_ACCOUNT).values(to))
            keys = list(s.scalars(select(AppSettingRow.key).where(AppSettingRow.account == LEGACY_ACCOUNT)))
            s.query(AppSettingRow).filter(AppSettingRow.account == self.account, AppSettingRow.key.in_(keys)) \
                .delete(synchronize_session=False)
            s.execute(update(AppSettingRow).where(AppSettingRow.account == LEGACY_ACCOUNT).values(to))
            days = set(s.scalars(select(DailySnapshotRow.day).where(DailySnapshotRow.account == self.account)))
            for i in range(0, len(snap_ids), 500):
                chunk = snap_ids[i:i + 500]
                s.execute(update(SnapshotRow).where(SnapshotRow.id.in_(chunk)).values(to))
                s.execute(update(AlertRow).where(AlertRow.account == LEGACY_ACCOUNT,
                                                 AlertRow.snapshot_id.in_(chunk)).values(to))
                s.execute(update(DailySnapshotRow).where(
                    DailySnapshotRow.account == LEGACY_ACCOUNT, DailySnapshotRow.snapshot_id.in_(chunk),
                    DailySnapshotRow.day.not_in(days)).values(to))
        return True

    # --- theses (append-only history) ---
    def save_thesis(self, symbol: str, thesis: ThesisIn, now: datetime) -> Thesis:
        with self.session() as s, s.begin():
            current = s.scalar(select(func.max(ThesisRow.version)).where(
                ThesisRow.account == self.account, ThesisRow.symbol == symbol)) or 0
            row = ThesisRow(account=self.account, symbol=symbol, version=current + 1,
                            data=thesis.model_dump(mode="json"), created_at=now)
            s.add(row)
        return Thesis(symbol=symbol, version=row.version, updated_at=now, **thesis.model_dump())

    def latest_theses(self) -> dict[str, Thesis]:
        with self.session() as s:
            mine = ThesisRow.account == self.account
            latest = (select(ThesisRow.symbol, func.max(ThesisRow.version).label("v")).where(mine)
                      .group_by(ThesisRow.symbol).subquery())
            rows = s.scalars(select(ThesisRow).where(mine).join(
                latest, (ThesisRow.symbol == latest.c.symbol) & (ThesisRow.version == latest.c.v)))
            return {r.symbol: Thesis(symbol=r.symbol, version=r.version, updated_at=_utc(r.created_at), **r.data)
                    for r in rows}

    def thesis_history(self, symbol: str) -> list[Thesis]:
        with self.session() as s:
            rows = s.scalars(select(ThesisRow).where(ThesisRow.account == self.account, ThesisRow.symbol == symbol)
                             .order_by(ThesisRow.version))
            return [Thesis(symbol=r.symbol, version=r.version, updated_at=_utc(r.created_at), **r.data)
                    for r in rows]

    # --- trades ---
    def replace_trades(self, trades: list[Trade]) -> int:
        with self.session() as s, s.begin():
            s.query(TradeRow).filter(TradeRow.account == self.account).delete()
            s.add_all(TradeRow(account=self.account, **t.model_dump()) for t in trades)
        return len(trades)

    def get_trades(self) -> list[Trade]:
        with self.session() as s:
            return [Trade(trade_id=r.trade_id, symbol=r.symbol, exchange=r.exchange, isin=r.isin,
                          trade_date=r.trade_date, side=r.side, quantity=r.quantity, price=r.price)
                    for r in s.scalars(select(TradeRow).where(TradeRow.account == self.account))]

    # --- snapshots ---
    def save_snapshot(self, snap: Snapshot) -> Snapshot:
        snap = snap.model_copy(update={"id": snap.id or str(uuid.uuid4())})
        with self.session() as s, s.begin():
            s.add(SnapshotRow(id=snap.id, account=self.account, created_at=snap.created_at,
                              input_hash=snap.input_hash, data=snap.model_dump(mode="json")))
        return snap

    def get_snapshot(self, snapshot_id: str) -> Snapshot | None:
        with self.session() as s:
            row = s.get(SnapshotRow, snapshot_id)
            return Snapshot.model_validate(row.data) if row and row.account == self.account else None

    def list_snapshots(self, limit: int = 50) -> list[dict]:
        with self.session() as s:
            rows = s.execute(select(SnapshotRow.id, SnapshotRow.created_at, SnapshotRow.input_hash)
                             .where(SnapshotRow.account == self.account)
                             .order_by(SnapshotRow.created_at.desc()).limit(limit))
            return [{"id": i, "created_at": _utc(c).isoformat(), "input_hash": h} for i, c, h in rows]

    def latest_snapshots(self, n: int = 2) -> list[Snapshot]:
        ids = [r["id"] for r in self.list_snapshots(n)]
        return [snap for i in ids if (snap := self.get_snapshot(i))]

    def prune(self, now: datetime, keep_recent: int = 20, keep_days: int = 2, alert_days: int = 7,
              news_days: int = 90, cache_days: int = 30) -> dict[str, int]:
        """Delete what nothing needs any more; every account, since the tables are shared. Each snapshot
        is ~350 KB and a new one is stored whenever a price changes (intraday every 15 min), so without
        this the database grows by several MB a day. Kept: every daily snapshot (the "what changed"
        baselines), the last `keep_days` days, the newest `keep_recent` per account, and those behind
        alerts of the last `alert_days` days (their audit trail). News fetched more than `news_days`
        ago (and its ratings) and AI cache entries older than `cache_days` go too."""
        from sqlalchemy import delete

        out = {}
        with self.session() as s, s.begin():
            keep = set(s.scalars(select(DailySnapshotRow.snapshot_id)))
            for col in (AlertRow.snapshot_id, AlertRow.previous_snapshot_id):
                keep |= set(s.scalars(select(col).where(AlertRow.created_at >= now - timedelta(days=alert_days))))
            for account in s.scalars(select(SnapshotRow.account).distinct()):
                keep |= set(s.scalars(select(SnapshotRow.id).where(SnapshotRow.account == account)
                                      .order_by(SnapshotRow.created_at.desc()).limit(keep_recent)))
            old = [i for i in s.scalars(select(SnapshotRow.id).where(SnapshotRow.created_at < now - timedelta(days=keep_days)))
                   if i not in keep]
            for i in range(0, len(old), 500):  # SQLite caps the parameters in one IN (...)
                s.execute(delete(SnapshotRow).where(SnapshotRow.id.in_(old[i:i + 500])))
            out["snapshots"] = len(old)
            stale = list(s.scalars(select(NewsItemRow.item_id).where(
                NewsItemRow.fetched_at < now - timedelta(days=news_days))))
            for i in range(0, len(stale), 500):
                s.execute(delete(NewsRatingRow).where(NewsRatingRow.item_id.in_(stale[i:i + 500])))
                s.execute(delete(NewsItemRow).where(NewsItemRow.item_id.in_(stale[i:i + 500])))
            out["news_items"] = len(stale)
            out["ai_cache"] = s.execute(delete(AiCacheRow).where(
                AiCacheRow.created_at < now - timedelta(days=cache_days))).rowcount or 0
        if out["snapshots"] and self.engine.dialect.name == "sqlite":
            try:  # SQLite keeps freed pages in the file until vacuumed
                with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                    conn.execute(text("VACUUM"))
            except Exception as e:  # noqa: BLE001 - the other backend is writing: the space is reused anyway
                out["vacuum_skipped"] = str(e)[:120]
        return out

    def current_holdings(self, by_weight: bool = False) -> dict:
        """{symbol: HoldingSnapshot} from the latest STORED snapshot ({} before the first one), so jobs that
        only need the list (news, fundamentals, the Telegram feed) work without a Kite login. by_weight:
        largest position first."""
        latest = self.latest_snapshots(1)
        holdings = latest[0].holdings if latest else {}
        if by_weight:
            return dict(sorted(holdings.items(), key=lambda x: -x[1].portfolio.weight))
        return holdings

    # --- alerts ---
    def save_alerts(self, alerts, snapshot_id: str, previous_snapshot_id: str, now: datetime,
                    repeat_after: timedelta = timedelta(days=5)) -> int:
        """Skips an alert when the same symbol+type+key is still open (unacknowledged) and recent,
        so a steady downtrend doesn't re-alert at every lower support level, while a second disclosure
        or another sector still does."""
        with self.session() as s, s.begin():
            seen = set(s.execute(
                select(AlertRow.symbol, AlertRow.type, AlertRow.key).where(
                    AlertRow.account == self.account, AlertRow.acknowledged_at.is_(None),
                    AlertRow.created_at >= now - repeat_after)).tuples())
            fresh = []
            for a in alerts:
                if (a.symbol, a.type, a.key) not in seen:  # also drops repeats within this batch
                    seen.add((a.symbol, a.type, a.key))
                    fresh.append(a)
            alerts = fresh
            for a in alerts:
                s.add(AlertRow(account=self.account, created_at=now, symbol=a.symbol, type=a.type, key=a.key,
                               severity=a.severity, message=a.message[:500], details=a.details,
                               snapshot_id=snapshot_id, previous_snapshot_id=previous_snapshot_id))
        return len(alerts)

    def list_alerts(self, open_only: bool = False, limit: int = 200) -> list[dict]:
        with self.session() as s:
            q = (select(AlertRow).where(AlertRow.account == self.account)
                 .order_by(AlertRow.created_at.desc(), AlertRow.id.desc()).limit(limit))
            if open_only:
                q = q.where(AlertRow.acknowledged_at.is_(None))
            return [{"id": r.id, "created_at": _utc(r.created_at).isoformat(), "symbol": r.symbol, "type": r.type,
                     "severity": r.severity, "message": r.message, "details": r.details,
                     "snapshot_id": r.snapshot_id, "previous_snapshot_id": r.previous_snapshot_id,
                     "acknowledged_at": _utc(r.acknowledged_at).isoformat() if r.acknowledged_at else None}
                    for r in s.scalars(q)]

    def open_alert_counts(self) -> dict[str, int]:
        with self.session() as s:
            rows = s.execute(select(AlertRow.severity, func.count())
                             .where(AlertRow.account == self.account, AlertRow.acknowledged_at.is_(None))
                             .group_by(AlertRow.severity))
            counts = {sev: n for sev, n in rows}
        return {"total": sum(counts.values()), **counts}

    def acknowledge_alerts(self, now: datetime, alert_id: int | None = None) -> int:
        with self.session() as s, s.begin():
            q = s.query(AlertRow).filter(AlertRow.account == self.account, AlertRow.acknowledged_at.is_(None))
            if alert_id is not None:
                q = q.filter(AlertRow.id == alert_id)
            return q.update({AlertRow.acknowledged_at: now})

    # --- backtests ---
    def save_backtest(self, result: dict, now: datetime) -> int:
        with self.session() as s, s.begin():
            row = BacktestRunRow(created_at=now, rules_version=result["rules_version"], horizon=result["horizon"],
                                 passed=result["passed"], criteria_hash=result["criteria_hash"], result=result)
            s.add(row)
        return row.id

    def list_backtests(self) -> list[dict]:
        with self.session() as s:
            rows = s.scalars(select(BacktestRunRow).order_by(BacktestRunRow.created_at.desc()))
            out = []
            for r in rows:
                window = r.result.get("window", "development")
                judged = r.result["periods"]["holdout" if window == "holdout" else "out_of_sample"]
                out.append({"id": r.id, "created_at": _utc(r.created_at).isoformat(), "window": window,
                            "rules_version": r.rules_version, "horizon": r.horizon, "passed": r.passed,
                            "criteria_hash": r.criteria_hash, "oos_excess_cagr": judged["excess_cagr"],
                            "universe": r.result["universe"]["name"]})
            return out

    def get_backtest(self, run_id: int) -> dict | None:
        with self.session() as s:
            r = s.get(BacktestRunRow, run_id)
            return {"id": r.id, "created_at": _utc(r.created_at).isoformat(), **r.result} if r else None

    # --- daily snapshots ---
    def record_daily(self, day: date, snapshot_id: str, now: datetime) -> None:
        with self.session() as s, s.begin():
            s.merge(DailySnapshotRow(account=self.account, day=day, snapshot_id=snapshot_id, created_at=now))

    def daily_for(self, day: date) -> DailySnapshotRow | None:
        with self.session() as s:
            return s.get(DailySnapshotRow, (self.account, day))

    def daily_on_or_before(self, day: date) -> DailySnapshotRow | None:
        with self.session() as s:
            return s.scalar(select(DailySnapshotRow)
                            .where(DailySnapshotRow.account == self.account, DailySnapshotRow.day <= day)
                            .order_by(DailySnapshotRow.day.desc()).limit(1))

    def earliest_daily(self) -> DailySnapshotRow | None:
        with self.session() as s:
            return s.scalar(select(DailySnapshotRow).where(DailySnapshotRow.account == self.account)
                            .order_by(DailySnapshotRow.day).limit(1))

    def list_daily(self, limit: int = 60) -> list[dict]:
        with self.session() as s:
            rows = s.scalars(select(DailySnapshotRow).where(DailySnapshotRow.account == self.account)
                             .order_by(DailySnapshotRow.day.desc()).limit(limit))
            return [{"day": r.day.isoformat(), "snapshot_id": r.snapshot_id,
                     "created_at": _utc(r.created_at).isoformat()} for r in rows]

    # --- settings (per account; shared=True for ones that belong to the person, e.g. the Telegram chat) ---
    def get_setting(self, key: str, shared: bool = False) -> dict | None:
        with self.session() as s:
            row = s.get(AppSettingRow, (SHARED if shared else self.account, key))
            return row.data if row else None

    def put_setting(self, key: str, data: dict, now: datetime, shared: bool = False) -> None:
        with self.session() as s, s.begin():
            s.merge(AppSettingRow(account=SHARED if shared else self.account, key=key, data=data, updated_at=now))

    # --- fundamentals ---
    def save_fundamentals(self, symbol: str, fetched_at: datetime, status: str, detail: str = "",
                          data: dict | None = None) -> None:
        with self.session() as s, s.begin():
            s.merge(FundamentalsRow(symbol=symbol, fetched_at=fetched_at, status=status, detail=detail[:500],
                                    data=data))

    def all_fundamentals(self) -> dict[str, dict]:
        with self.session() as s:
            return {r.symbol: {"fetched_at": _utc(r.fetched_at), "status": r.status, "detail": r.detail,
                               "data": r.data} for r in s.scalars(select(FundamentalsRow))}

    # --- news ---
    def upsert_news_items(self, items: list[dict], fetched_at: datetime) -> None:
        with self.session() as s, s.begin():
            for d in items:
                s.merge(NewsItemRow(item_id=d["item_id"], symbol=d["symbol"], fetched_at=fetched_at, data=d))

    def news_items(self, symbol: str | None = None) -> list[dict]:
        with self.session() as s:
            q = select(NewsItemRow)
            if symbol:
                q = q.where(NewsItemRow.symbol == symbol)
            return [r.data for r in s.scalars(q)]

    def save_news_ratings(self, ratings: list[dict]) -> None:
        with self.session() as s, s.begin():
            for d in ratings:
                s.merge(NewsRatingRow(item_id=d["item_id"], prompt_version=d["prompt_version"], data=d))

    def news_ratings(self, prompt_version: str) -> dict[str, dict]:
        with self.session() as s:
            return {r.item_id: r.data for r in s.scalars(
                select(NewsRatingRow).where(NewsRatingRow.prompt_version == prompt_version))}

    def save_news_status(self, symbol: str, status: str, detail: str, fetched_at: datetime) -> None:
        with self.session() as s, s.begin():
            s.merge(NewsStatusRow(symbol=symbol, status=status, detail=detail[:500], fetched_at=fetched_at))

    def news_statuses(self) -> dict[str, dict]:
        with self.session() as s:
            return {r.symbol: {"status": r.status, "detail": r.detail, "fetched_at": _utc(r.fetched_at)}
                    for r in s.scalars(select(NewsStatusRow))}

    # --- AI cache + usage ---
    def cache_get(self, key: str) -> dict | None:
        with self.session() as s:
            row = s.get(AiCacheRow, key)
            return row.response if row else None

    def cache_put(self, key: str, feature: str, model: str, prompt_version: str, response: dict,
                  now: datetime) -> None:
        with self.session() as s, s.begin():
            s.merge(AiCacheRow(key=key, feature=feature, model=model, prompt_version=prompt_version,
                               response=response, created_at=now))

    def log_usage(self, now: datetime, feature: str, model: str, cache_hit: bool, input_tokens: int = 0,
                  output_tokens: int = 0, grounded: bool | None = None) -> None:
        with self.session() as s, s.begin():
            s.add(AiUsageRow(at=now, feature=feature, model=model, cache_hit=cache_hit,
                             input_tokens=input_tokens, output_tokens=output_tokens, grounded=grounded))

    def model_calls_since(self, feature: str, since: datetime) -> int:
        with self.session() as s:
            return s.scalar(select(func.count()).select_from(AiUsageRow).where(
                AiUsageRow.feature == feature, AiUsageRow.at >= since, AiUsageRow.cache_hit.is_(False))) or 0

    def usage_summary(self, since: datetime) -> list[dict]:
        with self.session() as s:
            rows = s.execute(
                select(AiUsageRow.feature, AiUsageRow.model, AiUsageRow.cache_hit, func.count(),
                       func.sum(AiUsageRow.input_tokens), func.sum(AiUsageRow.output_tokens))
                .where(AiUsageRow.at >= since)
                .group_by(AiUsageRow.feature, AiUsageRow.model, AiUsageRow.cache_hit))
            return [{"feature": f, "model": m, "cache_hit": c, "calls": n, "input_tokens": i or 0,
                     "output_tokens": o or 0} for f, m, c, n, i, o in rows]
